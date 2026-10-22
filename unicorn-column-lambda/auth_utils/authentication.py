import json
import os

import boto3
from benchling_sdk.auth.client_credentials_oauth2 import ClientCredentialsOAuth2
from benchling_sdk.benchling import Benchling
from benchling_sdk.helpers.retry_helpers import RetryStrategy

import config
from sync.models import AuthenticationError

_KEYS = ("url", "client_id", "client_secret")
_UNSET_SECRET = "SET-AFTER-DEPLOY"  # dev or prod placeholder; not a real secret
_client = None


def _load_credentials():
    """Secrets Manager or SSM Parameter Store in Lambda (see config.BENCHLING_SECRET_ID);
    BENCHLING_URL/CLIENT_ID/CLIENT_SECRET env vars for local runs."""
    env = {"url": os.getenv("BENCHLING_URL"), "client_id": os.getenv("BENCHLING_CLIENT_ID"),
           "client_secret": os.getenv("BENCHLING_CLIENT_SECRET")}
    if config.BENCHLING_SECRET_ID:
        try:
            raw = boto3.client("secretsmanager").get_secret_value(
                SecretId=config.BENCHLING_SECRET_ID)["SecretString"]
            # The secret may hold only client_secret; url/client_id then come from the env vars.
            creds = {**env, **{k: v for k, v in json.loads(raw).items() if v}}
        except Exception as e:
            raise AuthenticationError(f"Could not read secret {config.BENCHLING_SECRET_ID}: {e}") from e
    elif config.BENCHLING_SSM_PARAMETER:
        try:
            raw = boto3.client("ssm").get_parameter(Name=config.BENCHLING_SSM_PARAMETER,
                                                    WithDecryption=True)["Parameter"]["Value"]
            creds = json.loads(raw)
        except Exception as e:
            raise AuthenticationError(f"Could not read SSM parameter {config.BENCHLING_SSM_PARAMETER}: {e}") from e
    else:
        creds = env
    if creds.get("client_secret") == _UNSET_SECRET:
        raise AuthenticationError(f"Secret {config.BENCHLING_SECRET_ID} still holds the deploy placeholder - "
                                  "set the Benchling client_secret with 'aws secretsmanager put-secret-value'")
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
