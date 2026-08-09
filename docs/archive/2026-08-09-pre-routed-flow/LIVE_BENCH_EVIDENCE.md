# Disposable Bench Evidence

This record captures the integration evidence for the independently provisioned
development Bench. It is not an approval for mutation or release. The live
probe was renewed on 2026-08-09; the new multihand provider is a disposable
HUF `develop` snapshot and does not mutate the reference Bench.

| Item | Observed evidence |
| --- | --- |
| Latest multihand provider | `frappe-harness-live-20260809-huf`, site `frappe-harness-live-20260809-huf.local`, web port `8093`; HUF checkout `954e8a104be3467045558b94216d499138d7e23d`, Frappe `15.117.0`, HUF `1.0.0-beta.1`. Provisioned by `/workspace/development/mh-tools/provision.sh` with dedicated Redis/database allocation; `/api/method/ping` returned `{"message":"pong"}`. |
| Bench identity | `frappe-harness-live-20260804`, site `frappe-harness-live-20260804.local` |
| BEX-02 facts spike | The bounded `BenchFactsRunner` executed only `bench version`, `bench --site frappe-harness-live-20260804.local list-apps --format json`, and `bench --site frappe-harness-live-20260804.local show-config -f json` through the disposable container bridge. It parsed Frappe `16.29.0`, `frappe`/`huf`/`task_tracker`, `developer_mode=true`, and `db_type=mariadb`; the parser discarded secret config keys and retained normalized evidence in `fixtures/live/environment_facts_2026-08-09.json`. `production_designated` was absent and therefore remains unknown; database health, disk, and tooling remain unsupported rather than inferred. |
| Isolation | Separate normal HUF app checkout and track worktree; both clean, unstashed, and non-symlinked. The existing reference Bench was not used. |
| HUF baseline | GitHub `develop` commit `4f05874e79b9e8427491bab795da4960e0ca7a70`; HUF `1.0.0-beta.1` installed. |
| Framework | Frappe `16.29.0` on `version-16`. |
| Health | Internal `GET /api/method/ping` returned `{"message":"pong"}` on the isolated web port. |
| Fresh disposable identity lifecycle | Run `2cd56562-f7b2-451e-bfb8-0ce3f34449a8` used the typed `FrappeDisposableTestIdentityProvider` against the Frappe-16 site with process-local Administrator session credentials, provisioned only `Task Manager` and `Task User`, persisted `test_identity_provisioned` ref `run:2cd56562-f7b2-451e-bfb8-0ce3f34449a8:evidence:98df8ee6-62a3-481f-a515-9d1007638de6`, cleaned both users, and persisted `test_identity_cleaned` ref `run:2cd56562-f7b2-451e-bfb8-0ce3f34449a8:evidence:2cfbcd59-eec7-4e36-8862-e3944f3b3109` in `/Users/safwan/Library/Caches/frappe-harness/identity-evidence-20260809.sqlite3`. A follow-up typed User listing found zero remaining `harness_user_` identities; no passwords/cookies were retained and no gate approval was recorded. |
| Latest-provider migration | On the fresh `develop` provider, `bench --site frappe-harness-live-20260809-huf.local migrate` exited `0` after Frappe and HUF DocType updates. |
| Live V2 affected checks | The typed `FrappeAffectedCheckProvider` ran after an exact V1→V2 reconfirmation on the disposable Frappe-16 Bench and persisted all four checks under run `6944e502-814d-4ea8-ae02-0100bfe14e93`: V2 metadata field match, existing-record preservation, generated page status 200, and destructive no-mutation (`mutation_attempted:false`). Normalized refs are retained in `fixtures/live/v2_affected_checks_2026-08-09.json`; no raw output or credentials were retained. |
| Fresh durable V2 affected-check run | On 2026-08-09, after restoring the generated `task_tracker.html` page into the isolated disposable app checkout, `tools/live_v2_affected_evidence.py` created run `fe86b4ce-17d6-44a6-af7f-4a9f28adc56b` in the operator-selected cache ledger `/Users/safwan/Library/Caches/frappe-harness/v2-evidence-20260809-b.sqlite3`. Typed REST metadata, record preservation, a real Playwright page/bootstrap probe, and destructive no-mutation all passed under exact V2 hashes; refs are `run:fe86b4ce-17d6-44a6-af7f-4a9f28adc56b:evidence:6ed2b613-79bf-4061-87a5-7765f81ce240`, `run:fe86b4ce-17d6-44a6-af7f-4a9f28adc56b:evidence:e523703f-f391-413c-910c-9c59d1441d90`, `run:fe86b4ce-17d6-44a6-af7f-4a9f28adc56b:evidence:6de8340c-7570-4f1b-b6bb-3426eb322069`, and `run:fe86b4ce-17d6-44a6-af7f-4a9f28adc56b:evidence:4b8a7390-78fa-492a-b52a-b4610a71108d`. No gate approval was recorded and no cookie/response body was retained. |
| `frappectl` | Pinned `frappectl 0.18.1` installed in the Bench environment. An authenticated, process-local read of HUF DocType `Agent` returned matching metadata (`name: Agent`, 76 fields, 4 permissions). |
| Typed harness probes | `FrappectlReadOnlyAdapter` returned live `Task` metadata through the policy runner (exit 0, JSON payload); `BenchRunner(SiteMigrate)` executed the exact `bench --site … migrate` argv (exit 0, output retained only as digests). |
| Live read-only reconciliation probe | The closed `FrappeRestProvider` now supports a typed, process-local session cookie alternative to API tokens. Against the restarted disposable Frappe-16 site, `inspect_meta("Task")` returned HTTP 200 and the expected `reference_url` `Data/options=URL` field through `FrappeMigrationInspectionProbe`; only the digest reference was retained. The session cookie and Bench were discarded/stopped after the probe. |
| Live interruption/replay reconciliation | Run `a5aad7e0-722b-4d30-998e-98a9cd117ad4` intentionally interrupted a typed migration provider after the durable `STARTED` attempt. A second provider call was refused while the lifecycle was `interrupted`; the live session-cookie metadata inspection then produced `migration_reconciliation` evidence and reset the state to `pending`. The generalized probe also verified the V2 Task metadata directly from the bound plan (`spec_hash` `98c5…`, `plan_hash` `2d8a…`). Normalized details are in `fixtures/live/reconciliation_replay_2026-08-09.json`; no raw output, cookie, or credentials were retained. |
| Run-bound lifecycle | A disposable run passed environment preflight, compilation, exact plan approval, `RunBoundRecoveryExecutor` backup/checkpoint, `RunBoundMigrationExecutor` (`bench --site … migrate`, exit 0), and `RunBoundFrappectlMetadataAdapter` using raw read-only metadata through `METADATA_VERIFIED`. The first non-raw probe correctly failed on omitted defaults; raw mode then matched the compiler-generated Project/Task plan exactly. |
| Generated frontend deployment | The compiled app-specific `task_tracker.api.frontend_manifest` endpoint returned Frappe's `{message: ...}` envelope with `Task Manager` for the manager test user and `Task User` for the regular test user; unauthenticated access returned HTTP 403. The packaged `/task_tracker.html` page returned HTTP 200 and its `/assets/task_tracker/harness/assets/index-JuuX1hsy.js` asset returned HTTP 200. New generated files were quarantined after the probe and the app checkout is clean again. |
| Live browser/session acceptance | Headless Playwright against the isolated Bench loaded `/task_tracker.html` (200), verified Guest manifest denial (403), logged in ephemeral Manager/User browser sessions (200), and observed server-derived `Task Manager`/`Task User` roles. With the session CSRF cookie, Manager created Project and linked Task (200/200) and deleted both (202/202); a mutation-disabled Task User run then loaded an existing Task through the generated SPA route and passed the no-Delete-button assertion. All proof records/users were disposable. |
| Cross-runtime manifest integrity | A live browser run exposed a Python/TypeScript canonical-hash newline mismatch; the compiler now hashes compact JSON without the artifact newline, and focused Python/TypeScript tests plus the live browser run pass against the same manifest. |
| Golden deployment | The compiler-generated `task_tracker` app completed scaffold, audited artifact copy, clean-source checkpoints, build, install, and migration on this Bench. The current golden source checkpoint is `8d5bf3273fccec7b4999fcbdc08640fb34b4a6b8`. |
| Golden metadata | After the corrected golden fixture was redeployed, typed `frappectl doctype show --raw` reads for generated `Project` and `Task` match the approved provider plan exactly (`spec_hash` `d069875f55943557c6929617c62726057e8ab5e8099d3754a87b134734669e5e`). |
| Golden behavior | Standard authenticated Frappe document API created a Project and a linked Task; the Task read-back retained its generated name, Link, and `Open`/`Medium` Select values. |
| Generated verification run | The current hash-bound 82-case plan passed 82/82 live cases through the closed typed REST provider: 26 backend, 24 permission, and 32 REST cases. It exercised supported types, required fields, Select/Link validation, CRUD by Manager/User/unassigned actors, filters/order/pagination, normalized validation errors, and anonymous collection denial. |
| Run-bound verification receipt | Run `d2f6d257-a1ff-4f84-ab4f-36358cdba687` persisted an all-pass 82-case report (`run:d2f6d257-a1ff-4f84-ab4f-36358cdba687:verification:8131a14a-3658-4395-9d4e-03e0a80ca644`). The executor correctly declined the `BACKEND_VERIFIED` transition because this run had not yet recorded its metadata predecessor; no state was falsely advanced. |
| Full run-bound READY receipt | Run `82ef49c3-e40a-4a99-bd7d-b2c0a4b806b3` passed preflight → compilation → plan approval → typed backup/checkpoint → `bench migrate` → raw `frappectl` metadata (`matches: true`) → 82/82 typed REST cases → four frontend scenarios → `READY` → `SUCCEEDED`. Persisted backend report id: `139f8554-e4d3-4d73-93a0-fdf78837c703`. Final receipt contained only hashes, references, and counts; all prefixed records, ephemeral users, generated files, and Bench processes were cleaned afterward. |
| Role proof | The full generated 24-case Manager/Task User/unassigned CRUD matrix passed. Every denied mutation used a distinct disposable target, and manager success used distinct delete targets. |
| Unauthenticated REST | Unauthenticated v2 `Project` and `Task` collection requests both returned Frappe HTTP `403`; the credential-free typed provider correctly normalizes those as `authentication_required` while authenticated `403` remains `permission_denied`. |
| Resource isolation | Registry allocation: web/socket/watcher `8111`/`9111`/`6811`; Redis database `13`; dedicated site/database. |
| Full-chain readiness probe | On 2026-08-09, the new `DockerBenchRunner` successfully executed the exact disposable `version`, `list-apps`, and `show-config` probes inside `frappe_docker_devcontainer-frappe-1` (`target_probe_passed:true`). `tools/live_v1_v2_modification_evidence.py` then failed closed with a fresh external store and no ledger because typed generated-artifact deployment and clean source-checkpoint providers are still unavailable; `evidence_store_created:false`, `gate_approval_recorded:false`. This is transport/readiness evidence only, not V1→V2 migration evidence. |

