import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from frappe_harness.contracts import (
    EntitySpec,
    FieldSpec,
    FieldType,
    PermissionSpec,
    ProjectSpec,
    RoleSpec,
    ScreenKind,
    ScreenSpec,
    canonical_json,
    normalize_identifier,
    spec_hash,
    validate_project_spec,
)


def task_tracker() -> ProjectSpec:
    roles = (RoleSpec("Task User"), RoleSpec("Task Manager"))
    project_permissions = (
        PermissionSpec("Task User", read=True),
        PermissionSpec("Task Manager", read=True, create=True, write=True, delete=True),
    )
    task_permissions = (
        PermissionSpec("Task User", read=True, create=True, write=True),
        PermissionSpec("Task Manager", read=True, create=True, write=True, delete=True),
    )
    return ProjectSpec(
        name="task_tracker",
        label="Task Tracker",
        module="task_tracker",
        roles=roles,
        entities=(
            EntitySpec("project", "Project", (FieldSpec("title", "Title", FieldType.DATA, required=True),), project_permissions),
            EntitySpec(
                "task", "Task", (
                    FieldSpec("title", "Title", FieldType.DATA, required=True),
                    FieldSpec("project", "Project", FieldType.LINK, required=True, options=("project",)),
                    FieldSpec("status", "Status", FieldType.SELECT, default="Open", options=("Open", "Done")),
                ), task_permissions,
            ),
        ),
        screens=(
            ScreenSpec("project_list", "project", ScreenKind.LIST, "Projects", ("title",)),
            ScreenSpec("task_form", "task", ScreenKind.FORM, "Task", ("title", "project", "status")),
        ),
    )


