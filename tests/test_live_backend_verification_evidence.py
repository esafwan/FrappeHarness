from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import live_backend_verification_evidence as lbve

from frappe_harness.frappe_verification_probe import CreatedRecord, VerificationFixtures
from frappe_harness.operator_bundle import (
    OperatorArtifactRef,
    OperatorInputBundle,
    OperatorInputBundleError,
    OperatorSuccessorInputRefs,
)
from frappe_harness.run_store import LockTarget, RunIntent, RunRecord, RunState
from frappe_harness.verification_program import (
    CaseResult,
    ProgramCase,
    VerificationProgram,
    VerificationRunReport,
)


def _hash(value: str) -> str:
    """Return a 64-character lower-case hex digest starting with the given value."""
    return (value * 64)[:64]


def _target() -> LockTarget:
    return LockTarget("/bench", "dev.local", "task_tracker")


def _artifact_ref(tmp_path: Path, kind: str, sha256: str) -> OperatorArtifactRef:
    path = tmp_path / f"{kind}.json"
    path.write_text("{}", encoding="utf-8")
    return OperatorArtifactRef(kind, path, sha256)


def _bundle(tmp_path: Path, target: LockTarget | None = None) -> OperatorInputBundle:
    target = target or _target()
    spec_hash = _hash("0a")
    plan_hash = _hash("0b")
    preflight_hash = _hash("0c")
    compilation_hash = _hash("0d")
    successor = OperatorSuccessorInputRefs(
        confirmed_spec=_artifact_ref(tmp_path, "spec", spec_hash),
        confirmed_plan=_artifact_ref(tmp_path, "plan", plan_hash),
        confirmed_compilation=_artifact_ref(tmp_path, "compilation", compilation_hash),
    )
    return OperatorInputBundle(
        parent_run_id="parent-run",
        target=target,
        provider_id="frappe-native",
        spec=_artifact_ref(tmp_path, "spec", spec_hash),
        plan=_artifact_ref(tmp_path, "plan", plan_hash),
        preflight=_artifact_ref(tmp_path, "preflight", preflight_hash),
        compilation=None,
        approver="operator",
        approved=True,
        rationale=None,
        schema_version=2,
        successor=successor,
    )


def _run(
    target: LockTarget,
    state: RunState,
    spec_hash: str = _hash("0a"),
    plan_hash: str = _hash("0b"),
) -> RunRecord:
    return RunRecord(
        id="run-id",
        target=target,
        state=state,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        metadata={},
        intent=RunIntent(target, "frappe-native", spec_hash, plan_hash),
    )


def _program(spec_hash: str = _hash("0a"), template_hash: str = _hash("0e")) -> VerificationProgram:
    return VerificationProgram(1, 16, spec_hash, template_hash, ())


class TestValidateSuccessorState:
    def test_accepts_running_successor(self):
        run = _run(_target(), RunState.RUNNING)
        lbve._validate_successor_state(run)  # does not raise

    @pytest.mark.parametrize("state", [s for s in RunState if s is not RunState.RUNNING])
    def test_rejects_non_running_successor(self, state):
        run = _run(_target(), state)
        with pytest.raises(lbve.LiveBackendVerificationError, match="RUNNING"):
            lbve._validate_successor_state(run)

    def test_rejects_untyped_successor(self):
        with pytest.raises(lbve.LiveBackendVerificationError, match="RunRecord"):
            lbve._validate_successor_state(object())  # type: ignore[arg-type]


class TestValidateBundleTarget:
    def test_accepts_matching_target(self, tmp_path):
        target = _target()
        bundle = _bundle(tmp_path, target)
        lbve._validate_bundle_target(bundle, target)  # does not raise

    def test_rejects_mismatched_target(self, tmp_path):
        bundle = _bundle(tmp_path, LockTarget("/other", "dev.local", "task_tracker"))
        with pytest.raises(lbve.LiveBackendVerificationError, match="target"):
            lbve._validate_bundle_target(bundle, _target())

    def test_rejects_untyped_inputs(self):
        with pytest.raises(lbve.LiveBackendVerificationError, match="typed"):
            lbve._validate_bundle_target(object(), _target())  # type: ignore[arg-type]


