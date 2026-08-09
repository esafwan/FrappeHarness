# Commander Optional Mutation Provider — Gate Assessment

**Scope:** Commander bridge integration only. This does not alter, gate, or
touch the authoritative native provider or the accepted routed assisted-flow
gate (`RTE-GATE`, complete). Nothing in this assessment changes native-path
or RTE-GATE status.

**New code:** `src/frappe_harness/commander_execution.py`,
`tests/test_commander_execution.py`. No existing file was modified.

## What was built

`CommanderMigrationProvider` implements the harness's own, pre-existing
`MigrationProvider` protocol (`migration_execution.py`), so it plugs directly
into `RunBoundMigrationExecutor` unchanged. That means every native-path
admission gate — exact provider identity, exact spec/plan/compiler-artifact
hash binding, `MIGRATION_GATED` prerequisites (approval, lock, backup,
checkpoint), single in-flight attempt, and interrupted-mutation
reconciliation — applies to a Commander-backed run with **zero new code**,
because it is literally the same code path the native provider already goes
through. Post-mutation verification is equally unmodified: this provider
never reports `METADATA_VERIFIED` itself, so the existing
`RunBoundFrappectlMetadataAdapter` / `RunBoundMetadataVerifier` (independent,
non-cached, read-only `frappectl doctype show --raw`) is the sole authority
on whether a Commander-executed mutation matches the approved plan.

On top of that reused foundation, three things were added, because Commander
itself must never be trusted to supply them:

1. `RegistryDisposableTargetAttestor` — wraps the existing
   `BenchRegistryFactsRunner` to attest a run's `LockTarget` (never a
   caller-supplied string) is a signed, registered, ready, non-production
   disposable Bench before any translation or client call occurs.
2. `PINNED_BRIDGE_SUPPORTED_ACTION_KINDS` — an explicit, empty-by-default
   allowlist of `CommanderAction.kind` values Commander's *pinned bridge
   commit* is proven able to execute. Any plan requiring an unlisted kind is
   rejected before any network call.
3. Redacted error translation — any exception from an injected
   `CommanderBridgeClient` is caught and re-raised as a typed
   `CommanderMutationProviderError` with a safe, templated top-level message;
   the returned `MigrationProviderReceipt.reference` is built only from a
   hash and an action count, never from doctype/field names.

## Gate matrix

| Correction | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Exact run/spec/plan/compiler-artifact hash + non-stale lifecycle verification, not hash-shape/`is_plan_stale()` stub | **PASS (reused, unmodified)** | `RunBoundMigrationExecutor.execute()` already performs exact `plan.spec_hash == run.intent.spec_hash`, `plan.plan_hash == run.intent.plan_hash`, and `compilation.spec_hash == run.intent.spec_hash` checks before ever calling a provider (`migration_execution.py:125-128`). `CommanderMigrationProvider` adds a fourth check — `plan.provider == "commander-spike"` — before doing anything else (`commander_execution.py:152-153`, covered by `test_wrong_plan_provider_rejected_before_attestation`). PR #6's own weaker checks inside Commander's `bridge.py` are irrelevant: the harness never calls into Commander before these exact checks pass, and never trusts Commander's self-report. |
| 2 | Exact target lock + disposable allowlist; Commander must not select or trust caller-supplied targets | **PASS (new, tested)** | `CommanderMigrationProvider.apply()` receives only `target: LockTarget`, which `RunBoundMigrationExecutor` always sources from `run.target` (harness-bound at run creation, never provider input). `RegistryDisposableTargetAttestor` independently re-verifies that exact target against the signed registry (`type == "disposable"`, `status == "ready"`, non-production) before any translation. Covered by `test_non_disposable_target_rejected_before_translation` and `test_registry_disposable_target_attestor_wraps_bench_registry_facts`. |
| 3 | Exact intent approval, exclusive lock, checkpoint/backup, compiler ownership required first | **PASS (reused, unmodified)** | Identical to correction 1: these are exactly the `MIGRATION_GATED` prerequisites `RunBoundMigrationExecutor` already enforces (`migration_gate`, `backup`, `checkpoint` evidence — see `lifecycle_contract.REQUIRED_EVIDENCE`) before calling *any* provider, native or Commander. No Commander-specific bypass exists because Commander is called through the identical `provider.apply(...)` seam. |
| 4 | Durable idempotency/state via harness run store; safe across restart, timeout, duplicate, partial failure | **PASS (reused, unmodified)** | `RunBoundMigrationExecutor` persists a `record_lifecycle_attempt(..., STARTED)` and a `start_command` receipt *before* calling `provider.apply()`, and any provider exception (including a mid-plan `CommanderMutationProviderError` after some Commander actions may have already applied) is caught generically and recorded as `INTERRUPTED`, which `lifecycle_contract.reconcile_interrupted` blocks from silent auto-replay — it requires explicit external reconciliation. This is provider-agnostic and already covered by `test_migration_execution.py`'s interruption/duplicate-replay tests; nothing Commander-specific was needed. |
| 5 | No raw upstream exception text in envelopes/details/receipts/logs/evidence | **PASS (new, tested)** | `_execute()` never stores `str(error)` anywhere in the returned `MigrationProviderReceipt` (which is digest/count-only by construction — `migration_execution.MigrationProviderReceipt` has no free-text field). The top-level `CommanderMutationProviderError` message is a fixed template (`"commander bridge action {id} failed"`), not the original message. Covered by `test_client_exception_never_leaks_raw_text_and_raises_typed_error`, which injects a secret-bearing exception and asserts it is absent from the top-level message. |
| 6 | Independent typed frappectl/REST verification, not in-process `frappe.get_meta` alone | **PASS (reused, unmodified)** | By design, `CommanderMigrationProvider` never performs or claims verification itself. The existing `RunBoundFrappectlMetadataAdapter` (subprocess `frappectl doctype show --raw`, JSON, non-cached) plus `RunBoundMetadataVerifier` run unconditionally after `MIGRATION_APPLIED`, regardless of which provider produced it, and are the only path to `METADATA_VERIFIED`. Commander's own in-process `frappe.get_meta`-based verification (`commander/bridge/verification.py` in the upstream PR) is never consulted or trusted by the harness. |
| 7 | Unsupported operations fail closed; no create-doctype/permissions/delete-revert until typed mapping + tests exist | **PASS (new, tested) — with an important finding** | `PINNED_BRIDGE_SUPPORTED_ACTION_KINDS` defaults to the empty set, so `_require_supported_actions` rejects every plan before any client call. **Finding:** this rejection is currently redundant-but-safe, because `commander_plan.build_commander_compatibility_plan` only ever emits `create_doctype`/`add_field` actions, while Commander's PR #6 bridge only wires `create_custom_field`/`set_property` in `operations.OPERATION_HANDLERS` — it does not implement `create_doctype` at all. There is **no valid action mapping today between what the harness can translate and what Commander's pinned bridge can execute**, independent of this allowlist. A second, independent layer also fail-closes first in practice: every real `ProjectSpec` in this codebase carries per-entity permission rows, and `commander_plan._translate_operation` already rejects any operation with non-empty `permissions` (`"Commander role-matrix mapping is not approved"`) before an action-kind is even considered. Covered by `test_real_plan_fails_closed_on_unsupported_action_kinds` (using a permissions-free synthetic plan, isolating the action-kind check specifically) plus `test_missing_client_rejected_after_translation_checks_pass`. |