Credentials used for the read probe were generated only on the disposable
development site, passed through process environment for that command, and are
not retained in this repository, artifacts, command receipts, or this record.

The renewed migration was preceded by a fresh site backup:
`20260804_032610-frappe-harness-live-20260804_local-database.sql.gz`.
The generated app source is clean, has no stashes, and contains no legacy
pre-canonical DocType path or tracked Python-cache files. `bench --site … version`
now reports `task_tracker 0.0.1`, proving the generated package retains the
Frappe-required `__version__` declaration.

The live generated-verification run was preceded by another fresh backup:
`20260804_035030-frappe-harness-live-20260804_local-database.sql.gz`.
It created a one-run unassigned actor and prefixed probe data only. Cleanup
removed all recorded probe documents and the actor; post-run exact checks found
no matching Project/Task probe records, no temporary user, no app-source
changes or stashes, and a healthy ping response.

### Controlled modification: optional Task Reference URL

The specification's §19.7 scenario was exercised on the same disposable
Bench. The checked-in V2 fixture adds only optional `Task.reference_url` to
the Task form; the deterministic schema diff contains the safe additive field
and safe screen update only. A fresh backup preceded the source checkpoint
`5d4e5347caf69db720775678a506f9264d426e78` and migration:
`20260804_040305-frappe-harness-live-20260804_local-database.sql.gz`.