class TestValidateSuccessorIntent:
    def test_accepts_matching_intent(self, tmp_path):
        target = _target()
        bundle = _bundle(tmp_path, target)
        run = _run(target, RunState.RUNNING)
        lbve._validate_successor_intent(bundle, run)  # does not raise

    def test_rejects_spec_hash_drift(self, tmp_path):
        target = _target()
        bundle = _bundle(tmp_path, target)
        run = _run(target, RunState.RUNNING, spec_hash=_hash("ff"))
        with pytest.raises(lbve.LiveBackendVerificationError, match="intent"):
            lbve._validate_successor_intent(bundle, run)

    def test_rejects_plan_hash_drift(self, tmp_path):
        target = _target()
        bundle = _bundle(tmp_path, target)
        run = _run(target, RunState.RUNNING, plan_hash=_hash("ff"))
        with pytest.raises(lbve.LiveBackendVerificationError, match="intent"):
            lbve._validate_successor_intent(bundle, run)

    def test_rejects_schema_v1_bundle(self, tmp_path):
        target = _target()
        bundle = _bundle(tmp_path, target)
        object.__setattr__(bundle, "schema_version", 1)
        object.__setattr__(bundle, "successor", None)
        run = _run(target, RunState.RUNNING)
        with pytest.raises(lbve.LiveBackendVerificationError, match="schema-v2"):
            lbve._validate_successor_intent(bundle, run)


class TestValidateProgramBinding:
    def test_accepts_matching_spec_hash(self, tmp_path):
        bundle = _bundle(tmp_path)
        program = _program(spec_hash=bundle.reconfirmed_spec_hash)
        lbve._validate_program_binding(program, bundle)  # does not raise

    def test_rejects_mismatched_spec_hash(self, tmp_path):
        bundle = _bundle(tmp_path)
        program = _program(spec_hash=_hash("ff"))
        with pytest.raises(lbve.LiveBackendVerificationError, match="spec hash"):
            lbve._validate_program_binding(program, bundle)


class TestLoadProgramFromBundle:
    @patch("live_backend_verification_evidence.load_verification_program")
    @patch("live_backend_verification_evidence.generate_verification_templates")
    @patch("live_backend_verification_evidence.load_confirmed_compilation")
    @patch("live_backend_verification_evidence.load_confirmed_plan")
    @patch("live_backend_verification_evidence.load_confirmed_spec")
    def test_reconstructs_and_hash_checks_program(
        self,
        mock_load_spec,
        mock_load_plan,
        mock_load_compilation,
        mock_generate_templates,
        mock_load_program,
        tmp_path,
    ):
        bundle = _bundle(tmp_path)
        spec_hash = bundle.reconfirmed_spec_hash
        program = _program(spec_hash=spec_hash, template_hash="t" * 64)
        mock_load_program.return_value = program
        mock_generate_templates.return_value = MagicMock(
            artifacts={"harness/verification-plan.json": b'{"version": 1}'},
        )

        result = lbve._load_program_from_bundle(bundle)

        assert result is program
        mock_load_spec.assert_called_once_with(bundle.successor.confirmed_spec)
        mock_load_plan.assert_called_once_with(
            bundle.successor.confirmed_plan, mock_load_spec.return_value,
        )
        mock_load_compilation.assert_called_once_with(
            bundle.successor.confirmed_compilation, mock_load_spec.return_value,
        )
        mock_generate_templates.assert_called_once_with(mock_load_spec.return_value)
        mock_load_program.assert_called_once_with({"version": 1})

    @patch("live_backend_verification_evidence.load_verification_program")
    @patch("live_backend_verification_evidence.generate_verification_templates")
    @patch("live_backend_verification_evidence.load_confirmed_compilation")
    @patch("live_backend_verification_evidence.load_confirmed_plan")
    @patch("live_backend_verification_evidence.load_confirmed_spec")
    def test_rejects_hash_mismatch_after_loading(
        self,
        _mock_load_spec,
        _mock_load_plan,
        _mock_load_compilation,
        mock_generate_templates,
        mock_load_program,
        tmp_path,
    ):
        bundle = _bundle(tmp_path)
        program = _program(spec_hash=_hash("ff"))
        mock_load_program.return_value = program
        mock_generate_templates.return_value = MagicMock(
            artifacts={"harness/verification-plan.json": b"{}"},
        )
        with pytest.raises(lbve.LiveBackendVerificationError, match="spec hash"):
            lbve._load_program_from_bundle(bundle)


