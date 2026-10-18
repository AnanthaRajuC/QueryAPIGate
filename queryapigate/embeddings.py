"""Turning a caller's text into a vector, for a saved query's semantic (pgvector) search - the ``embed``
parameter rule (params.py). Experimental (experimental.py): a server with no ``QUERYAPIGATE_EMBEDDING_URL``
never calls out, and a query that needs embedding is refused rather than run unfiltered.

The endpoint is OpenAI-compatible (``POST`` with ``{input, model}`` -> ``{data: [{embedding: [...]}]}``), so it
works with OpenAI, Azure OpenAI, or a local Ollama (``http://localhost:11434/v1/embeddings``). One outbound call
per ``embed`` parameter per request, bounded by ``config.CONNECT_TIMEOUT``; any failure is a clean error that
names the problem, never a silently unfiltered result.
"""
import json
import urllib.request

from . import config
from .errors import ApiError

_MAX_RESPONSE = 1024 * 1024


def embed(text):
    """The embedding vector (a list of floats) for ``text``, from the configured endpoint. Raises ApiError:
    ``embedding_unavailable`` when no endpoint is configured, ``embedding_failed`` when the endpoint errors or
    answers in a shape we can't read."""
    if not config.embeddings_configured():
        raise ApiError('Semantic search is not configured on this server (set QUERYAPIGATE_EMBEDDING_URL and '
                       'QUERYAPIGATE_EMBEDDING_MODEL)', 503, code='embedding_unavailable')
    body = json.dumps({'input': text, 'model': config.embedding_model()}).encode()
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    key = config.embedding_api_key()
    if key:
        headers['Authorization'] = f'Bearer {key}'
    request = urllib.request.Request(config.embedding_url(), data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=config.CONNECT_TIMEOUT) as response:  # noqa: S310 - operator URL
            document = json.loads(response.read(_MAX_RESPONSE))
        vector = document['data'][0]['embedding']
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        raise ApiError(f'The embeddings endpoint did not answer usefully: {error}', 502,
                       code='embedding_failed') from None
    if not isinstance(vector, list) or not vector or not all(isinstance(x, (int, float)) for x in vector):
        raise ApiError('The embeddings endpoint returned no usable vector', 502, code='embedding_failed')
    return [float(x) for x in vector]


def vector_literal(vector):
    """A pgvector text literal, ``'[0.1,0.2,...]'`` - bound as a ``:name`` parameter (the SQL casts it with
    ``::vector``), never as a text ``{name}`` placeholder, whose brackets and commas wouldn't pass sqltools'
    safe-text rule."""
    return '[' + ','.join(repr(float(x)) for x in vector) + ']'