Frappe 16 requires the semantic URL projection `fieldtype: Data` with
`options: URL`; raw Project/Task metadata matches the V2 plan exactly. The
preexisting golden Project and Task remained readable after migration. A
disposable valid URL Task was created and removed; an invalid URL returned
HTTP `417`, normalized as a validation error. The generated V2 page was then
exercised in a Manager browser session: a valid
`https://example.com/reference` Task returned 200, `not-a-url` returned the
expected Frappe 417 validation response, and the temporary Project/Task were
deleted with 202 responses. The generated app source is again clean and
unstashed. Desk/base-frontend-specific proof remains out of scope.

Remaining work: production designation and database health; host disk/tooling facts now have a strict read-only adapter but still require target-bound integration evidence.
acquisition, live provider replay/reconciliation generalization, live
provider-side V2 affected-check gate review, free-form Gemma4/Gemini
equivalence and candidate comparison, and all milestone/release approvals.
The golden V1 provider/frontend lifecycle and final READY receipt are already
proven above.

### Destructive-change no-mutation probe

On 2026-08-09, a read-only probe against the same disposable site classified a
`Task.priority` type/options change as blocked (`field_type`, `field_options`)
before any provider call. The harness admission output recorded
`mutation_attempted: false`; the live `Task` count remained `5` before and after,
and the `Task` DocType modified timestamp remained
`2026-08-09 07:06:08.104769`. This is live safety evidence, not an M8 approval:
provider-side affected-check execution and signed gate review remain outstanding;
the run-bound executor and normalized durable evidence path are implemented and
covered by focused tests.

### Source ownership probe

The post-run read-only ownership probe on 2026-08-09 returned a clean `master`
checkout, zero Git stashes, and no generated compiler-owned `task_tracker/api.py`
or `task_tracker/www` path; the baseline `task_tracker/public/.gitkeep` remains
the only expected public-directory content. This supports
`source_clean=true`/`ownership_conflict=false` for the disposable target. The
reusable `OwnershipRunner` now provides that fixed read-only collection seam;
production-designation collection remains separate implementation work.

The bounded `OwnershipRunner` also detected stale generated paths in the
disposable app checkout before cleanup, then returned `source_clean=true` and
`ownership_conflict=false` after restoring only the owned manifests and
removing those exact disposable paths. Normalized evidence is in
`fixtures/live/ownership_facts_2026-08-09.json`; no command output was retained.