class TestEntityToDoctype:
    @pytest.mark.parametrize(
        ("entity", "doctype"),
        [
            ("project", "Project"),
            ("task", "Task"),
            ("task_tracker", "Task Tracker"),
        ],
    )
    def test_capitalizes_entity_names(self, entity, doctype):
        assert lbve._entity_to_doctype(entity) == doctype


class TestRecordName:
    def test_extracts_name_from_data_envelope(self):
        from frappe_harness.frappe_rest_provider import FrappeRestResult, RestReceipt
        result = FrappeRestResult(
            RestReceipt("create", 201, 0, "a" * 64), {"data": {"name": "TASK-1"}}, None,
        )
        assert lbve._record_name(result) == "TASK-1"

    def test_extracts_name_from_top_level_payload(self):
        from frappe_harness.frappe_rest_provider import FrappeRestResult, RestReceipt
        result = FrappeRestResult(
            RestReceipt("create", 201, 0, "a" * 64), {"name": "PROJ-1"}, None,
        )
        assert lbve._record_name(result) == "PROJ-1"

    def test_returns_none_for_non_mapping_payload(self):
        from frappe_harness.frappe_rest_provider import FrappeRestResult, RestReceipt
        result = FrappeRestResult(
            RestReceipt("create", 201, 0, "a" * 64), "not-a-mapping", None,
        )
        assert lbve._record_name(result) is None

    def test_returns_none_when_name_is_missing(self):
        from frappe_harness.frappe_rest_provider import FrappeRestResult, RestReceipt
        result = FrappeRestResult(
            RestReceipt("create", 201, 0, "a" * 64), {"data": {}}, None,
        )
        assert lbve._record_name(result) is None


class TestProvisionRunFixtures:
    def test_creates_baseline_and_delete_records(self):
        admin_rest = MagicMock()
        from frappe_harness.frappe_rest_provider import FrappeRestResult, RestReceipt
        admin_rest.create_document.side_effect = [
            FrappeRestResult(
                RestReceipt("create", 201, 0, "a" * 64), {"data": {"name": "PROJ-1"}}, None,
            ),
            FrappeRestResult(
                RestReceipt("create", 201, 0, "a" * 64), {"data": {"name": "TASK-1"}}, None,
            ),
            FrappeRestResult(
                RestReceipt("create", 201, 0, "a" * 64), {"data": {"name": "TASK-DEL-1"}}, None,
            ),
        ]
        template = VerificationFixtures(
            valid_payloads={"project": {"title": "p"}, "task": {"title": "t"}},
            record_names={},
            update_payloads={},
            delete_names={},
        )
        program = VerificationProgram(
            version=1,
            target_frappe_major=16,
            spec_hash=_hash("0a"),
            template_hash=_hash("0e"),
            cases=(ProgramCase(
                "permissions:task:delete:Task Manager",
                "permissions", "task", "delete", "Task Manager", "denied",
                {"permission": "delete"}, (),
            ),),
        )

        fixtures, created = lbve._provision_run_fixtures(admin_rest, template, program)

        assert fixtures.record_names == {"project": "PROJ-1", "task": "TASK-1"}
        assert fixtures.delete_names == {"permissions:task:delete:Task Manager": "TASK-DEL-1"}
        assert len(created) == 3
        assert created[0] == CreatedRecord("administrator", "project", "PROJ-1")
        admin_rest.create_document.assert_any_call("Project", {"title": "p"})
        admin_rest.create_document.assert_any_call("Task", {"title": "t"})

    def test_raises_when_baseline_creation_fails(self):
        admin_rest = MagicMock()
        from frappe_harness.frappe_rest_provider import FrappeRestResult, RestErrorKind, RestReceipt
        admin_rest.create_document.return_value = FrappeRestResult(
            RestReceipt("create", 403, 0, "a" * 64), None, RestErrorKind.PERMISSION,
        )
        template = VerificationFixtures(
            valid_payloads={"project": {"title": "p"}},
            record_names={},
            update_payloads={},
            delete_names={},
        )
        program = _program()
        with pytest.raises(lbve.LiveBackendVerificationError, match="fixture creation failed"):
            lbve._provision_run_fixtures(admin_rest, template, program)


