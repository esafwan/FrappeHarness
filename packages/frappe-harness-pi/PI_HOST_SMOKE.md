# Pi host smoke evidence

Observed locally on 2026-08-09:

```text
pi --version
0.82.1
```

The host is available and the lifecycle smoke now imports the compiled adapter
entry point inside a real Pi RPC extension, verifies its typed descriptor, and
exercises session replacement with built-in tools disabled. The smoke pins the
observed host version and verifies the required isolation flags. A
model-backed TUI/session test, host-version negotiation, and release
compatibility claim are still intentionally not claimed before PI-GATE;
compatibility is not claimed. The packed-distribution smoke loads the temporary npm-installed `dist/index.js`
factory in this Pi host and verifies the `pi` manifest in `package.json`, but
the host's Node 26.5.0 runtime is outside the package's declared `>=20 <23`
engine range. The executable `compatibility-gate` therefore fails closed and
records the remaining negotiation/model-session gaps. The
`--no-tools --no-extensions --no-skills --no-session --offline` startup flags
remain the required isolation shape.
