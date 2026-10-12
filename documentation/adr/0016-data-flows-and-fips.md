# ADR 0016: Where data goes, and FIPS-validated cryptography

- **Status:** Proposed
- **Date:** 2026-10-12
- **Backlog:** #101 (compliance documentation: data flows, and FIPS-validated cryptography).

## Context

Every organisation that buys or approves software sends a security questionnaire, and two of its sections can't be
answered from QueryAPIGate's documentation today:

- **Where does personal data go, and for how long?** GDPR's records of processing (Article 30), SOC 2's data
  classification, and every vendor review ask it. The answers exist, but scattered: run history keeps an ad-hoc
  query's parameter *names*, never values (`history.record_adhoc`); its SQL as text, a hash or not at all
  (`QUERYAPIGATE_HISTORY_ADHOC_SQL`); traces never hold values (ADR 0010); agents' write requests keep their
  arguments for 30 days (ADR 0008). Each is documented where its feature is, and nowhere all together.
- **Is the cryptography FIPS 140 validated?** US federal agencies and their contractors (FedRAMP), and many banks and
  healthcare organisations, can only run software whose cryptography goes through a validated module. Nobody has
  checked whether QueryAPIGate runs on one.

Looking for the answers turned up one place they're wrong today:

- **The image's access log writes query-string values.** gunicorn's default line is the full request -
  `GET /q/orders?customer_email=alice@example.com` - so every parameter sent in the URL is in the container's log,
  kept as long as the log platform keeps it. QueryAPIGate's own log, run history and traces all leave values out on
  purpose; this one line undoes that.

What's already true about the cryptography:

- **Only FIPS-approved algorithms are used.** Every hash is SHA-256 or HMAC-SHA256 (key and token hashes, the audit
  chain, masking's `hash`, the response cache's keys); there's no MD5, SHA-1 or BLAKE2 anywhere. Fernet is AES-128 in
  CBC mode with HMAC-SHA256. Secrets come from the operating system's random source (`secrets`). JWTs are verified
  with RS256, ES256 or HS256 - each approved - through `cryptography`. TLS is Python's `ssl`.
- **But validation is a property of the module, not the algorithm.** Python's `hashlib`, `hmac` and `ssl` use
  whichever OpenSSL the interpreter links to; the `cryptography` wheel from PyPI carries its own OpenSSL, which isn't
  a validated module; the PostgreSQL and MySQL drivers' binary wheels carry theirs too.

## Decision

### 1. One page says where data goes

`documentation/DATA_PROTECTION.md`: every place QueryAPIGate keeps or sends data, in one table - what is kept, where,
for how long and by which setting, who can read it, and what is never kept.

| Kept in | Holds | For | Read by |
|---|---|---|---|
| The store: connections, queries, keys, roles, ... | Configuration; passwords encrypted or as references | Until deleted | Administrators |
| Run history | Who ran what, when, how long, how many rows; an ad-hoc run's SQL as set, its parameter names - never values | `QUERYAPIGATE_HISTORY_LIMIT` / `_RETENTION_DAYS` | `observe` |
| The audit log | Every administrative change, by whom | `QUERYAPIGATE_AUDIT_LOG_LIMIT`; the export file and SIEM as you keep them | `observe` |
| Write requests | An agent's write: its arguments, and who approved it | 30 days | `writes.approve` |
| The response cache | Results of cached queries | The query's `cache_ttl` | The callers it was cached for |
| Logs, traces, metrics | Requests by path - not values; callers' addresses and names | Your platform's retention | Your operators |
| ... | | | |

The table is the page's spine; each row links to the setting that bounds it. Kept honest by a test: **every table in
the store's schema, every setting that bounds retention, and every place data leaves the process (logs, traces, the
SIEM stream, exports, the cache) must appear on the page** - a new table or setting without a row fails CI, as a new
error code without a line in the catalogue does.

