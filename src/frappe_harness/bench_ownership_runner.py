"""Bounded, read-only source ownership facts for the BEX-03 seam."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import subprocess
from typing import Any, Callable

from .environment_preflight import EnvironmentIdentity, OwnershipObservation
from .run_store import LockTarget


class OwnershipAcquisitionError(RuntimeError):
    """The fixed source ownership checks failed or returned drifted output."""


@dataclass(frozen=True)
class OwnershipRunner:
    """Check only the target app checkout; never accepts arbitrary argv."""

    process: Callable[..., Any] | None = None

    def inspect(self, target: LockTarget) -> OwnershipObservation:
        app_path = _app_path(target)
        run = self.process or subprocess.run
        clean = _run_empty(run, ("git", "-C", app_path, "status", "--porcelain=v1", "--untracked-files=all"), app_path)
        stashes = _run_empty(run, ("git", "-C", app_path, "stash", "list", "--format=%H"), app_path)
        generated = any(_path_exists(run, app_path, relative) for relative in ("api.py", "www"))
        return OwnershipObservation(
            EnvironmentIdentity(target.bench, target.site, target.app),
            source_clean=clean and stashes,
            ownership_conflict=generated,
        )


def _app_path(target: LockTarget) -> str:
    if not isinstance(target, LockTarget):
        raise OwnershipAcquisitionError("ownership target must be a LockTarget")
    if not target.bench.startswith("/") or any(part in {"", ".", ".."} for part in target.bench.split("/")[1:]):
        raise OwnershipAcquisitionError("ownership target Bench path is not canonical")
    if not target.app or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in target.app):
        raise OwnershipAcquisitionError("ownership app slug is not allowlisted")
    return str(PurePosixPath(target.bench) / "apps" / target.app)


def _run_empty(process: Callable[..., Any], argv: tuple[str, ...], cwd: str) -> bool:
    try:
        result = process(argv, shell=False, check=False, cwd=cwd, capture_output=True, text=False, timeout=30)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        raise OwnershipAcquisitionError("source ownership command failed") from error
    if result.returncode != 0:
        raise OwnershipAcquisitionError("source ownership command returned a non-zero exit")
    return not (result.stdout or b"").strip()


def _path_exists(process: Callable[..., Any], app_path: str, relative: str) -> bool:
    # Use a fixed Python existence probe through the injected process seam; no
    # caller-supplied shell or path is accepted.
    try:
        result = process(("test", "-e", str(PurePosixPath(app_path) / relative)), shell=False, check=False,
                         cwd=app_path, capture_output=True, text=False, timeout=30)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        raise OwnershipAcquisitionError("generated-path ownership probe failed") from error
    if result.returncode not in {0, 1}:
        raise OwnershipAcquisitionError("generated-path ownership probe returned an invalid exit")
    return result.returncode == 0
