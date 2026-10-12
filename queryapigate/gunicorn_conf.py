"""gunicorn's settings for QueryAPIGate (`gunicorn -c python:queryapigate.gunicorn_conf ...`) - today, the access log's
format: paths without their query strings, unless QUERYAPIGATE_ACCESS_LOG_QUERY=1 (accesslog.py, ADR 0016). The
image uses it; so can any gunicorn command running QueryAPIGate."""
from queryapigate.accesslog import gunicorn_format

access_log_format = gunicorn_format()
