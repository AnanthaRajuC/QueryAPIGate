# ADR 0025: Semantic retrieval - an `embed` parameter, not a vector store

- **Status:** Accepted (2026-10-18)
- **Backlog:** making MCP tools stronger for agents. Experimental (`experimental.py`), Postgres/`pgvector` only.

## Context

Agents increasingly want to search data *by meaning* - "find comfortable running shoes", "tickets like this one" -
not by exact match. That is a vector search: embed the query text into a vector and rank rows by distance to a
stored embedding. The databases QueryAPIGate fronts can already do the ranking (`pgvector`'s `<=>`); what they can't
do is turn the caller's words into a vector, which needs an embedding model.

Two shapes were possible:

1. **Become a retrieval engine** - build or bundle vector indexing, an embedding model, hybrid search. This is a
   different product (it is what a vector database or a tool like SereneDB is), and it would pull QueryAPIGate from a
   governance gateway into the data plane.
2. **Add one binding** - let a saved query declare that a text parameter should be *embedded* before it is bound,
   and leave the storage, indexing and ranking to the database the operator already runs.

The gateway's whole value is that governance (masking, row-level `from_claim`, rate limits, audit) is enforced on
every call. A semantic query should be no exception - an agent should be able to search by meaning and still see
only what it is allowed to.

## Decision

Add a parameter rule **`embed`**. A text parameter marked `"embed": true` takes words from the caller; the server
embeds them and binds the resulting vector, exactly where the SQL casts it (`CAST(:q AS vector)`). The caller - human
or agent - only ever supplies text and never sees or chooses the vector, the same contract `from_claim` already has
for identity values.

- **Embedding happens server-side**, once per `embed` parameter per request, against an operator-configured
  OpenAI-compatible endpoint (`QUERYAPIGATE_EMBEDDING_URL`/`_MODEL`/`_API_KEY`) - OpenAI, Azure OpenAI, or a local
  Ollama. The binding is `embeddings.py`; the outbound call reuses the project's `urllib` pattern, no new dependency.
- **The vector is bound, never interpolated.** It is a `:name` bound parameter carrying a `pgvector` literal, not a
  `{name}` text placeholder - whose brackets and commas `sqltools` would reject anyway.
- **Fail closed.** A query with an `embed` parameter is refused with `embedding_unavailable` when no endpoint is
  configured, and with `unsupported_operation` on a non-Postgres connection - it is never run with the raw text
  bound where a vector belongs, which would error or silently mis-rank.
- **Governance is unchanged.** `embed` only transforms one bind value; masking, `from_claim`, `require_claim_binding`,
  rate limits and the audit log all apply to a semantic query as to any other.
- **Scope.** Postgres/`pgvector` only for now; the literal format is pgvector's. Other vector stores (DuckDB VSS,
  sqlite-vss) are a later extension, not a removal.

## Consequences

- An operator with `pgvector` and an embeddings endpoint can expose semantic search as a governed REST endpoint and
  MCP tool with one parameter rule - no new service, no new tool code, no data leaving the governed boundary beyond
  the query text going to the embedding model the operator chose.
- QueryAPIGate stays a control plane: it does not store, index, or rank vectors - the database does.
- It is experimental: the settings and the `embed` rule may change as they meet more vector stores, per the
  compatibility promise. It graduates under `experimental.GRADUATION` (tested against a real `pgvector` in CI, a
  security review of the outbound call, a stable shape).