class ContractsTest(unittest.TestCase):
    def test_golden_task_tracker_is_valid(self):
        report = validate_project_spec(task_tracker())
        self.assertTrue(report.valid, report.issues)

    def test_canonical_serialization_and_hash_are_stable(self):
        spec = task_tracker()
        self.assertEqual(canonical_json(spec), canonical_json(spec))
        self.assertEqual(spec_hash(spec), spec_hash(spec))
        self.assertIn('"field_type":"Data"', canonical_json(spec))

    def test_identifier_normalization_is_deterministic(self):
        self.assertEqual(normalize_identifier(" Task Tracker! "), "task_tracker")
        self.assertEqual(normalize_identifier("99 Bottles"), "app_99_bottles")

    def test_rejects_bad_identifier_and_incomplete_permissions(self):
        spec = task_tracker()
        bad_entity = EntitySpec("Bad Entity", "", spec.entities[0].fields, (PermissionSpec("Task User", read=True),))
        report = validate_project_spec(ProjectSpec("bad name", "", "bad module", (bad_entity,), spec.roles, ()))
        codes = {issue.code for issue in report.errors}
        self.assertTrue({"invalid_identifier", "missing_label", "incomplete_permissions"} <= codes)

    def test_rejects_select_and_link_errors(self):
        spec = task_tracker()
        entity = EntitySpec("thing", "Thing", (
            FieldSpec("choice", "Choice", FieldType.SELECT, default="x"),
            FieldSpec("parent", "Parent", FieldType.LINK, options=("missing",)),
        ), spec.entities[0].permissions)
        report = validate_project_spec(ProjectSpec("thing_app", "Thing App", "thing_app", (entity,), spec.roles, ()))
        codes = {issue.code for issue in report.errors}
        self.assertIn("select_options_required", codes)
        self.assertIn("invalid_select_default", codes)
        self.assertIn("unknown_link_target", codes)

    def test_rejects_required_link_cycle(self):
        spec = task_tracker()
        permissions = spec.entities[0].permissions
        a = EntitySpec("a", "A", (FieldSpec("b", "B", FieldType.LINK, required=True, options=("b",)),), permissions)
        b = EntitySpec("b", "B", (FieldSpec("a", "A", FieldType.LINK, required=True, options=("a",)),), permissions)
        report = validate_project_spec(ProjectSpec("cycle_app", "Cycle", "cycle_app", (a, b), spec.roles, ()))
        self.assertIn("required_link_cycle", {issue.code for issue in report.errors})

    def test_rejects_unknown_screen_fields(self):
        spec = task_tracker()
        screen = ScreenSpec("bad_screen", "project", ScreenKind.LIST, "Bad", ("does_not_exist",))
        changed = ProjectSpec(spec.name, spec.label, spec.module, spec.entities, spec.roles, (screen,))
        self.assertIn("unknown_screen_field", {issue.code for issue in validate_project_spec(changed).errors})

    def test_accepts_email_and_phone_but_rejects_incompatible_defaults(self):
        spec = task_tracker()
        entity = EntitySpec(
            "contact",
            "Contact",
            (
                FieldSpec("email", "Email", FieldType.EMAIL, default="person@example.test"),
                FieldSpec("phone", "Phone", FieldType.PHONE, default="+1 555 0100"),
                FieldSpec("bad_text", "Bad text", FieldType.DATA, default=True),
                FieldSpec("bad_check", "Bad check", FieldType.CHECK, default="false"),
                FieldSpec("also_bad_check", "Also bad check", FieldType.CHECK, default=1),
            ),
            spec.entities[0].permissions,
        )
        report = validate_project_spec(ProjectSpec("contact_app", "Contact App", "contact_app", (entity,), spec.roles, ()))
        codes = {issue.code for issue in report.errors}
        self.assertNotIn("unsupported_field_type", codes)
        self.assertIn("invalid_string_default", codes)
        self.assertIn("invalid_check_default", codes)

    def test_rejects_reserved_names_and_more_than_twenty_stored_fields(self):
        spec = task_tracker()
        fields = tuple(FieldSpec(f"field_{index}", f"Field {index}", FieldType.DATA) for index in range(21))
        entity = EntitySpec("oversized", "Oversized", fields, spec.entities[0].permissions)
        reserved = EntitySpec("reserved", "Reserved", (FieldSpec("owner", "Owner", FieldType.DATA),), spec.entities[0].permissions)
        report = validate_project_spec(ProjectSpec("limits_app", "Limits", "limits_app", (entity, reserved), spec.roles, ()))
        codes = {issue.code for issue in report.errors}
        self.assertIn("field_count", codes)
        self.assertIn("reserved_field_name", codes)

    def test_rejects_hidden_required_and_invalid_entity_field_references(self):
        spec = task_tracker()
        permissions = spec.entities[0].permissions
        entity = EntitySpec(
            "entry",
            "Entry",
            (
                FieldSpec("title", "Title", FieldType.DATA, required=True, hidden=True),
                FieldSpec("status", "Status", FieldType.SELECT, options=("Open", " open ")),
                FieldSpec("amount", "Amount", FieldType.CURRENCY),
            ),
            permissions,
            title_field="amount",
            list_columns=("title", "missing"),
            search_fields=("amount",),
            standard_filters=("missing",),
        )
        report = validate_project_spec(ProjectSpec("entry_app", "Entry App", "entry_app", (entity,), spec.roles, ()))
        codes = {issue.code for issue in report.errors}
        self.assertTrue({"required_hidden_without_default", "duplicate_select_option", "invalid_title_field", "hidden_entity_field", "unknown_entity_field", "unsuitable_search_field"} <= codes)

    def test_rejects_link_when_selecting_role_cannot_read_generated_target(self):
        roles = (RoleSpec("Editor"),)
        source_permissions = (PermissionSpec("Editor", create=True, write=True),)
        target_permissions = (PermissionSpec("Editor", read=False),)
        target = EntitySpec("target", "Target", (FieldSpec("title", "Title", FieldType.DATA),), target_permissions)
        source = EntitySpec("source", "Source", (FieldSpec("target", "Target", FieldType.LINK, options=("target",)),), source_permissions)
        report = validate_project_spec(ProjectSpec("links_app", "Links", "links_app", (target, source), roles, ()))
        self.assertTrue(report.valid)
        self.assertIn("link_target_not_readable", {issue.code for issue in report.issues})


if __name__ == "__main__":
    unittest.main()
