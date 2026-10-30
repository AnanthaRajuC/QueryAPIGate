"""gunicorn's settings for QueryAPIGate (`gunicorn -c python:queryapigate.gunicorn_conf ...`): the request threads
(QUERYAPIGATE_THREADS, default 8), whether there is an access log at all (QUERYAPIGATE_ACCESS_LOG, on by default, to
stdout), and its format - paths without their query strings, unless QUERYAPIGATE_ACCESS_LOG_QUERY=1 (accesslog.py, ADR
0016). The image uses it; so can any gunicorn command running QueryAPIGate. A value given on gunicorn's command line,
or in GUNICORN_CMD_ARGS, overrides the one here, as gunicorn reads a config file first.

Where it listens: QUERYAPIGATE_BIND, which the image sets to 0.0.0.0:5000 and the Helm chart to 127.0.0.1:5001 behind
the data plane; unset, gunicorn's own default.

Behind the Rust data plane (QUERYAPIGATE_DATAPLANE_SECRET set, ADR 0030) every request reaches gunicorn over loopback,
which gunicorn trusts by default to set the scheme (X-Forwarded-Proto, -Ssl, -Protocol) and SCRIPT_NAME from headers -
a client's own, passed on. So it trusts no peer's then, unless FORWARDED_ALLOW_IPS says otherwise: Python reads the
client's forwarding headers through ProxyFix, as QUERYAPIGATE_TRUST_PROXY says, as it does without the data plane."""
# Every name at the top of this file is read as a gunicorn setting of that name, and `config` is one (the config file's
# own path): imported under it, gunicorn refuses to start. Hence the underscores.
import os as _os

from queryapigate import config as _settings
from queryapigate.accesslog import gunicorn_format as _gunicorn_format

threads = _settings.threads()
accesslog = '-' if _settings.access_log() else None  # None: no access log - not one written to /dev/null
access_log_format = _gunicorn_format()
if _settings.bind():
    bind = _settings.bind()
if _settings.dataplane_secret() and 'FORWARDED_ALLOW_IPS' not in _os.environ:
    forwarded_allow_ips = ''
