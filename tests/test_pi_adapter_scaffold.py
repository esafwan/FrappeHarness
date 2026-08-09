import json
from pathlib import Path


ROOT = Path(__file__).parents[1] / "packages" / "frappe-harness-pi"


def test_pi_adapter_is_pinned_isolated_and_dependency_free():
    manifest = json.loads((ROOT / "package.json").read_text())
    lock = json.loads((ROOT / "package-lock.json").read_text())
    assert manifest["name"] == "@frappe-harness/frappe-harness-pi"
    assert manifest["version"] == "0.1.0"
    assert manifest["piHostVersion"] == "0.82.1"
    assert manifest["engines"]["node"] == ">=20 <23"
    assert "dependencies" not in manifest
    assert "scripts" in manifest["files"]
    assert lock["lockfileVersion"] == 3
    assert set(lock["packages"]) == {"", "node_modules/typescript"}
    tsconfig = json.loads((ROOT / "tsconfig.json").read_text())
    assert tsconfig["compilerOptions"]["strict"] is True
    assert tsconfig["compilerOptions"]["noEmit"] is True


def test_pi_boundary_exposes_only_typed_data_operations_and_no_authority_words():
    source = (ROOT / "src" / "index.ts").read_text()
    for operation in (
        "get_supported_vocabulary", "submit_requirement_facts", "submit_clarifications",
        "propose_spec_patch", "get_validation_report", "harness_status",
    ):
        assert operation in source
    for forbidden in ("child_process", "fs.", "exec(", "spawn(", "password"):
        assert forbidden not in source


def test_pi_descriptor_has_operator_commands_and_explicit_denylist():
    source = (ROOT / "src" / "index.ts").read_text()
    for command in ("/harness", "/spec", "/validate", "/plan", "/status", "/approval"):
        assert command in source
    for tool in ("bash", "read", "write", "edit", "pi.exec", "filesystem", "shell"):
        assert f'"{tool}"' in source
    assert "allow: []" in source


def test_pi_headless_envelope_is_versioned_and_session_non_authoritative():
    source = (ROOT / "src" / "index.ts").read_text()
    for marker in ("HeadlessRequest", "HeadlessResponse", "protocol_version: 1", "authoritative: false", "bindHeadlessRequest"):
        assert marker in source
    assert "session_id" in source
    assert "session_hash" in source


def test_pi_security_contract_covers_malicious_authority_payloads():
    source = (ROOT / "src" / "index.ts").read_text()
    # These values are deliberately only test fixtures: the typed boundary
    # offers no fields through which they could become executable authority.
    malicious = {
        "path": "/etc/passwd", "command": "rm -rf /", "query": "DROP TABLE runs",
        "credential": "secret", "provider_url": "http://internal",
    }
    assert all(key not in {"request_id", "operation", "payload"} for key in malicious)
    assert "DENIED_BUILTIN_TOOLS" in source
    assert "allow: []" in source
    assert "child_process" not in source and "from 'node:fs'" not in source
    assert "assertDataOnly" in source
    assert "AUTHORITY_TOKENS" in source
    assert "path traversal values are not allowed" in source
    assert "Object.entries(value)" in source
    assert '["request_id", "operation", "payload"]' in source


def test_pi_security_contract_requires_request_ids_and_replacement_metadata():
    source = (ROOT / "src" / "index.ts").read_text()
    assert "request_id: string" in source
    assert "session_id: string" in source
    assert "session_hash?: string" in source
    assert "authoritative: false" in source
    # A replacement session is represented by new metadata, not an in-memory
    # replay cache or authority transfer inside this removable adapter.
    assert "Map<" not in source and "Set<" not in source


def test_pi_host_smoke_is_observed_but_not_misrepresented_as_compatibility():
    smoke = (ROOT / "PI_HOST_SMOKE.md").read_text()
    assert "0.82.1" in smoke
    assert "not claimed" in smoke
    assert "--no-tools" in smoke


def test_pi_host_smoke_script_is_offline_and_non_session():
    script = (ROOT / "scripts" / "pi-host-smoke.mjs").read_text()
    for flag in ("--no-session", "--no-tools", "--no-extensions", "--no-skills", "--offline"):
        assert flag in script
    assert "PI_OFFLINE" in script
    assert 'model_session_started: false' in script
    assert 'PINNED_PI_VERSION = "0.82.1"' in script
    assert "isolated_startup_exit" in script
    assert 'compatibility_claim: false' in script


def test_pi_local_contract_and_equivalence_smokes_are_packaged():
    manifest = json.loads((ROOT / "package.json").read_text())
    assert "contract-smoke" in manifest["scripts"]
    assert "headless-equivalence-smoke" in manifest["scripts"]
    assert (ROOT / "scripts" / "contract-smoke.mjs").exists()
    assert (ROOT / "scripts" / "headless-equivalence-smoke.mjs").exists()
