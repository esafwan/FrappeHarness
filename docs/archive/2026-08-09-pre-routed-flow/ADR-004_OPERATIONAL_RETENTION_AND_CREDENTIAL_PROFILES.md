# ADR-004: Secret-free credential profiles and bounded evidence retention

Status: accepted for V1 control-plane contracts.

The run ledger may record only a typed credential-profile identity: profile id,
provider, exact Bench/site/app target, authentication mode, expiry metadata, and
a bounded retention window. The secret itself is resolved by a short-lived
provider process (environment, session, or external secret manager) and is
never persisted, logged, or included in a receipt.

Profile acquisition and release are append-only, run-bound evidence. A profile
whose target differs from the immutable run intent is rejected. Release records
carry only a short operator reason and the same public profile identity.

REST verification remains closed and typed: fields, equality filters, ordering,
direction, page start, and page size are validated before transport. Page size
is bounded to 100; arbitrary query strings, headers, URLs, and credentials are
not accepted by the provider API.

Operational retention is intentionally conservative in V1: command output is
digest-only, lifecycle evidence is canonical JSON, and profile metadata may be
retained for at most 30 days (default zero-day retention). The typed
`OperationalBudget` contract also bounds command/model timeouts, artifact/log
bytes, retry count, model tokens, and model cost before executor wiring. Backup/checkpoint
files remain under the operator's separately protected Bench retention policy;
the harness ledger stores only their typed references and digests.
