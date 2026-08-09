"""Digest-bound typed loaders for schema-v2 operator references.

The operator bundle remains reference-only.  These loaders are the narrow
handoff from referenced JSON documents to the deterministic compiler/plan
builders; they never accept executable payloads or let a document override
the generated typed values.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .compiler import CompilationResult, compile_project
from .contracts import ProjectSpec, spec_hash
from .environment_preflight import (
    EnvironmentObservationAcquisitionError,
    normalize_environment_facts_payload,
)
from .operator_bundle import OperatorArtifactRef, OperatorInputBundleError
from .provider_plan import MetadataPlan, build_metadata_plan, plan_hash_document
from .run_store import LockTarget
from .spec_io import project_from_mapping


class OperatorTypedInputError(OperatorInputBundleError):
    """A referenced document cannot be reconciled with typed generated values."""


def load_confirmed_spec(reference: OperatorArtifactRef) -> ProjectSpec:
    _require_kind(reference, "spec")
    raw = _read_object(reference)
    try:
        value = project_from_mapping(raw)
    except Exception as error:
        raise OperatorTypedInputError("spec reference is not a valid ProjectSpec") from error
    if spec_hash(value) != reference.sha256:
        raise OperatorTypedInputError("spec reference digest is not the canonical ProjectSpec hash")
    return value


def load_confirmed_plan(reference: OperatorArtifactRef, spec: ProjectSpec) -> MetadataPlan:
    _require_kind(reference, "plan")
    if not isinstance(spec, ProjectSpec):
        raise OperatorTypedInputError("plan loader requires a typed ProjectSpec")
    raw = _read_object(reference)
    try:
        generated = build_metadata_plan(spec, provider=str(raw["provider"]))
    except (KeyError, TypeError, ValueError) as error:
        raise OperatorTypedInputError("plan reference is not a supported metadata plan") from error
    if raw.get("spec_hash") != generated.spec_hash:
        raise OperatorTypedInputError("plan reference does not bind the confirmed ProjectSpec")
    if _canonical_digest(raw) != reference.sha256:
        raise OperatorTypedInputError("plan reference digest does not match its canonical JSON document")
    if _canonical_digest(raw) == _canonical_digest(plan_hash_document(generated)):
        return generated
    if raw.get("plan_hash") != generated.plan_hash:
        raise OperatorTypedInputError("plan reference does not carry the generated plan hash")
    return generated


def load_confirmed_compilation(reference: OperatorArtifactRef, spec: ProjectSpec) -> CompilationResult:
    _require_kind(reference, "compilation")
    raw = _read_object(reference)
    if raw.get("schema_version") != 1 or not isinstance(raw.get("spec_hash"), str):
        raise OperatorTypedInputError("compilation reference has an unsupported envelope")
    generated = compile_project(spec)
    if raw["spec_hash"] != generated.spec_hash:
        raise OperatorTypedInputError("compilation reference does not bind the confirmed ProjectSpec")
    expected = _compilation_envelope(generated)
    if raw != expected or _canonical_digest(raw) != reference.sha256:
        raise OperatorTypedInputError("compilation reference does not match deterministic compiler output")
    return generated


def load_confirmed_preflight(reference: OperatorArtifactRef, target: LockTarget):
    """Load a complete, target-bound environment observation without inference."""
    _require_kind(reference, "preflight")
    if not isinstance(target, LockTarget):
        raise OperatorTypedInputError("preflight loader requires a typed LockTarget")
    raw = _read_object(reference)
    try:
        facts = normalize_environment_facts_payload(raw)
    except (EnvironmentObservationAcquisitionError, TypeError, ValueError) as error:
        raise OperatorTypedInputError("preflight reference is not a complete normalized facts document") from error
    identity = facts.identity
    if (identity.bench, identity.site, identity.app) != (target.bench, target.site, target.app):
        raise OperatorTypedInputError("preflight reference does not bind the exact target")
    return facts


def compilation_envelope(result: CompilationResult) -> dict[str, Any]:
    """Return the payload format accepted for a digest-bound compilation ref."""
    return _compilation_envelope(result)


def _compilation_envelope(result: CompilationResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "spec_hash": result.spec_hash,
        "artifact_hashes": {
            path: hashlib.sha256(content).hexdigest()
            for path, content in sorted(result.artifacts.items())
        },
        "ownership_manifest": result.ownership_manifest,
    }


def _require_kind(reference: OperatorArtifactRef, kind: str) -> None:
    if not isinstance(reference, OperatorArtifactRef) or reference.kind != kind:
        raise OperatorTypedInputError(f"typed loader requires a {kind} reference")


def _read_object(reference: OperatorArtifactRef) -> Mapping[str, Any]:
    reference.verify()
    try:
        raw = json.loads(reference.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OperatorTypedInputError(f"{reference.kind} reference is not valid UTF-8 JSON") from error
    if not isinstance(raw, Mapping):
        raise OperatorTypedInputError(f"{reference.kind} reference must contain a JSON object")
    return raw


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
