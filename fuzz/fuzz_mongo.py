"""Once a filter, projection, sort and parameters are all accepted, no server-side JavaScript operator is anywhere in
what would be sent to MongoDB, and no parameter became a document (see fuzz/README.md) - the guarantees 0.34.2 found
broken, in the projection and through a parameter."""
import sys

import atheris

with atheris.instrument_imports():
    from queryapigate import mongotools
    from queryapigate.errors import ApiError

FORBIDDEN = {'$where', '$function', '$accumulator'}
KEYS = ('$where', '$function', '$accumulator', '$expr', '$ne', '$gt', '$in', '$and', '$or', '$regex', 'name', 'x',
        ':p', ':q', 'body', 'args', 'lang')


def value(fdp, depth):
    kind = fdp.ConsumeIntInRange(0, 7 if depth < 4 else 4)
    if kind == 0:
        return None
    if kind == 1:
        return fdp.ConsumeBool()
    if kind == 2:
        return fdp.ConsumeInt(4)
    if kind == 3:
        return fdp.PickValueInList([':p', ':q', ':missing', 'p', '$where'])
    if kind == 4:
        return fdp.ConsumeUnicodeNoSurrogates(12)
    if kind in (5, 6):
        return document(fdp, depth + 1)
    return [value(fdp, depth + 1) for _ in range(fdp.ConsumeIntInRange(0, 3))]


def document(fdp, depth=0):
    def key():
        return fdp.PickValueInList(list(KEYS)) if fdp.ConsumeBool() else fdp.ConsumeUnicodeNoSurrogates(8)
    return {key(): value(fdp, depth) for _ in range(fdp.ConsumeIntInRange(0, 4))}


def forbidden_anywhere(node):
    if isinstance(node, dict):
        return any(key in FORBIDDEN or forbidden_anywhere(item) for key, item in node.items())
    if isinstance(node, list):
        return any(forbidden_anywhere(item) for item in node)
    return False


def TestOneInput(data):
    fdp = atheris.FuzzedDataProvider(data)
    filter_doc, projection, sort = document(fdp), document(fdp) if fdp.ConsumeBool() else None, None
    if fdp.ConsumeBool():
        sort = document(fdp)
    params = {name: value(fdp, 0) for name in ('p', 'q') if fdp.ConsumeBool()}
    try:
        mongotools.validate_filter(filter_doc)
        mongotools.validate_options(projection, sort)
        sent = mongotools.fill_placeholders(filter_doc, params)
    except ApiError:
        return
    assert not forbidden_anywhere([sent, projection, sort]), (sent, projection, sort)
    for name in mongotools.placeholder_names(filter_doc):
        assert not isinstance(params[name], dict), params[name]


if __name__ == '__main__':
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
