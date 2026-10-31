#!/bin/sh
# The suite inside an image, on its interpreter and its installed packages. What only a development checkout has is
# added, the image's own packages kept at their versions: git (the release check's tests), the test tools, and the
# H2 extra's jaydebeapi (its tests mock the JVM). QUERYAPIGATE_BIND is the image's own setting, which one test
# expects unset.
set -e
apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends git >/dev/null
git config --global --add safe.directory /src
pip freeze > /tmp/c.txt
pip install -q -c /tmp/c.txt openapi-spec-validator hypothesis pyyaml jaydebeapi >/tmp/pip.log 2>&1 || cat /tmp/pip.log
python -VV
env -u QUERYAPIGATE_BIND python -m unittest discover -s tests -t . 2>&1 | grep -E "^Ran |^OK|^FAILED|^(FAIL|ERROR):"
