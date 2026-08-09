"""Typed, secret-free operator inputs for the live modification chain.

The operator bundle is deliberately a *reference* document.  It carries no
specification, preflight response, generated artifact, credential, shell
command, or provider payload.  Those values are loaded by their typed
adapters after the run store and exact target have been checked.  Keeping the
boundary reference-only prevents a JSON document from becoming an alternate
execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping

from .run_store import LockTarget, RunIntent, RunRecord


class OperatorInputBundleError(ValueError):
    """The operator bundle is malformed, unsafe, or cannot be verified."""


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROFILE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def _text(value: Any, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OperatorInputBundleError(f"{label} must be a non-blank trimmed string")
    if pattern is not None and not pattern.fullmatch(value):
        raise OperatorInputBundleError(f"{label} contains unsupported characters")
    return value


def _digest(value: Any, label: str) -> str:
    value = _text(value, label)
    if not _HASH_RE.fullmatch(value):
        raise OperatorInputBundleError(f"{label} must be a lower-case SHA-256 digest")
    return value


@dataclass(frozen=True)
class OperatorArtifactRef:
    """A digest-bound, local JSON artifact reference; never a raw payload."""

    kind: Literal["spec", "plan", "preflight", "compilation"]
    path: Path
    sha256: str

    def __post_init__(self) -> None:
        if self.kind not in {"spec", "plan", "preflight", "compilation"}:
            raise OperatorInputBundleError("unsupported operator artifact kind")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise OperatorInputBundleError(f"{self.kind} path must be absolute")
        if self.path.is_symlink() or not self.path.is_file():
            raise OperatorInputBundleError(f"{self.kind} path must be an existing regular file")
        _digest(self.sha256, f"{self.kind} sha256")

    def verify(self) -> None:
        """Verify the referenced bytes without parsing or executing them."""
        try:
            digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError as error:
            raise OperatorInputBundleError(f"cannot read {self.kind} reference") from error
        if digest != self.sha256:
            raise OperatorInputBundleError(f"{self.kind} reference digest mismatch")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": str(self.path), "sha256": self.sha256}


@dataclass(frozen=True)
class OperatorSuccessorInputRefs:
    """Explicit digest-bound artifacts needed to construct V2 executor inputs.

    The references remain data-only.  Typed adapters must still parse and
    validate each document before constructing ``ProjectSpec``,
    ``MetadataPlan``, or ``CompilationResult`` values.
    """

    confirmed_spec: OperatorArtifactRef
    confirmed_plan: OperatorArtifactRef
    confirmed_compilation: OperatorArtifactRef

    def __post_init__(self) -> None:
        refs = (self.confirmed_spec, self.confirmed_plan, self.confirmed_compilation)
        if any(not isinstance(ref, OperatorArtifactRef) for ref in refs):
            raise OperatorInputBundleError("successor references must be typed artifact references")
        if tuple(ref.kind for ref in refs) != ("spec", "plan", "compilation"):
            raise OperatorInputBundleError("successor references have incorrect kinds")

    def as_dict(self) -> dict[str, dict[str, str]]:
        return {
            "confirmed_spec": self.confirmed_spec.as_dict(),
            "confirmed_plan": self.confirmed_plan.as_dict(),
            "confirmed_compilation": self.confirmed_compilation.as_dict(),
        }


@dataclass(frozen=True)
class OperatorInputBundle:
    """All non-secret inputs needed to compose one run-bound V1-to-V2 chain."""

    parent_run_id: str
    target: LockTarget
    provider_id: str
    spec: OperatorArtifactRef
    plan: OperatorArtifactRef
    preflight: OperatorArtifactRef
    compilation: OperatorArtifactRef | None
    approver: str
    approved: bool
    rationale: str | None = None
    schema_version: int = 1
    successor: OperatorSuccessorInputRefs | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in {1, 2}:
            raise OperatorInputBundleError("unsupported operator bundle schema version")
        _text(self.parent_run_id, "parent_run_id", _ID_RE)
        if not isinstance(self.target, LockTarget):
            raise OperatorInputBundleError("target must be a LockTarget")
        _text(self.provider_id, "provider_id", _PROFILE_RE)
        parent_refs = (self.spec, self.plan, self.preflight)
        if any(not isinstance(ref, OperatorArtifactRef) for ref in parent_refs):
            raise OperatorInputBundleError("bundle references must be typed artifact references")
        if tuple(ref.kind for ref in parent_refs) != ("spec", "plan", "preflight"):
            raise OperatorInputBundleError("bundle references have incorrect kinds")
        if self.schema_version == 1:
            if not isinstance(self.compilation, OperatorArtifactRef) or self.compilation.kind != "compilation":
                raise OperatorInputBundleError("schema v1 requires a typed compilation reference")
            if self.successor is not None:
                raise OperatorInputBundleError("schema v1 does not accept successor references")
        else:
            if self.compilation is not None:
                raise OperatorInputBundleError("schema v2 does not accept the ambiguous v1 compilation reference")
            if not isinstance(self.successor, OperatorSuccessorInputRefs):
                raise OperatorInputBundleError("schema v2 requires typed successor references")
        _text(self.approver, "approver", _ID_RE)
        if not isinstance(self.approved, bool):
            raise OperatorInputBundleError("approved must be a JSON boolean")
        if self.rationale is not None:
            _text(self.rationale, "rationale")

    def verify_references(self) -> None:
        for reference in self._references():
            reference.verify()

    def verify_reference_documents(self) -> None:
        """Validate that every referenced artifact is a JSON object.

        The bundle remains reference-only: this method does not retain or
        interpret the decoded payload.  It provides the narrow hand-off seam
        used by the eventual typed adapters, while rejecting a digest-valid
        scalar/array before an executor can treat it as a preflight, plan, or
        compilation document.
        """
        for reference in self._references():
            reference.verify()
            try:
                document = json.loads(reference.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise OperatorInputBundleError(
                    f"{reference.kind} reference must be valid UTF-8 JSON"
                ) from error
            if not isinstance(document, Mapping):
                raise OperatorInputBundleError(
                    f"{reference.kind} reference must contain a JSON object"
                )

    def verify_execution_admission(self, parent: RunRecord) -> None:
        """Validate all reference and approval prerequisites before composition.

        This is deliberately still fail-closed and side-effect free.  A
        future CLI adapter can call one typed seam before constructing the
        full run-bound executor; it cannot accidentally proceed with a
        digest-valid but malformed document or an unapproved bundle.
        """
        self.verify_reference_documents()
        self.verify_parent_intent(parent)
        if self.approved is not True:
            raise OperatorInputBundleError("operator bundle approval must be true for execution")
        if self.schema_version == 2 and self.successor_intent == parent.intent:
            raise OperatorInputBundleError("successor intent must differ from the persisted parent intent")

    @property
    def successor_intent(self) -> RunIntent:
        """Return the exact V2 intent approved by a schema-v2 bundle."""
        if self.schema_version != 2 or self.successor is None:
            raise OperatorInputBundleError("successor intent is available only for schema v2")
        return RunIntent(
            self.target,
            self.provider_id,
            self.successor.confirmed_spec.sha256,
            self.successor.confirmed_plan.sha256,
        )

    @property
    def reconfirmed_spec_hash(self) -> str:
        """Expose the digest that a typed adapter must reconfirm after parsing."""
        if self.schema_version != 2 or self.successor is None:
            raise OperatorInputBundleError("reconfirmed V2 specification is available only for schema v2")
        return self.successor.confirmed_spec.sha256

    def verify_parent_intent(self, parent: RunRecord) -> None:
        """Verify this reference bundle names exactly one persisted parent intent.

        This check is deliberately separate from parsing and digest verification:
        a valid JSON bundle is not execution authority until its parent ledger
        record binds the same target, provider, spec hash, and plan hash.
        """
        if not isinstance(parent, RunRecord):
            raise OperatorInputBundleError("parent must be a persisted RunRecord")
        if parent.id != self.parent_run_id:
            raise OperatorInputBundleError("operator bundle parent run does not match the persisted record")
        if parent.target != self.target:
            raise OperatorInputBundleError("operator bundle target does not match the persisted parent intent")
        if parent.intent.target != self.target:
            raise OperatorInputBundleError("persisted parent intent target does not match the operator bundle")
        if parent.intent.provider != self.provider_id:
            raise OperatorInputBundleError("operator bundle provider does not match the persisted parent intent")
        if parent.intent.spec_hash != self.spec.sha256:
            raise OperatorInputBundleError("operator bundle spec reference does not match the persisted parent intent")
        if parent.intent.plan_hash != self.plan.sha256:
            raise OperatorInputBundleError("operator bundle plan reference does not match the persisted parent intent")

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "parent_run_id": self.parent_run_id,
            "target": {"bench": self.target.bench, "site": self.target.site, "app": self.target.app},
            "provider_id": self.provider_id,
            "spec": self.spec.as_dict(),
            "plan": self.plan.as_dict(),
            "preflight": self.preflight.as_dict(),
            "approval": {"approver": self.approver, "approved": self.approved, "rationale": self.rationale},
        }
        if self.schema_version == 1:
            assert self.compilation is not None
            value["compilation"] = self.compilation.as_dict()
        else:
            assert self.successor is not None
            value["successor"] = self.successor.as_dict()
        return value

    def _references(self) -> tuple[OperatorArtifactRef, ...]:
        if self.schema_version == 1:
            assert self.compilation is not None
            return self.spec, self.plan, self.preflight, self.compilation
        assert self.successor is not None
        return (
            self.spec,
            self.plan,
            self.preflight,
            self.successor.confirmed_spec,
            self.successor.confirmed_plan,
            self.successor.confirmed_compilation,
        )


_BUNDLE_V1_KEYS = frozenset({
    "schema_version", "parent_run_id", "target", "provider_id", "spec", "plan",
    "preflight", "compilation", "approval",
})
_BUNDLE_V2_KEYS = frozenset({
    "schema_version", "parent_run_id", "target", "provider_id", "spec", "plan",
    "preflight", "successor", "approval",
})
_REF_KEYS = frozenset({"kind", "path", "sha256"})
_SUCCESSOR_KEYS = frozenset({"confirmed_spec", "confirmed_plan", "confirmed_compilation"})
_TARGET_KEYS = frozenset({"bench", "site", "app"})
_APPROVAL_KEYS = frozenset({"approver", "approved", "rationale"})


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OperatorInputBundleError(f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unknown:
            detail.append("unknown=" + ",".join(unknown))
        raise OperatorInputBundleError(f"{label} keys invalid ({'; '.join(detail)})")


def _ref(value: Any, label: str, expected_kind: str) -> OperatorArtifactRef:
    raw = _object(value, label)
    _exact_keys(raw, _REF_KEYS, label)
    if raw["kind"] != expected_kind:
        raise OperatorInputBundleError(f"{label} kind must be {expected_kind!r}")
    return OperatorArtifactRef(expected_kind, Path(_text(raw["path"], f"{label}.path")), _digest(raw["sha256"], f"{label}.sha256"))


def bundle_from_mapping(value: Mapping[str, Any]) -> OperatorInputBundle:
    """Parse a strict, payload-free mapping into a typed operator bundle."""
    raw = _object(value, "operator bundle")
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise OperatorInputBundleError("unsupported operator bundle schema version")
    _exact_keys(
        raw,
        _BUNDLE_V1_KEYS if schema_version == 1 else _BUNDLE_V2_KEYS,
        "operator bundle",
    )
    target = _object(raw["target"], "target")
    _exact_keys(target, _TARGET_KEYS, "target")
    approval = _object(raw["approval"], "approval")
    _exact_keys(approval, _APPROVAL_KEYS, "approval")
    successor = None
    compilation = None
    if schema_version == 1:
        compilation = _ref(raw["compilation"], "compilation", "compilation")
    else:
        successor_raw = _object(raw["successor"], "successor")
        _exact_keys(successor_raw, _SUCCESSOR_KEYS, "successor")
        successor = OperatorSuccessorInputRefs(
            confirmed_spec=_ref(successor_raw["confirmed_spec"], "successor.confirmed_spec", "spec"),
            confirmed_plan=_ref(successor_raw["confirmed_plan"], "successor.confirmed_plan", "plan"),
            confirmed_compilation=_ref(
                successor_raw["confirmed_compilation"],
                "successor.confirmed_compilation",
                "compilation",
            ),
        )
    return OperatorInputBundle(
        parent_run_id=_text(raw["parent_run_id"], "parent_run_id", _ID_RE),
        target=LockTarget(_text(target["bench"], "target.bench"), _text(target["site"], "target.site"), _text(target["app"], "target.app")),
        provider_id=_text(raw["provider_id"], "provider_id", _PROFILE_RE),
        spec=_ref(raw["spec"], "spec", "spec"),
        plan=_ref(raw["plan"], "plan", "plan"),
        preflight=_ref(raw["preflight"], "preflight", "preflight"),
        compilation=compilation,
        approver=_text(approval["approver"], "approval.approver", _ID_RE),
        approved=approval["approved"],
        rationale=approval["rationale"],
        schema_version=schema_version,
        successor=successor,
    )


def load_operator_input_bundle(path: str | Path, *, verify: bool = True) -> OperatorInputBundle:
    """Load a strict bundle; optionally verify every referenced digest."""
    source = Path(path)
    if source.is_symlink() or not source.is_absolute() or not source.is_file():
        raise OperatorInputBundleError("operator bundle path must be an absolute regular file")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OperatorInputBundleError("operator bundle is not valid UTF-8 JSON") from error
    bundle = bundle_from_mapping(raw)
    if verify:
        bundle.verify_references()
    return bundle


def bundle_json(bundle: OperatorInputBundle) -> str:
    if not isinstance(bundle, OperatorInputBundle):
        raise TypeError("bundle must be an OperatorInputBundle")
    return json.dumps(bundle.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
