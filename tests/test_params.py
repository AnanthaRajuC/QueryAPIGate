"""Tests for queryapigate.params: checking parameter definitions and enforcing them on request values."""
import unittest

from queryapigate import params, sqltools
from queryapigate.errors import ApiError


def resolve_error(declared, supplied, used=None):
    with unittest.TestCase().assertRaises(ApiError) as caught:
        params.resolve(declared, supplied, used)
    return caught.exception


class DefinitionTests(unittest.TestCase):
    def test_short_and_full_forms(self):
        self.assertEqual(params.read_definition('int')['type'], 'integer')
        full = params.read_definition({'type': 'str', 'enum': ['a', 'b'], 'default': 'a', 'description': 'd'})
        self.assertEqual((full['type'], full['enum'], full['default'], full['description']),
                         ('string', ['a', 'b'], 'a', 'd'))
        self.assertFalse(full['required'])  # a default makes the parameter optional
        self.assertTrue(params.read_definition('int')['required'])

    def test_valid_definitions_are_accepted(self):
        params.parse_definitions({
            'id': 'int', 'n': {'type': 'integer', 'min': 1, 'max': 10}, 'f': {'type': 'float', 'default': 0.5},
            's': {'type': 'string', 'min_length': 1, 'max_length': 9, 'pattern': '[a-z]+'},
            'b': {'type': 'bool', 'required': False}, 'any': {}, 'anything': {'description': 'no type'},
        })

    def test_every_problem_is_reported_together(self):
        with self.assertRaises(ApiError) as caught:
            params.parse_definitions({
                'a': {'type': 'uuid'}, 'b': {'colour': 'red'}, 'c': {'type': 'str', 'min': 1},
                'd': {'type': 'int', 'min': 5, 'max': 1}, 'e': {'type': 'str', 'pattern': '('},
                'f': {'type': 'int', 'default': 'x'}, 'g': {'type': 'int', 'min': 1, 'default': 0},
                'h': {'type': 'str', 'enum': []}, 'i': {'type': 'int', 'enum': ['x']},
                'j': {'required': True, 'default': 1}, 'bad name': 'int', 'k': 12, 'l': {'required': 'yes'},
            })
        errors = caught.exception.extra['errors']
        self.assertEqual(set(errors), set('abcdefghij') | {'bad name', 'k', 'l'})
        self.assertIn('unknown type', errors['a'])
        self.assertIn('unknown rule', errors['b'])
        self.assertIn("'min' only applies to numbers", errors['c'])
        self.assertIn("'min' must not be greater", errors['d'])
        self.assertIn('not a valid regular expression', errors['e'])
        self.assertIn("'default' that must be an integer", errors['f'])
        self.assertIn("'default' that must be at least 1", errors['g'])
        self.assertIn('non-empty list', errors['h'])
        self.assertIn("'enum' value that must be an integer", errors['i'])
        self.assertIn("both 'required' and have a 'default'", errors['j'])
        self.assertEqual(caught.exception.status, 400)

    def test_query_parameters_must_be_an_object(self):
        with self.assertRaises(ApiError):
            params.parse_definitions(['id'])

    def test_unusable_definitions_are_read_leniently_at_run_time(self):
        self.assertIsNone(params.read_definition('weird')['type'])        # unknown type name: untyped
        self.assertEqual(params.read_definition({'colour': 1})['type'], None)
        self.assertEqual(params.read_definitions('nope'), {})
        self.assertEqual(params.resolve({'x': 'weird'}, {'x': '5'}), {'x': '5'})  # old files keep working

    def test_json_schema(self):
        spec = params.read_definition({'type': 'int', 'min': 1, 'max': 9, 'default': 3, 'enum': [1, 3, 9]})
        self.assertEqual(params.json_schema(spec), {'type': 'integer', 'enum': [1, 3, 9], 'minimum': 1,
                                                    'maximum': 9, 'default': 3})
        self.assertEqual(params.json_schema(params.read_definition({})), {'type': 'string'})
        text = params.json_schema(params.read_definition({'type': 'str', 'min_length': 2, 'pattern': 'a+'}))
        self.assertEqual(text, {'type': 'string', 'minLength': 2, 'pattern': 'a+'})