class TestDeleteCreatedRecord:
    def test_counts_ok_response_as_cleaned(self):
        rest = MagicMock()
        from frappe_harness.frappe_rest_provider import FrappeRestResult, RestReceipt
        rest.delete_document.return_value = FrappeRestResult(
            RestReceipt("delete", 202, 0, "a" * 64), {"data": {"name": "TASK-1"}}, None,
        )
        record = CreatedRecord("administrator", "task", "TASK-1")
        assert lbve._delete_created_record(rest, record) is True
        rest.delete_document.assert_called_once_with("Task", "TASK-1")

    def test_counts_not_found_response_as_cleaned(self):
        rest = MagicMock()
        from frappe_harness.frappe_rest_provider import FrappeRestResult, RestErrorKind, RestReceipt
        rest.delete_document.return_value = FrappeRestResult(
            RestReceipt("delete", 404, 0, "a" * 64), None, RestErrorKind.NOT_FOUND,
        )
        record = CreatedRecord("administrator", "project", "PROJ-1")
        assert lbve._delete_created_record(rest, record) is True

    def test_counts_other_errors_as_not_cleaned(self):
        rest = MagicMock()
        from frappe_harness.frappe_rest_provider import FrappeRestResult, RestErrorKind, RestReceipt
        rest.delete_document.return_value = FrappeRestResult(
            RestReceipt("delete", 403, 0, "a" * 64), None, RestErrorKind.PERMISSION,
        )
        record = CreatedRecord("administrator", "task", "TASK-1")
        assert lbve._delete_created_record(rest, record) is False


class TestCleanup:
    def test_deletes_tasks_before_projects_and_cleans_users(self):
        admin_rest = MagicMock()
        admin_rest.delete_document.return_value = MagicMock(ok=True)

        probe = MagicMock()
        probe.cleanup_records = [
            CreatedRecord("administrator", "project", "PROJ-1"),
            CreatedRecord("administrator", "task", "TASK-1"),
        ]

        identity_provider = MagicMock()
        identities = [
            (MagicMock(), identity_provider),
            (MagicMock(), identity_provider),
        ]

        cleaned_records, cleaned_users = lbve._cleanup(admin_rest, probe, identities)

        assert cleaned_records == 2
        assert cleaned_users == 2
        # Task should be deleted before Project to avoid Link constraint errors.
        calls = admin_rest.delete_document.call_args_list
        from unittest.mock import call
        assert calls[0] == call("Task", "TASK-1")
        assert calls[1] == call("Project", "PROJ-1")
        assert identity_provider.cleanup.call_count == 2

    def test_continues_when_user_cleanup_fails(self):
        admin_rest = MagicMock()
        admin_rest.delete_document.return_value = MagicMock(ok=True)

        probe = MagicMock()
        probe.cleanup_records = []

        failing_provider = MagicMock()
        failing_provider.cleanup.side_effect = RuntimeError("cleanup failed")
        ok_provider = MagicMock()
        identities = [
            (MagicMock(), failing_provider),
            (MagicMock(), ok_provider),
        ]

        cleaned_records, cleaned_users = lbve._cleanup(admin_rest, probe, identities)

        assert cleaned_records == 0
        assert cleaned_users == 1


