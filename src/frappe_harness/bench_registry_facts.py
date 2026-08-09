"""Secret-free facts from the frappe-multihand bench registry."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .bench_command_policy import InspectedBenchTarget


class BenchRegistryFactsError(RuntimeError):
    """The registry cannot attest the exact disposable target."""


@dataclass(frozen=True)
class BenchRegistryFacts:
    target: InspectedBenchTarget
    production_designated: bool
    status: str
    purpose: str
    signed_by: str


class BenchRegistryFactsRunner:
    """Read only approved public registry fields; never retain credentials."""

    def __init__(self, registry_path: str | Path) -> None:
        self.registry_path = Path(registry_path)

    def inspect(self, target: InspectedBenchTarget) -> BenchRegistryFacts:
        if not isinstance(target, InspectedBenchTarget):
            raise ValueError("target must be an InspectedBenchTarget")
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BenchRegistryFactsError("bench registry is unavailable or malformed") from error
        if not isinstance(raw, Mapping):
            raise BenchRegistryFactsError("bench registry is not an object")
        if isinstance(raw.get("benches"), Mapping):
            entry = raw["benches"].get(target.bench_path.rsplit("/", 1)[-1])
        elif isinstance(raw.get("target"), Mapping) and isinstance(raw.get("registry_entry"), Mapping):
            target_payload = raw["target"]
            if (
                target_payload.get("bench") != target.bench_path
                or target_payload.get("site") != target.site_name
                or target_payload.get("app") != target.app_slug
            ):
                raise BenchRegistryFactsError("sanitized registry target does not match")
            entry = dict(raw["registry_entry"])
            entry.update({"path": target.bench_path, "site_name": target.site_name})
        else:
            raise BenchRegistryFactsError("bench registry has no typed benches map")
        if not isinstance(entry, Mapping):
            raise BenchRegistryFactsError("exact Bench is absent from the registry")
        if (
            entry.get("path") != target.bench_path
            or entry.get("site_name") != target.site_name
            or entry.get("type") != "disposable"
            or entry.get("status") != "ready"
        ):
            raise BenchRegistryFactsError("registry entry does not attest a ready disposable exact target")
        purpose = entry.get("purpose")
        signed_by = entry.get("signed_by")
        if not isinstance(purpose, str) or not purpose.strip() or not isinstance(signed_by, str) or not signed_by.strip():
            raise BenchRegistryFactsError("registry entry lacks purpose or signer attestation")
        return BenchRegistryFacts(target, False, "ready", purpose, signed_by)
