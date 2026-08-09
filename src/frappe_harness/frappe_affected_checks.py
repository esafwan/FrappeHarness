"""Fixed, read-only Frappe provider for the V2 affected-check seam."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from .frappe_rest_provider import FrappeRestProvider
from .run_store import LockTarget, RunStore


@dataclass(frozen=True)
class BrowserAttestation:
    """Secret-free evidence that a browser probe exercised the bound target."""

    target: LockTarget
    url: str
    passed: bool
    page_title: str | None = None
    status_code: int | None = None
    response_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, LockTarget):
            raise ValueError("browser attestation target must be a LockTarget")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("browser attestation URL must be an absolute HTTP(S) URL without credentials")
        if not isinstance(self.passed, bool):
            raise ValueError("browser attestation passed must be boolean")
        if self.status_code is not None and (
            isinstance(self.status_code, bool) or not isinstance(self.status_code, int) or not 100 <= self.status_code <= 599
        ):
            raise ValueError("browser attestation status_code must be an HTTP status")
        if self.response_hash is not None and (
            not isinstance(self.response_hash, str) or len(self.response_hash) != 64
            or any(char not in "0123456789abcdef" for char in self.response_hash)
        ):
            raise ValueError("browser attestation response_hash must be a SHA-256 digest")


class BrowserProbe(Protocol):
    def __call__(self, target: LockTarget) -> BrowserAttestation: ...


@dataclass(frozen=True)
class FrappeAffectedCheckConfig:
    """Exact run binding and disposable record facts for one V2 check set."""

    bench: str
    site: str
    app: str
    spec_hash: str
    plan_hash: str
    task_name: str
    expected_record: Mapping[str, Any]
    browser_probe: BrowserProbe
    added_field: str = "reference_url"
    added_field_type: str = "Data"
    added_field_options: str = "URL"


class FrappeAffectedCheckProvider:
    """Run only the four fixed V2 checks through typed read-only REST calls."""

    def __init__(
        self,
        rest: FrappeRestProvider,
        config: FrappeAffectedCheckConfig,
        *,
        store: RunStore | None = None,
    ) -> None:
        if store is not None and not isinstance(store, RunStore):
            raise ValueError("affected-check store must be a RunStore when supplied")
        self.rest = rest
        self.config = config
        self.store = store

    def execute(self, run_id: str, check: str) -> Mapping[str, object]:
        if check not in {"migration", "preservation", "browser", "destructive_no_mutation"}:
            raise ValueError("unsupported affected check")
        browser_attestation: BrowserAttestation | None = None
        if check == "browser":
            browser_attestation = self._browser_attestation()
            passed = browser_attestation is not None and browser_attestation.passed is True
        else:
            passed = {
                "migration": self._metadata_matches,
                "preservation": self._record_preserved,
                "destructive_no_mutation": self._record_preserved,
            }[check]()
        result: dict[str, object] = {
            "passed": passed,
            "target": {"bench": self.config.bench, "site": self.config.site, "app": self.config.app},
            "spec_hash": self.config.spec_hash,
            "plan_hash": self.config.plan_hash,
        }
        if browser_attestation is not None:
            result["browser_attestation"] = {
                "target": {
                    "bench": browser_attestation.target.bench,
                    "site": browser_attestation.target.site,
                    "app": browser_attestation.target.app,
                },
                "url": browser_attestation.url,
                "passed": browser_attestation.passed,
                "page_title": browser_attestation.page_title,
                "status_code": browser_attestation.status_code,
                "response_hash": browser_attestation.response_hash,
            }
        if check == "destructive_no_mutation":
            result["mutation_attempted"] = False
        return result

    def _browser_attestation(self) -> BrowserAttestation | None:
        target = LockTarget(self.config.bench, self.config.site, self.config.app)
        try:
            attestation = self.config.browser_probe(target)
        except Exception:
            return None
        if not isinstance(attestation, BrowserAttestation):
            return None
        if attestation.target != target:
            return None
        if not isinstance(attestation.url, str) or not attestation.url.startswith(("http://", "https://")):
            return None
        return attestation

    def _metadata_matches(self) -> bool:
        result = self.rest.inspect_meta("Task")
        if not result.ok or not isinstance(result.payload, Mapping):
            return False
        data = result.payload.get("data", result.payload)
        fields = data.get("fields") if isinstance(data, Mapping) else None
        if not isinstance(fields, list):
            return False
        return any(
            isinstance(field, Mapping)
            and field.get("fieldname") == self.config.added_field
            and field.get("fieldtype") == self.config.added_field_type
            and field.get("options") == self.config.added_field_options
            for field in fields
        )

    def _record_preserved(self) -> bool:
        result = self.rest.get_document("Task", self.config.task_name)
        if not result.ok or not isinstance(result.payload, Mapping):
            return False
        data = result.payload.get("data", result.payload)
        if not isinstance(data, Mapping):
            return False
        return all(data.get(key) == value for key, value in self.config.expected_record.items())