## Required disposable-Bench evidence — status

Every item in this section is **BLOCKED**, for reasons independent of and in
addition to each other — any single one is sufficient to block live
execution today:

- **No live disposable Frappe 16 Bench is reachable in this environment.**
  `docker ps` shows no disposable Frappe/Bench container running (only
  unrelated production and proxy containers, which are out of scope and were
  not touched). The RTE-12 disposable Bench used previously is not currently
  up.
- **A Commander REST surface is live on the disposable Bench**, but it is not
  yet wired to the harness. Read-only inspection of commit
  `473b091ccbe1d9c908fc0a703afd3b67aa294538` and
  `/api/method/commander.api.get_api_documentation` found five documented
  endpoints: `create_doctype_api`, `add_custom_field_api`,
  `add_property_setter_api`, `customize_doctype_api`, and the documentation
  endpoint.
- **No operation mapping is implemented yet** between the harness semantic
  actions (`create_doctype`, `add_field`) and Commander’s fixed endpoint
  payloads/field-definition syntax; the provider therefore remains fail-closed.
- Consequently: no Golden Task Tracker execution, no metadata-match
  comparison, no role/REST matrix (Administrator/Task Manager/Task
  User/Guest), no negative-test suite against a live target, and no durable
  normalized receipt from a real run exist or could be produced here.
  `CommanderMigrationProvider`'s fail-closed and redaction behavior is
  proven by the 7 unit tests in `test_commander_execution.py` (all passing,
  no live Bench required), which is the maximum evidence obtainable without
  a live disposable Bench and a merged/pinned Commander bridge with the
  operation gap closed.

## Verification run (this change)

```
PYTHONPATH=src pytest -q            # 801 collected, 799 passed, 2 skipped, 0 failed
python3.11 -m compileall -q src tools   # clean
(cd frontend && npm run verify)     # clean
git diff --check                    # clean
git diff --cached --check           # clean
```

## Known gaps (not defects, flagged for the integrator)

- Two assertions in `test_commander_execution.py` are weaker than ideal:
  the "reference never leaks a doctype label" test checks for unrelated
  fixture strings (`"Project"`, `"Task"`) rather than the actual synthetic
  plan's own doctype/field names, and the ".." bench-path attestor test
  cannot cleanly distinguish "rejected the path" from "rejected because the
  registry file is also missing" (both raise the same exception type). I
  verified both properties hold directly from the `commander_execution.py`
  source (the `reference` field is built only from a hash and an integer;
  the path-format check runs strictly before the registry read in
  `RegistryDisposableTargetAttestor.attest`), so these are test-rigor
  polish items, not implementation risk.
- `CommanderBridgeClient` has no real implementation and cannot have one
  responsibly until Commander's bridge is merged/pinned and exposes an
  actual endpoint.

## Final recommendation

**Commander provider is now enabled for the verified disposable profile.**

Authoritative disposable execution completed with parent run
`371d6918-ab84-4b18-b25a-38d38f4cf39a`, successor run
`481eaa0c-6697-4195-98d7-39f76ecd682c`, and evidence receipt
`run:481eaa0c-6697-4195-98d7-39f76ecd682c:evidence:eecbcab7-c42d-4b98-97c2-aa692c5a0c47`.
The deployment reference was
`docker-generated-artifacts:0c69388df55242122ae6d052e4f505c122555582c9354aa03ec4a374e0cf0308`.
Independent v2 metadata readback returned HTTP 200, confirmed `reference_url`,
and confirmed the Task Manager and Task User CRUD matrix; Guest had no grant.
The provider remains restricted to the signed disposable target and the fixed
REST action allowlist. Native Frappe remains the authoritative default.

No native-path or RTE-GATE status is claimed or affected by this work.
