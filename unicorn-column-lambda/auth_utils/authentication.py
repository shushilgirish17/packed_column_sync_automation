import json
import os

import boto3
from benchling_sdk.auth.client_credentials_oauth2 import ClientCredentialsOAuth2
from benchling_sdk.benchling import Benchling
from benchling_sdk.helpers.retry_helpers import RetryStrategy

import config
from sync.models import AuthenticationError

_KEYS = ("url", "client_id", "client_secret")
_client = None


def _load_credentials():
    """SSM Parameter Store in Lambda; BENCHLING_URL/CLIENT_ID/CLIENT_SECRET env vars for local runs."""
    if config.BENCHLING_SSM_PARAMETER:
        try:
            raw = boto3.client("ssm").get_parameter(Name=config.BENCHLING_SSM_PARAMETER,
                                                    WithDecryption=True)["Parameter"]["Value"]
            creds = json.loads(raw)
        except Exception as e:
            raise AuthenticationError(f"Could not read SSM parameter {config.BENCHLING_SSM_PARAMETER}: {e}") from e
    else:
        creds = {"url": os.getenv("BENCHLING_URL"), "client_id": os.getenv("BENCHLING_CLIENT_ID"),
                 "client_secret": os.getenv("BENCHLING_CLIENT_SECRET")}
    missing = [k for k in _KEYS if not creds.get(k)]
    if missing:
        raise AuthenticationError(f"Benchling credentials missing: {', '.join(missing)}")
    return creds


def initialize_benchling() -> Benchling:
    """Cached across warm invocations. SDK retries 429/5xx with exponential backoff."""
    global _client
    if _client is None:
        creds = _load_credentials()
        url = creds["url"].rstrip("/")
        _client = Benchling(
            url=url,
            auth_method=ClientCredentialsOAuth2(
                client_id=creds["client_id"],
                client_secret=creds["client_secret"],
                token_url=url + "/api/v2/token",
            ),
            retry_strategy=RetryStrategy(max_tries=5, backoff_factor=1.0),
        )
    return _client
