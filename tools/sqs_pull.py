"""
sqs_pull.py
-----------
Producer for the watch folder, fed by AWS instead of Benchling directly.

    Benchling canvas -> Lambda -> S3 (<stem>.zip + <stem>.job.json) + SQS message
                     -> sqs_pull.py -> <WATCH_DIR>/incoming -> watch_service.ps1 -> UNICORN

For each SQS message it:
  1. downloads the import archive from S3 (message["s3"]["zipKey"]),
  2. checks the archive's CRCs (lib/unicorn_coltype.verify_archive),
  3. writes <stem>.zip, then <stem>.job.json into <WATCH_DIR>/incoming
     (the zip first - watch_service triggers on the .job.json),
  4. deletes the message.
A message is deleted only after both files are in place, so a crash mid-way just
means SQS redelivers it after the visibility timeout. A bad archive goes to
<WATCH_DIR>/failed with its job, and the message is deleted.

Config (.env or environment):
  SQS_QUEUE_URL   https://sqs.us-east-2.amazonaws.com/<account>/packed-column-sync.fifo
  AWS_REGION      default us-east-2
  AWS_PROFILE     optional; the `aws configure sso` profile to use
  WATCH_DIR       same folder watch_service.ps1 watches

Usage:
  python tools/sqs_pull.py           # keep polling (long-poll, 20 s)
  python tools/sqs_pull.py --once    # take what is waiting, then exit

Needs boto3:  pip install boto3
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "lib"))
import config
import unicorn_coltype
from config import settings


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def _aws_clients():
    try:
        import boto3
    except ImportError:
        sys.exit("boto3 not installed. Run: pip install boto3")
    session = boto3.Session(profile_name=config._get("AWS_PROFILE", "") or None,
                            region_name=config._get("AWS_REGION", "us-east-2"))
    return session.client("sqs"), session.client("s3")


def _write_atomic(path: pathlib.Path, data: bytes) -> None:
    """Write under a temporary name, then rename, so the watcher never sees a half file."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def handle(message: dict, s3, dirs: dict) -> str:
    """Hand one job to the watch folder. Returns 'incoming' or 'failed'."""
    job = json.loads(message["Body"])
    loc = job["s3"]
    zip_name = job["zip"]
    stem = zip_name[:-len(".zip")]

    archive = s3.get_object(Bucket=loc["bucket"], Key=loc["zipKey"])["Body"].read()

    tmp_zip = dirs["failed"] / f"{zip_name}.check"
    tmp_zip.write_bytes(archive)
    problems = unicorn_coltype.verify_archive(str(tmp_zip))
    tmp_zip.unlink()

    target = dirs["failed"] if problems else dirs["incoming"]
    _write_atomic(target / zip_name, archive)
    _write_atomic(target / f"{stem}.job.json", json.dumps(job, indent=2).encode("utf-8"))

    if problems:
        _log(f"  ! {stem}: archive failed CRC check {problems} -> failed/")
        return "failed"
    _log(f"  {stem} -> incoming/  (column '{job['columnName']}', source {job.get('source')})")
    return "incoming"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="drain what is waiting, then exit")
    args = ap.parse_args()

    queue_url = config._get("SQS_QUEUE_URL", "")
    if not queue_url:
        sys.exit("SQS_QUEUE_URL not set in .env")

    dirs = {name: settings.watch_dir / name for name in ("incoming", "failed")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    sqs, s3 = _aws_clients()
    _log(f"sqs_pull started - {queue_url} -> {dirs['incoming']}")

    while True:
        try:
            resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20)
        except Exception as e:  # network / expired SSO session: report and keep going
            _log(f"  ! receive failed: {e}")
            if args.once:
                return 1
            time.sleep(30)
            continue

        messages = resp.get("Messages", [])
        if not messages:
            if args.once:
                _log("queue empty - done")
                return 0
            continue

        for m in messages:
            try:
                handle(m, s3, dirs)
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])
            except Exception as e:  # leave the message; SQS redelivers it after the visibility timeout
                _log(f"  ! message {m.get('MessageId')} not handed over, will retry: {e}")
                if args.once:
                    return 1


if __name__ == "__main__":
    sys.exit(main())
