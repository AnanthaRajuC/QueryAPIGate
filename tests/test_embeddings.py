"""Semantic-retrieval embeddings (experimental): the embeddings.py helper and app.bind_embeddings(), both
tested without a live endpoint or a real Postgres - the outbound call and the connection are mocked."""
import json
import unittest
from unittest import mock

from queryapigate import app as app_module
from queryapigate import config, embeddings, store
from queryapigate.errors import ApiError


def _ok_response(vector):
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps({'data': [{'embedding': vector}]}).encode()
    return cm


class VectorLiteralTests(unittest.TestCase):
    def test_pgvector_literal(self):
        self.assertEqual(embeddings.vector_literal([1, 2, 3]), '[1.0,2.0,3.0]')


class EmbedTests(unittest.TestCase):
    def test_refused_when_not_configured(self):
        with mock.patch.object(config, 'embeddings_configured', return_value=False):
            with self.assertRaises(ApiError) as caught:
                embeddings.embed('hello')
        self.assertEqual(caught.exception.code, 'embedding_unavailable')

    def test_calls_the_endpoint_and_returns_the_vector(self):
        with mock.patch.object(config, 'embeddings_configured', return_value=True), \
             mock.patch.object(config, 'embedding_url', return_value='http://localhost:11434/v1/embeddings'), \
             mock.patch.object(config, 'embedding_model', return_value='nomic-embed-text'), \
             mock.patch.object(config, 'embedding_api_key', return_value=None), \
             mock.patch('urllib.request.urlopen', return_value=_ok_response([0.1, 0.2, 0.3])):
            self.assertEqual(embeddings.embed('hello'), [0.1, 0.2, 0.3])

    def test_endpoint_failure_is_embedding_failed(self):
        with mock.patch.object(config, 'embeddings_configured', return_value=True), \
             mock.patch.object(config, 'embedding_url', return_value='http://x/v1/embeddings'), \
             mock.patch.object(config, 'embedding_model', return_value='m'), \
             mock.patch.object(config, 'embedding_api_key', return_value=None), \
             mock.patch('urllib.request.urlopen', side_effect=OSError('connection refused')):
            with self.assertRaises(ApiError) as caught:
                embeddings.embed('hello')
        self.assertEqual(caught.exception.code, 'embedding_failed')


class BindEmbeddingsTests(unittest.TestCase):
    SAVED = {'connection_name': 'c', 'query_parameters': {'q': {'type': 'str', 'embed': True}}}

    def test_no_embed_parameters_leaves_values_untouched(self):
        self.assertEqual(app_module.bind_embeddings({'query_parameters': {}}, {'x': 1}, 'c'), {'x': 1})

    def test_embed_needs_a_postgres_connection(self):
        with mock.patch.object(store, 'get_connection', return_value={'db': 'sqlite'}):
            with self.assertRaises(ApiError) as caught:
                app_module.bind_embeddings(self.SAVED, {'q': 'running shoes'}, 'c')
        self.assertEqual(caught.exception.code, 'unsupported_operation')

    def test_embed_replaces_text_with_a_vector_literal(self):
        with mock.patch.object(store, 'get_connection', return_value={'db': 'postgres'}), \
             mock.patch.object(app_module.embeddings, 'embed', return_value=[0.1, 0.2]):
            out = app_module.bind_embeddings(self.SAVED, {'q': 'running shoes'}, 'c')
        self.assertEqual(out['q'], '[0.1,0.2]')


if __name__ == '__main__':
    unittest.main()