class TestRedactedSummary:
    def test_contains_only_secret_free_fields(self, tmp_path):
        bundle = _bundle(tmp_path)
        result = MagicMock()
        result.snapshot = MagicMock()
        result.report_record.id = "report-1"
        result.report.results = (MagicMock(), MagicMock())
        result.report.passed = True
        result.report.program_hash = "t" * 64

        summary = lbve._redacted_summary(
            successor_id="succ-1",
            result=result,
            bundle=bundle,
            created_count=3,
            user_count=2,
            cleaned_records=3,
            cleaned_users=2,
        )

        assert summary == {
            "successor_id": "succ-1",
            "parent_run_id": bundle.parent_run_id,
            "backend_verified": True,
            "report_id": "report-1",
            "case_count": 2,
            "passed": True,
            "spec_hash": bundle.reconfirmed_spec_hash,
            "plan_hash": bundle.successor.confirmed_plan.sha256,
            "template_hash": "t" * 64,
            "created_record_count": 3,
            "disposable_user_count": 2,
            "cleaned_record_count": 3,
            "cleaned_user_count": 2,
            "gate_approval_recorded": False,
        }
        text = json.dumps(summary)
        assert "password" not in text.lower()
        assert "cookie" not in text.lower()
        assert "sid=" not in text
        assert "@" not in text

    def test_reports_backend_verified_false_when_snapshot_is_none(self, tmp_path):
        bundle = _bundle(tmp_path)
        result = MagicMock()
        result.snapshot = None
        result.report_record.id = "report-1"
        result.report.results = ()
        result.report.passed = False
        result.report.program_hash = "t" * 64

        summary = lbve._redacted_summary(
            "succ-1", result, bundle, 0, 0, 0, 0,
        )
        assert summary["backend_verified"] is False


