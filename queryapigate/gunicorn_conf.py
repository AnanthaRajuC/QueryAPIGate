"""gunicorn's settings for QueryAPIGate (`gunicorn -c python:queryapigate.gunicorn_conf ...`): the request threads
(QUERYAPIGATE_THREADS, default 8), whether there is an access log at all (QUERYAPIGATE_ACCESS_LOG, on by default, to
stdout), and its format - paths without their query strings, unless QUERYAPIGATE_ACCESS_LOG_QUERY=1 (accesslog.py, ADR
0016). The image uses it; so can any gunicorn command running QueryAPIGate. A value given on gunicorn's command line,
or in GUNICORN_CMD_ARGS, overrides the one here, as gunicorn reads a config file first."""
# Every name at the top of this file is read as a gunicorn setting of that name, and `config` is one (the config file's
# own path): imported under it, gunicorn refuses to start. Hence the underscores.
from queryapigate import config as _settings
from queryapigate.accesslog import gunicorn_format as _gunicorn_format

threads = _settings.threads()
accesslog = '-' if _settings.access_log() else None  # None: no access log - not one written to /dev/null
access_log_format = _gunicorn_format()
