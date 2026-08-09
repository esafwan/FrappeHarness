import subprocess

from frappe_harness.frappectl_adapter import FrappectlReadOnlyAdapter
from frappe_harness.frappectl_policy import DoctypeShowRequest
from frappe_harness.frappectl_runner import FrappectlRunner


def test_policy_argv_integrates_with_the_no_shell_runner(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(argv, 0, b'{"data": {}}', b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = FrappectlReadOnlyAdapter(FrappectlRunner()).inspect(DoctypeShowRequest("Task"))

    assert seen["argv"] == ("frappectl", "--json", "doctype", "show", "Task")
    assert result.payload == {"data": {}}


def test_adapter_binds_to_a_preconfigured_read_only_profile(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, b'{"data": {}}', b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    FrappectlReadOnlyAdapter(FrappectlRunner(), profile="harness-live").inspect(DoctypeShowRequest("Task"))

    assert seen["argv"] == ("frappectl", "--json", "--site", "harness-live", "doctype", "show", "Task")
