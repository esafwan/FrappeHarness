# Pi adapter compatibility

PI-01 scaffold status: package `@frappe-harness/frappe-harness-pi` `0.1.0`, Node
`>=20 <23`, compiled ESM plus declarations, zero runtime dependencies. The package is not
enabled by the core harness and does not claim compatibility with a Pi host
release yet.

The local host smoke and package metadata pin Pi `0.82.1` (see
`PI_HOST_SMOKE.md`), but this is not yet an accepted package/session
compatibility claim. Before PI-GATE, pin the exact Pi host package and commit its lockfile, then
verify package loading, session replacement, no-built-in-tools operation, and
headless CLI equivalence. Until those checks pass, this adapter remains an
isolated scaffold and must not be installed into an operator session.

The host-only smoke observed Pi `0.82.1` on 2026-08-09, verified the required
isolation flags, and exited successfully with all five flags supplied together
in offline mode. This is observational evidence only, not a host/package
compatibility claim or PI-GATE approval.

The model-free `scripts/pi-lifecycle-smoke.mjs` now exercises Pi RPC
`get_state`/`new_session` and extension `session_start`/`session_before_switch`
events with built-in tools disabled; it reports `compatibility_claim: false`.
This proves only the local lifecycle/control seam. Pi 0.82.1 exposes no
documented host-version handshake or `piHostVersion` negotiation, so package,
host, and session compatibility remain unclaimed.

`package.json` now includes a `pi` manifest declaring `dist/index.js` as the
extension entry, so the Pi host can discover the adapter when the package is
installed via its package manager.

`scripts/packed-distribution-smoke.mjs` builds the adapter, creates an npm
tarball in a temporary directory, installs that tarball without lifecycle
scripts, validates the `pi` manifest and compiled export/factory surface, and
loads the installed entry in the local Pi host with built-in tools, discovered
extensions, skills, sessions, and network startup disabled. It never installs
the package into Pi's settings and deletes the temporary artifact after the
check.

The packed-distribution smoke is packaging evidence only. The observed host is
running on Node 26.5.0, outside the declared `>=20 <23` engine range. The
fail-closed `npm run compatibility-gate` command exits non-zero and reports
`unsupported_node_runtime`, `host_version_negotiation_absent`, and
`model_backed_session_not_evidenced`. Therefore neither the successful packed
load nor the pinned Pi version constitutes PI-GATE approval or a compatibility
claim.
