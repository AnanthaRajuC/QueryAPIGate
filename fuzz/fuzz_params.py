"""params.resolve binds or refuses with an ApiError, and a declared parameter is never bound to a document (see
fuzz/README.md)."""
import sys

import atheris

with atheris.instrument_imports():
    from queryapigate import params
    from queryapigate.errors import ApiError

TYPES = ('int', 'float', 'str', 'bool', 'string', 'number', 'integer', 'boolean', 'nonsense')


def supplied_value(fdp):
    kind = fdp.ConsumeIntInRange(0, 5)
    return (None, fdp.ConsumeBool(), fdp.ConsumeInt(8), fdp.ConsumeFloat(), fdp.ConsumeUnicodeNoSurrogates(16),
            {'$ne': None})[kind]


def rule(fdp):
    if fdp.ConsumeBool():
        return fdp.PickValueInList(list(TYPES))
    spec = {'type': fdp.PickValueInList(list(TYPES))}
    for key in ('required', 'default', 'min', 'max', 'min_length', 'max_length', 'pattern', 'enum'):
        if fdp.ConsumeIntInRange(0, 3) == 0:
            if key == 'required':
                spec[key] = fdp.ConsumeBool()
            elif key == 'pattern':
                spec[key] = fdp.ConsumeUnicodeNoSurrogates(8)
            elif key == 'enum':
                spec[key] = [supplied_value(fdp) for _ in range(fdp.ConsumeIntInRange(1, 3))]
            else:
                spec[key] = supplied_value(fdp)
    return spec


def TestOneInput(data):
    fdp = atheris.FuzzedDataProvider(data)
    declared = {name: rule(fdp) for name in ('a', 'b') if fdp.ConsumeBool()}
    supplied = {name: supplied_value(fdp) for name in ('a', 'b', 'c') if fdp.ConsumeBool()}
    try:
        params.parse_definitions(declared)  # only rules a query could be saved with ever reach resolve()
    except ApiError:
        return
    try:
        values = params.resolve(declared, supplied)
    except ApiError:
        return
    assert isinstance(values, dict), values
    for name in declared:
        assert not isinstance(values.get(name), dict), (declared, supplied, values)


if __name__ == '__main__':
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