The page also answers the questions a record of processing needs: the categories of personal data (callers' names,
email addresses from SSO and JWTs, IP addresses, whatever the connected databases hold), why each is kept, and what a
deployer controls.

### 2. The access log leaves values out

The image's gunicorn logs the request's **path, without its query string**, by default - the same as QueryAPIGate's
own log - along with method, status, size, time and the request id. `QUERYAPIGATE_ACCESS_LOG_QUERY=1` puts the query
string back, for a deployment that wants it and has said so. The referrer, which can carry another URL's query
string, is left out too.

### 3. Forgetting a person

A caller who is a person - `jwt:alice@example.com` - is named in run history, usage counters and the response
cache's ownership. `queryapigate history forget CALLER` (and `DELETE /api/v1/history?caller=...`, `history.forget`,
owners only) deletes that caller's runs and usage counters, and records that it did - in the audit log, with the
caller's SHA-256, not their name, so the record of the erasure doesn't keep what was erased.

The audit log isn't edited: it's hash-chained (ADR 0012), and its entries name administrators acting in their role -
kept under legitimate interest, and its retention is the deployer's to set. The page says so, and says what to tell a
person who asks.

### 4. FIPS: approved algorithms, a validated module, tested

- **QueryAPIGate itself uses only approved algorithms** - as above - and the page lists every use, by what it
  protects. A test fails if the code gains a call to a non-approved one (`md5`, `sha1`, `blake2`, `random` for a
  secret).
- **Validation comes from the platform.** The page says how to run on a validated module: a distribution whose
  OpenSSL is validated and in FIPS mode (Red Hat Enterprise Linux 9, Ubuntu Pro FIPS), with Python and `cryptography`
  built against it - the distribution's own packages, not PyPI's wheels - and the database drivers likewise
  (`psycopg2` from source, not `psycopg2-binary`). What's outside QueryAPIGate's control is said plainly: DuckDB's
  own TLS for remote files, a JDBC driver's Java runtime.
- **Tested in CI, under OpenSSL 3's FIPS provider.** A job runs the suite in a Red Hat UBI 9 container with OpenSSL
  configured to the FIPS provider only, Python and `cryptography` from the distribution's packages - and first checks
  that FIPS is really in force (MD5 refused, `cryptography` reporting FIPS). Anything that breaks there is a bug.
- **Not in 0.29: a FIPS image.** Building and publishing a UBI-based image is a separate decision, made when someone
  asks for one; the page and the CI job are what it would rest on.

### 5. Stability

The page is documentation, kept true by tests. `QUERYAPIGATE_ACCESS_LOG_QUERY` and `history forget` are stable from
0.29: they are small, and what they promise - values left out, a person's runs deleted - is not something to change.

## Consequences

- **New:** `documentation/DATA_PROTECTION.md` with its consistency test; the image's access log format and
  `QUERYAPIGATE_ACCESS_LOG_QUERY`; `queryapigate history forget` and `DELETE /api/v1/history?caller=`, with the
  `history.forget` capability; the approved-algorithms test; the FIPS CI job.
- **Upgrading:** the image's access log no longer has query strings. A log pipeline that parsed them gets them back
  with `QUERYAPIGATE_ACCESS_LOG_QUERY=1`. No schema change.
- **What it doesn't do:**
  - It doesn't make QueryAPIGate FIPS validated - no application is; it runs on a validated module, and the page
    says how and what to check.
  - It doesn't certify anything (SOC 2, ISO 27001): those audit an organisation, not a product. The page is what an
    auditor of yours reads.
  - It doesn't erase what the connected databases hold, or the audit log's entries about administrators.

## Delivery

About a week, in three slices:

1. The data-protection page and its consistency test; the access log without values; `history forget`.
2. FIPS: the approved-algorithms test, the page's section, and the CI job under UBI 9's FIPS provider - and whatever
   it finds.
3. A guide answering a security questionnaire's data and cryptography sections from the page, the security review,
   the threat model, and 0.29.0 prep.
