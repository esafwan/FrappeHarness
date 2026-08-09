# Frappe Harness Pi adapter

This optional skill is an operator-facing description of the typed JSON
boundary. It may help a Pi session form requests for the allowlisted
operations exported by `src/index.ts`.

Operator command labels are `/harness`, `/spec`, `/validate`, `/plan`,
`/status`, and `/approval`; they are UI routing labels, not authorities. The
descriptor's built-in tool denylist (`bash`, `read`, `write`, `edit`, `pi.exec`,
`filesystem`, and `shell`) must remain enforced by the host integration.

Headless requests use the versioned envelope exported by `src/index.ts`.
`session_id`/`session_hash` are display and correlation metadata only, and the
envelope requires `authoritative: false`; the core JSON response remains the
source of truth and must also be usable without a Pi session.

The direct Frappe Harness CLI and core remain authoritative. This package must
not be granted built-in `bash`, filesystem, database, Bench, network-provider,
credential, or approval-recording tools. A Pi UI confirmation is not an
authorization. Send requests to the headless harness protocol and display its
typed response only.

For local control-plane diagnostics, `npm run pi-lifecycle-smoke` starts Pi
offline with built-in tools disabled, checks typed RPC state/session replacement,
and observes extension lifecycle events without sending a model prompt. Its
`compatibility_claim: false` result must remain explicit until a host-version
handshake and supported package/session contract are documented.
