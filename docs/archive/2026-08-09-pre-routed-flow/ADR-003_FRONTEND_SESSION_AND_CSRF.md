# ADR-003: Fixed frontend session, CSRF, and effective-role boundary

Status: Accepted; disposable Bench acceptance proven, production release still pending

## Decision

The generated frontend is a same-origin asset hosted by the generated Frappe
app. It uses the browser's Frappe session cookie (`credentials: same-origin`)
and never accepts caller-supplied authorization, URL, or arbitrary request
headers. The server endpoint that returns the frontend manifest derives
`effective_role` from the authenticated session; the browser may select only a
declared route, never a role.

State-changing `/api/resource` calls use the `X-Frappe-CSRF-Token` header. The
fixed runtime reads the token from the page's `meta[name="csrf-token"]` element
or the Frappe `globalThis.csrf_token` value. Production clients opt into
`requireCsrf`, which fails before a mutation if no token is available. GET
requests do not require a token.

The server remains authoritative for session validity, effective roles,
permissions, and CSRF validation. Client affordances are only a usability
projection and are not an authorization boundary.

## Consequences and remaining proof

- The local fixed frontend can test the contract with an injected token and
  cannot override credentials or headers through its public resource client.
- Disposable Bench acceptance has proven asset placement, session login, CSRF
  bearing mutations, Manager/User role projection, Guest denial, and cleanup.
  The full Task User browser delete-denial scenario is still a tracked UI-proof
  gap; production release remains out of scope until the release gate is approved.
- Browser test-user credentials remain process-local and must not enter source,
  receipts, or evidence documents.
