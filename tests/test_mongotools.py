"""Tests for queryapigate.mongotools: JSON-document placeholder substitution and the find-only safety guard -
the non-SQL sibling of sqltools.py's own validation/placeholder tests."""
import unittest

from queryapigate import mongotools
from queryapigate.errors import ApiError


class PlaceholderNamesTests(unittest.TestCase):
    def test_finds_a_top_level_placeholder(self):
        self.assertEqual(mongotools.placeholder_names({'status': ':status'}), ['status'])

    def test_finds_a_nested_placeholder(self):
        doc = {'$and': [{'age': {'$gte': ':min_age'}}, {'active': ':active'}]}
        self.assertEqual(mongotools.placeholder_names(doc), ['min_age', 'active'])

    def test_ignores_a_plain_string_that_is_not_a_bare_placeholder(self):
        self.assertEqual(mongotools.placeholder_names({'name': 'John', 'note': 'see :status field'}), [])

    def test_deduplicates_while_keeping_first_appearance_order(self):
        doc = {'a': ':x', 'b': {'c': ':y'}, 'd': ':x'}
        self.assertEqual(mongotools.placeholder_names(doc), ['x', 'y'])

    def test_no_placeholders_in_a_literal_filter(self):
        self.assertEqual(mongotools.placeholder_names({'status': 'active'}), [])


class FillPlaceholdersTests(unittest.TestCase):
    def test_substitutes_a_string_value(self):
        self.assertEqual(mongotools.fill_placeholders({'status': ':status'}, {'status': 'active'}),
                         {'status': 'active'})

    def test_substitutes_a_non_string_value_without_stringifying_it(self):
        result = mongotools.fill_placeholders({'age': {'$gte': ':min_age'}}, {'min_age': 21})
        self.assertEqual(result['age']['$gte'], 21)
        self.assertIsInstance(result['age']['$gte'], int)

    def test_substitutes_inside_a_list(self):
        result = mongotools.fill_placeholders({'status': {'$in': [':a', ':b']}}, {'a': 'x', 'b': 'y'})
        self.assertEqual(result['status']['$in'], ['x', 'y'])

    def test_missing_value_is_rejected(self):
        with self.assertRaises(ApiError) as caught:
            mongotools.fill_placeholders({'status': ':status'}, {})
        self.assertIn('status', caught.exception.message)

    def test_does_not_mutate_the_original_document(self):
        original = {'status': ':status'}
        mongotools.fill_placeholders(original, {'status': 'active'})
        self.assertEqual(original, {'status': ':status'})

    def test_a_value_not_used_by_the_filter_is_ignored(self):
        # Mirrors sqltools.fill_placeholders(): values may carry more than one placeholder style needs.
        self.assertEqual(mongotools.fill_placeholders({'status': 'active'}, {'unused': 1}), {'status': 'active'})


class ValidateFilterTests(unittest.TestCase):
    def test_accepts_a_plain_filter(self):
        self.assertEqual(mongotools.validate_filter({'status': 'active'}), {'status': 'active'})

    def test_rejects_a_non_dict_filter(self):
        with self.assertRaises(ApiError):
            mongotools.validate_filter(['status', 'active'])

    def test_rejects_where(self):
        with self.assertRaises(ApiError):
            mongotools.validate_filter({'$where': 'this.status == "active"'})

    def test_rejects_where_nested_inside_an_and(self):
        with self.assertRaises(ApiError):
            mongotools.validate_filter({'$and': [{'$where': 'true'}]})

    def test_rejects_function_and_accumulator(self):
        with self.assertRaises(ApiError):
            mongotools.validate_filter({'$expr': {'$function': {}}})
        with self.assertRaises(ApiError):
            mongotools.validate_filter({'$expr': {'$accumulator': {}}})


if __name__ == '__main__':
    unittest.main()