class TestMain:
    @patch("live_backend_verification_evidence._cleanup")
    @patch("live_backend_verification_evidence.RunBoundFrappeVerificationAdapter")
    @patch("live_backend_verification_evidence._derive_fixtures")
    @patch("live_backend_verification_evidence._build_actors")
    @patch("live_backend_verification_evidence.FrappeRestProvider")
    @patch("live_backend_verification_evidence._admin_session_cookie")
    @patch("live_backend_verification_evidence.load_confirmed_preflight")
    @patch("live_backend_verification_evidence._load_program_from_bundle")
    @patch("live_backend_verification_evidence.load_operator_input_bundle")
    @patch("live_backend_verification_evidence.RunStore")
    @patch.dict("os.environ", {"FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD": "secret"})
    def test_outputs_secret_free_summary_and_returns_zero_when_report_passes(
        self,
        mock_store_cls,
        mock_load_bundle,
        mock_load_program,
        _mock_load_preflight,
        _mock_admin_cookie,
        _mock_rest_provider,
        mock_build_actors,
        mock_derive_fixtures,
        mock_adapter_cls,
        mock_cleanup,
        tmp_path,
        capsys,
    ):
        target = _target()
        bundle = _bundle(tmp_path, target)
        program = _program(spec_hash=bundle.reconfirmed_spec_hash)
        successor = _run(target, RunState.RUNNING)
        object.__setattr__(successor, "id", "succ-1")

        mock_store = MagicMock()
        mock_store.get_run.return_value = successor
        mock_store_cls.return_value = mock_store
        mock_load_bundle.return_value = bundle
        mock_load_program.return_value = program

        identities = [(MagicMock(), MagicMock()), (MagicMock(), MagicMock())]
        mock_build_actors.return_value = ({"administrator": MagicMock()}, identities)
        mock_derive_fixtures.return_value = MagicMock()

        report = VerificationRunReport(
            program_hash=_hash("0e"),
            results=tuple(
                CaseResult(f"case-{i}", "success", "success", "passed", "expected_outcome")
                for i in range(82)
            ),
        )
        adapter = MagicMock()
        adapter.execute_backend.return_value = MagicMock()
        adapter.execute_backend.return_value.snapshot = MagicMock()
        adapter.execute_backend.return_value.report_record.id = "report-1"
        adapter.execute_backend.return_value.report = report
        adapter.cleanup_records = [MagicMock(), MagicMock()]
        mock_adapter_cls.return_value = adapter
        mock_cleanup.return_value = (82, 2)

        argv = [
            "--base-url", "http://localhost:8080",
            "--site", "dev.local",
            "--bench-path", "/bench",
            "--store", str(tmp_path / "store.sqlite3"),
            "--bundle", str(tmp_path / "bundle.json"),
            "--successor-id", "succ-1",
        ]
        exit_code = lbve.main(argv)

        assert exit_code == 0
        mock_store_cls.assert_called_once_with(Path(str(tmp_path / "store.sqlite3")))
        adapter.execute_backend.assert_called_once_with("succ-1", program=program)
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["successor_id"] == "succ-1"
        assert summary["passed"] is True
        assert summary["case_count"] == 82
        assert summary["cleaned_record_count"] == 82
        assert summary["cleaned_user_count"] == 2
        assert "secret" not in captured.out

    @patch.dict("os.environ", {}, clear=True)
    def test_exits_when_password_missing(self):
        with pytest.raises(SystemExit, match="FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD"):
            lbve.main([
                "--base-url", "http://localhost", "--site", "s",
                "--bench-path", "b", "--store", "/tmp/s", "--bundle", "/tmp/b",
            ])

    @patch("live_backend_verification_evidence._cleanup")
    @patch("live_backend_verification_evidence.RunBoundFrappeVerificationAdapter")
    @patch("live_backend_verification_evidence._derive_fixtures")
    @patch("live_backend_verification_evidence._build_actors")
    @patch("live_backend_verification_evidence.FrappeRestProvider")
    @patch("live_backend_verification_evidence._admin_session_cookie")
    @patch("live_backend_verification_evidence.load_confirmed_preflight")
    @patch("live_backend_verification_evidence._load_program_from_bundle")
    @patch("live_backend_verification_evidence.load_operator_input_bundle")
    @patch("live_backend_verification_evidence.RunStore")
    @patch.dict("os.environ", {"FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD": "secret"})
    def test_returns_two_when_report_fails(
        self,
        mock_store_cls,
        mock_load_bundle,
        mock_load_program,
        _mock_load_preflight,
        _mock_admin_cookie,
        _mock_rest_provider,
        mock_build_actors,
        mock_derive_fixtures,
        mock_adapter_cls,
        mock_cleanup,
        tmp_path,
        capsys,
    ):
        target = _target()
        bundle = _bundle(tmp_path, target)
        program = _program(spec_hash=bundle.reconfirmed_spec_hash)
        successor = _run(target, RunState.RUNNING)

        mock_store = MagicMock()
        mock_store.get_run.return_value = successor
        mock_store_cls.return_value = mock_store
        mock_load_bundle.return_value = bundle
        mock_load_program.return_value = program
        mock_build_actors.return_value = ({"administrator": MagicMock()}, [])
        mock_derive_fixtures.return_value = MagicMock()

        report = VerificationRunReport(
            program_hash=_hash("0e"),
            results=(
                CaseResult("case-1", "success", "permission_denied", "failed", "unexpected"),
            ),
        )
        adapter = MagicMock()
        adapter.execute_backend.return_value = MagicMock()
        adapter.execute_backend.return_value.snapshot = None
        adapter.execute_backend.return_value.report_record.id = "report-2"
        adapter.execute_backend.return_value.report = report
        adapter.cleanup_records = []
        mock_adapter_cls.return_value = adapter
        mock_cleanup.return_value = (0, 0)

        exit_code = lbve.main([
            "--base-url", "http://localhost:8080",
            "--site", "dev.local",
            "--bench-path", "/bench",
            "--store", str(tmp_path / "store.sqlite3"),
            "--bundle", str(tmp_path / "bundle.json"),
        ])

        assert exit_code == 2
        assert json.loads(capsys.readouterr().out)["passed"] is False

    @patch("live_backend_verification_evidence.RunStore")
    @patch.dict("os.environ", {"FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD": "secret"})
    def test_propagates_admission_errors_as_system_exit(self, mock_store_cls, tmp_path):
        mock_store = MagicMock()
        mock_store.get_run.side_effect = lbve.RunStoreError("store failure")
        mock_store_cls.return_value = mock_store
        bundle = _bundle(tmp_path)
        with patch(
            "live_backend_verification_evidence.load_operator_input_bundle", return_value=bundle,
        ):
            with pytest.raises(SystemExit, match="store failure"):
                lbve.main([
                    "--base-url", "http://localhost:8080",
                    "--site", "dev.local",
                    "--bench-path", "/bench",
                    "--store", str(tmp_path / "store.sqlite3"),
                    "--bundle", str(tmp_path / "bundle.json"),
                ])
