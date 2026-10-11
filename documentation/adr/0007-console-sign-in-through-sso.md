# ADR 0007: Console sign-in through the organisation's identity provider (OpenID Connect)

- **Status:** Proposed
- **Date:** 2026-10-11
- **Backlog:** #84 Phase 2 (SSO for the Console). Builds on [ADR 0003](0003-named-administrators.md) (named
  administrators, roles, tokens) and shares its identity-provider plumbing with
  [ADR 0006](0006-ai-agents.md) (OAuth for the MCP server).

## Context

Since 0.16 an administrator is a named account with a role, signing in to the Console by pasting a personal admin
token (`qagadm_...`) or the shared break-glass key. That works, but:

- **people hold long-lived secrets.** A token lasts 90 days by default, sits in a password manager or a shell history,
  and works from anywhere until someone revokes it;
- **offboarding is a second job.** Someone leaving is removed at the identity provider; their QueryAPIGate token keeps
  working until an owner remembers it;
- **MFA, device and network policy don't apply.** The organisation's sign-in policies stop at its identity provider;
  a pasted token skips them;
- **roles drift.** Who is an admin is decided by hand in QueryAPIGate, not by the groups the organisation already
  manages.

AI agents now sign in through the identity provider (ADR 0006); the people who govern what those agents may do still
paste tokens. ADR 0003 left this for Phase 2: "an SSO sign-in creates or finds an administrator and maps group claims
to these same roles; the Console then gets a session cookie instead of a pasted token."

What 0.18 already has: OpenID Connect discovery with issuer checking and HTTPS-only fetching, key retrieval with
rotation, ID-token-style verification (`jwtauth.py`); a store-backed `server_secrets` table; administrators with
roles and `via` in every audit entry.

## Decision

### 1. OpenID Connect, authorization code flow with PKCE, done by the server

The QueryAPIGate server is the OIDC client - a **confidential client** with a client secret, registered once by an
administrator at the provider. The browser never sees a token:

1. The Console's **Sign in with <provider>** goes to `GET /auth/sso/login`. The server creates a pending sign-in -
   `state`, `nonce` and a PKCE verifier, stored server-side for 10 minutes - and redirects to the provider's
   authorization endpoint (`response_type=code`, `scope=openid email profile`, PKCE `S256`).
2. The provider signs the person in - its MFA, its policies - and redirects to `GET /auth/sso/callback?code&state`.
3. The server finds the pending sign-in by `state` (used once, then deleted), exchanges the code at the token
   endpoint with the client secret and the PKCE verifier, and verifies the **ID token**: signature against the
   provider's keys, `iss`, `aud` = the client ID, `nonce`, `exp`, `iat`.
4. It maps the person to an administrator (section 2), starts a session (section 3), and redirects to the Console.

Configuration:

| Setting | Meaning |
|---|---|
| `QUERYAPIGATE_SSO_ISSUER` | The provider's issuer URL; endpoints and keys come from its metadata |
| `QUERYAPIGATE_SSO_CLIENT_ID` / `QUERYAPIGATE_SSO_CLIENT_SECRET` | The client registered at the provider (the secret as `${VAR}` or in the environment, never in the store) |
| `QUERYAPIGATE_SSO_REDIRECT_URL` | The callback as the provider calls it: `https://data.example.com/auth/sso/callback` |
| `QUERYAPIGATE_SSO_ROLES` | Groups to roles: `owner=qag-owners,admin=qag-admins,developer=qag-devs,auditor=qag-audit` |
| `QUERYAPIGATE_SSO_GROUPS_CLAIM` | The claim holding the person's groups (default `groups`) |
| `QUERYAPIGATE_SSO_SESSION_HOURS` | How long a session lasts (default 8) |

All required ones together or none, `https://` only (`localhost` excepted), checked at startup - as for the MCP
server's OAuth settings. The MCP server and the Console may name the same provider; they are separate clients there,
with separate audiences, and neither accepts the other's tokens.

**Rejected: the browser doing the flow (a public client) and holding tokens** - tokens in JavaScript are exposed to
any script on the page, and the Console would need refresh-token handling. A server-side flow with an HttpOnly
session cookie keeps every token off the page. **Rejected: SAML** - every provider in scope speaks OIDC, and SAML's
XML signature handling is a well-known source of vulnerabilities.

### 2. Who the person is, and what role they get

- **The link is the provider's subject, never the email.** An administrator signed in through SSO is bound to
  (`iss`, `sub`) on first sign-in. An email can be reassigned or unverified; a `sub` is stable and unique per
  provider.
- **The role comes from the groups, at every sign-in.** The groups claim is matched against `QUERYAPIGATE_SSO_ROLES`;
  the most capable matching role wins (owner > admin > developer > auditor). **No matching group: no sign-in** - the
  person is shown why, and nothing is created. Someone moved out of `qag-admins` at the provider has their new role,
  or none, from their next sign-in.
- **First sign-in creates the administrator** (just-in-time): its name from `preferred_username`, or the email's
  local part, made to fit administrator names (`alice.ito`), with a number added if taken; `email` from a verified
  email claim. Audited as `create_admin` with `via: sso`.
