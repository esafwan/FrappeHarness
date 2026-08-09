"""Constrained, read-only subprocess adapter for ``frappectl`` verification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import subprocess
from typing import Any, Protocol, Sequence


class ReadOnlyCommandError(ValueError):
    """Raised when an argv vector is outside the verification command policy."""


class FrappectlToolUnavailable(RuntimeError):
    """Raised when the explicitly allowlisted verifier is not installed.

    This is intentionally distinct from a non-zero command receipt: no child
    process started, so inventing an exit code or receipt would misrepresent
    the evidence available to a run gate.
    """


class SafeArgv(Protocol):
    """Minimal command contract; other typed command objects may implement it."""

    @property
    def argv(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class ReadOnlyFrappectlCommand:
    """An already tokenized, policy-checked argv vector for ``frappectl``."""

    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_argv(self.argv)


@dataclass(frozen=True)
class RedactedProcessReceipt:
    argv: tuple[str, ...]
    exit_code: int
    started_at: str
    completed_at: str
    stdout_bytes: int
    stdout_sha256: str
    stderr_bytes: int
    stderr_sha256: str


@dataclass(frozen=True)
class FrappectlVerificationResult:
    receipt: RedactedProcessReceipt
    payload: Any | None


_READ_COMMANDS = {
    ("doctype", "list"),
    ("doctype", "show"),
    ("doc", "list"),
    ("doc", "get"),
}


def _validate_argv(argv: Sequence[str]) -> None:
    if not argv or any(not isinstance(part, str) or not part for part in argv):
        raise ReadOnlyCommandError("argv must contain non-empty string tokens")
    if argv[0] != "frappectl":
        raise ReadOnlyCommandError("read-only runner accepts only the frappectl executable")
    position = 1
    # JSON output and a profile are the only allowed global options. Accept
    # either order because frappectl supports global flags before subcommands.
    seen_json = False
    seen_site = False
    while position < len(argv) and argv[position] in {"--json", "-s", "--site"}:
        if argv[position] == "--json":
            if seen_json:
                raise ReadOnlyCommandError("--json may appear only once")
            seen_json = True
            position += 1
            continue
        if seen_site or position + 1 >= len(argv) or argv[position + 1].startswith("-"):
            raise ReadOnlyCommandError("site profile option requires one profile name")
        seen_site = True
        position += 2
    if tuple(argv[position : position + 2]) not in _READ_COMMANDS:
        raise ReadOnlyCommandError("command is not permitted by the read-only verification policy")
    if not seen_json:
        raise ReadOnlyCommandError("verification commands require --json")
    tail = argv[position + 2 :]
    if "--json" in tail:
        if tail.count("--json") != 1 or len(tail) == 1:
            raise ReadOnlyCommandError("invalid trailing --json option")
        tail = tuple(token for token in tail if token != "--json")
    command = tuple(argv[position : position + 2])
    expected_tail_count = {("doc", "get"): 2}.get(command)
    if command == ("doctype", "show"):
        valid = len(tail) in {1, 2} and (len(tail) == 1 or tail[1] == "--raw")
    elif command == ("doc", "list"):
        valid = len(tail) in {1, 3} and (len(tail) == 1 or tail[1] == "--limit")
    else:
        valid = expected_tail_count is not None and len(tail) == expected_tail_count
    if not valid or any(token.startswith("-") for token in tail[::2]):
        raise ReadOnlyCommandError("command arguments are outside the read-only policy")


class FrappectlRunner:
    """Run a policy-approved verification command without exposing process output."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, command: SafeArgv) -> FrappectlVerificationResult:
        argv = tuple(command.argv)
        _validate_argv(argv)
        started_at = _now()
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                check=False,
                capture_output=True,
                text=False,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as error:
            raise FrappectlToolUnavailable(
                "frappectl is unavailable; install the pinned read-only verifier before inspection"
            ) from error
        completed_at = _now()
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        receipt = RedactedProcessReceipt(
            argv=argv,
            exit_code=completed.returncode,
            started_at=started_at,
            completed_at=completed_at,
            stdout_bytes=len(stdout),
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_bytes=len(stderr),
            stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        )
        # A server error may contain an HTML page or sensitive diagnostic detail;
        # never attempt to interpret it as a successful verification result.
        payload = json.loads(stdout.decode("utf-8")) if completed.returncode == 0 else None
        return FrappectlVerificationResult(receipt=receipt, payload=payload)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
