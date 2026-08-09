"""Bridge typed read-only requests to the runner without widening either boundary."""

from __future__ import annotations

from typing import Any

from .frappectl_policy import ReadOnlyRequest, build_readonly_argv
from .frappectl_runner import FrappectlRunner, FrappectlVerificationResult, ReadOnlyFrappectlCommand


class FrappectlReadOnlyAdapter:
    def __init__(self, runner: FrappectlRunner | None = None, *, profile: str | None = None) -> None:
        self._runner = runner or FrappectlRunner()
        self._profile = profile

    def inspect(self, request: ReadOnlyRequest) -> FrappectlVerificationResult:
        return self._runner.run(ReadOnlyFrappectlCommand(build_readonly_argv(request, profile=self._profile)))