- **An SSO administrator's role is the provider's.** The Console shows it as managed by SSO and doesn't offer to
  change it; deactivating one in QueryAPIGate still works, and ends their sessions at once.
- **Existing administrators can be linked**: an owner marks `alice` as "signs in through SSO"; her next SSO sign-in
  with a verified email matching hers binds her `sub` - once, audited - so teams moving from tokens keep their
  administrators and their history.
- **Personal admin tokens stay**, for automation and as a fallback; an owner can turn off token sign-in to the Console
  for SSO administrators (`QUERYAPIGATE_SSO_ONLY=yes`) so people can't route around the provider's policies. The
  break-glass key keeps working as before: audited and alerted.

**Providers without group claims** (Google Workspace's ID tokens carry none): `QUERYAPIGATE_SSO_DEFAULT_ROLE` gives
everyone who signs in one role - but only together with `QUERYAPIGATE_SSO_ALLOWED_DOMAINS`, checked against a
verified email, since otherwise any Google account in the world would get it. Startup refuses one without the other.

### 3. The session: a cookie that never authenticates a request on its own

- **Server-side sessions** in a new `admin_sessions` table (schema 10): an ID stored as a hash, the administrator,
  created, expires (`QUERYAPIGATE_SSO_SESSION_HOURS`), last seen. Every instance sees a sign-out or a deactivation on
  the next request, as with tokens. Pending sign-ins (state, nonce, PKCE verifier) are rows in `sso_logins`, kept 10
  minutes.
- **The cookie:** `__Host-qag_session` - `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, no `Domain`. Its value is
  random (32 bytes), never a token from the provider.
- **CSRF: every cookie-authenticated request must also carry `X-QAG-CSRF`**, the session's own CSRF token, which the
  Console reads from `GET /auth/session` and sends on every call - reads included. A request with the cookie and
  without the header is treated as carrying no credential. So a link, a form or an image on another site can never
  act as the administrator, whatever the method - including a GET of a write-enabled saved query. `SameSite=Strict`
  and CORS (which never allows credentials) are two more locks on the same door.
- **What the cookie reaches:** what the administrator's token would - the Management API and the data-plane
  endpoints the Console uses - with the same role checks. `/mcp` and `/events` ignore it: agents and streams keep
  their own credentials.
- **Sign-out** (`POST /auth/sso/logout`, CSRF-checked) deletes the session; where the provider publishes an
  `end_session_endpoint`, the Console offers signing out there too.
- **Deprovisioning lag:** a session lasts at most `QUERYAPIGATE_SSO_SESSION_HOURS`; someone removed at the provider
  keeps an open session until then (back-channel logout is out of scope for now). Deactivating them in QueryAPIGate
  ends it immediately. The guide says so.

### 4. What's audited

Every sign-in, refused sign-in (no matching group, a domain not allowed, a failed ID-token check - without the token)
and sign-out is an audit entry; administrator actions in a session carry `via: sso`. A new alert, `sso_unreachable`,
when the provider's metadata or keys can't be fetched - sign-in is then down, and the break-glass key or a token is
how owners get in.

## Consequences

- **New:** schema 10 (`admin_sessions`, `sso_logins`, and an SSO link on administrators); `/auth/sso/login`,
  `/auth/sso/callback`, `/auth/sso/logout`, `/auth/session`; eight settings; `via: sso`; the Console's sign-in screen
  and SSO-managed administrators; one alert. Experimental in its first release.
- **Not breaking:** tokens and the break-glass key work as before; nothing changes until SSO is configured.
- **New attack surface, treated as such:** the callback (state, nonce, PKCE, single use, ID-token checks), the
  session cookie and CSRF, open redirects (the post-sign-in redirect only ever goes to a Console path), and the
  role mapping. The release gets a focused security review, as 0.16 and 0.17 did.
- **The provider becomes the gate to administration** - who controls the `qag-owners` group controls QueryAPIGate.
  The threat model says so.
- **Not in scope:** SAML, SCIM provisioning, back-channel logout, custom roles, and SSO for data callers (they have
  JWTs and the MCP server's OAuth).

## Delivery

About a week and a half, in slices:

1. Shared OIDC metadata (`oidc.py`, from 0.18's MCP discovery): endpoints, keys, `end_session_endpoint`; settings and
   their startup checks.
2. The flow: login, callback, ID-token verification, schema 10, sessions, the cookie and CSRF, sign-out; tests against
   a stand-in provider and a real Keycloak (the authorization code flow driven end to end), including replayed state,
   wrong nonce, a token for another client, and cross-site requests.
3. Administrators from SSO: just-in-time creation, group-to-role mapping on every sign-in, linking existing ones,
   `QUERYAPIGATE_SSO_ONLY`, default role with allowed domains.
4. Console: the sign-in screen (SSO first, token as the fallback), "via SSO" in the header, SSO-managed administrators
   on the Administrators screen, sign-out.
5. A guide (Keycloak, with notes for Entra ID, Okta and Google Workspace), the threat model, the `sso_unreachable`
   alert, the changelog - and a security review before release.