class ResolveTests(unittest.TestCase):
    def test_strings_are_converted_to_the_declared_type(self):
        declared = {'id': 'int', 'ratio': 'float', 'on': 'bool', 'name': 'str'}
        self.assertEqual(params.resolve(declared, {'id': '3', 'ratio': '0.5', 'on': 'yes', 'name': '007'}),
                         {'id': 3, 'ratio': 0.5, 'on': True, 'name': '007'})

    def test_native_json_values_are_type_checked(self):
        declared = {'id': 'int', 'on': 'bool', 'name': 'str', 'x': 'float'}
        self.assertEqual(params.resolve(declared, {'id': 3, 'on': False, 'name': 'a', 'x': 2}),
                         {'id': 3, 'on': False, 'name': 'a', 'x': 2})
        errors = resolve_error(declared, {'id': True, 'on': 1, 'name': 5, 'x': [1]}).extra['errors']
        self.assertEqual(sorted(errors), ['id', 'name', 'on', 'x'])  # a bool is not an integer, a number not text

    def test_integers_must_fit_in_64_bits(self):
        # No supported driver can bind a wider integer - it must fail here as a 400, not in the driver as a 500
        declared = {'id': 'int'}
        for ok in ('9223372036854775807', '-9223372036854775808', 2 ** 63 - 1):
            self.assertEqual(params.resolve(declared, {'id': ok}), {'id': int(ok)})
        for bad in ('9223372036854775808', '-9223372036854775809', 2 ** 63, '9' * 30):
            self.assertEqual(resolve_error(declared, {'id': bad}).extra['errors'], {'id': 'must be a 64-bit integer'})

    def test_defaults_and_optional_parameters(self):
        declared = {'rating': {'type': 'str', 'default': 'PG'}, 'q': {'type': 'str', 'required': False}}
        self.assertEqual(params.resolve(declared, {}), {'rating': 'PG', 'q': None})
        self.assertEqual(params.resolve(declared, {'rating': 'R', 'q': 'x'}), {'rating': 'R', 'q': 'x'})

    def test_required_missing_and_null(self):
        self.assertEqual(resolve_error({'id': 'int'}, {}).extra['errors'], {'id': 'is required'})
        self.assertEqual(resolve_error({'id': 'int'}, {'id': None}).extra['errors'], {'id': 'must not be null'})
        self.assertEqual(params.resolve({'id': {'type': 'int', 'required': False}}, {'id': None}), {'id': None})

    def test_enum_bounds_length_and_pattern(self):
        declared = {'r': {'type': 'str', 'enum': ['G', 'PG']}, 'n': {'type': 'int', 'min': 1, 'max': 5},
                    's': {'type': 'str', 'min_length': 2, 'max_length': 4, 'pattern': '[a-z]+'}}
        good = {'r': 'G', 'n': '5', 's': 'abc'}
        self.assertEqual(params.resolve(declared, good), {'r': 'G', 'n': 5, 's': 'abc'})
        errors = resolve_error(declared, {'r': 'X', 'n': '9', 's': 'A'}).extra['errors']
        self.assertEqual(errors['r'], 'must be one of: G, PG')
        self.assertEqual(errors['n'], 'must be at most 5')
        self.assertIn('at least 2 characters', errors['s'])
        self.assertEqual(resolve_error(declared, {**good, 'n': '0'}).extra['errors'], {'n': 'must be at least 1'})
        self.assertIn('pattern', resolve_error(declared, {**good, 's': 'ab1'}).extra['errors']['s'])
        self.assertIn('at most 4', resolve_error(declared, {**good, 's': 'abcde'}).extra['errors']['s'])

    def test_all_violations_are_reported_at_once_in_the_message(self):
        error = resolve_error({'a': 'int', 'b': 'int'}, {'a': 'x'})
        self.assertEqual(sorted(error.extra['errors']), ['a', 'b'])
        self.assertEqual(error.message, 'Invalid parameters: a must be an integer; b is required')

    def test_undeclared_values_pass_through_and_unused_declarations_are_not_enforced(self):
        declared = {'id': 'int', 'unused': 'int'}
        self.assertEqual(params.resolve(declared, {'id': '1', 'other': 'x'}, used={'id', 'other'}),
                         {'id': 1, 'other': 'x'})

    def test_non_finite_and_odd_numbers_are_rejected(self):
        for bad in ('nan', 'inf', 'abc', ''):
            self.assertIn('id', resolve_error({'id': 'float'}, {'id': bad}).extra['errors'], bad)
        self.assertIn('id', resolve_error({'id': 'int'}, {'id': '1.5'}).extra['errors'])

    def test_booleans(self):
        for text, expected in (('true', True), ('1', True), ('YES', True), ('false', False), ('0', False)):
            self.assertIs(params.resolve({'b': 'bool'}, {'b': text})['b'], expected)
        self.assertIn('b', resolve_error({'b': 'bool'}, {'b': 'maybe'}).extra['errors'])

    def test_placeholder_names_lists_bound_then_text_placeholders_once(self):
        self.assertEqual(sqltools.placeholder_names("SELECT :a, ':x', :b, :a FROM t WHERE c = '{c}' AND d = {d}"),
                         ['a', 'b', 'c', 'd'])


if __name__ == '__main__':
    unittest.main()
