"""AWS (ADR 0015): what `${aws-sm:...}` references and `aws-rds-iam` credentials resolve through - Secrets Manager, and
IAM authentication tokens for RDS and Aurora.

Through boto3 (the `aws` extra), with the standard credential chain - IRSA on EKS, an instance or task role, the usual
AWS_* environment variables - and region (QUERYAPIGATE_AWS_REGION, else AWS_REGION and the rest of the chain).
boto3's own AWS_ENDPOINT_URL_SECRETS_MANAGER points it elsewhere, for a VPC endpoint or a local stand-in.

Nothing here decides what may be read - secretrefs.py checks QUERYAPIGATE_AWS_SECRET_PREFIXES and
QUERYAPIGATE_AWS_RDS_HOSTS first.
"""
import json
import threading

from . import config

RDS_TOKEN_SECONDS = 900  # an IAM authentication token is good for 15 minutes


class AwsError(Exception):
    """What AWS, or reaching it, refused - the message names why, never a secret."""


_clients: dict[str, object] = {}
_lock = threading.Lock()


def _client(service):
    with _lock:
        if service not in _clients:
            try:
                import boto3
            except ImportError:
                raise AwsError('the "boto3" package is not installed - pip install "queryapigate[aws]"') from None
            _clients[service] = boto3.client(service, region_name=config.aws_region())
        return _clients[service]


def reset():
    with _lock:
        _clients.clear()


def _failure(error):
    """An AWS error as a sentence: its code and message, never what was asked for in full."""
    response = getattr(error, 'response', None) or {}
    code = (response.get('Error') or {}).get('Code')
    message = (response.get('Error') or {}).get('Message') or str(error)
    return f'{code}: {message}' if code else message


def secret(secret_id, field=None):
    """A Secrets Manager secret's SecretString - or, with `field`, that field of its JSON: (value, None)."""
    try:
        answer = _client('secretsmanager').get_secret_value(SecretId=secret_id)
    except AwsError:
        raise
    except Exception as error:  # botocore's ClientError, or one of its connection errors
        raise AwsError(_failure(error)) from None
    text = answer.get('SecretString')
    if text is None:
        raise AwsError(f'{secret_id} holds binary data, not text')
    if field is None:
        return text, None
    try:
        data = json.loads(text)
    except ValueError:
        raise AwsError(f"{secret_id} isn't JSON, so it has no field '{field}'") from None
    if not isinstance(data, dict) or field not in data:
        raise AwsError(f"{secret_id} has no field '{field}'")
    if not isinstance(data[field], str):
        raise AwsError(f"{secret_id}#{field} isn't text")
    return data[field], None


def rds_token(host, port, user):
    """An IAM authentication token for `user` on the database at host:port - signed here, from the server's own AWS
    credentials, with no call to AWS: ((user, token), seconds it lasts)."""
    try:
        region = {'Region': config.aws_region()} if config.aws_region() else {}  # else the client's own region
        token = _client('rds').generate_db_auth_token(DBHostname=host, Port=int(port), DBUsername=user, **region)
    except AwsError:
        raise
    except Exception as error:  # no credentials, no region
        raise AwsError(_failure(error)) from None
    return (user, token), RDS_TOKEN_SECONDS
