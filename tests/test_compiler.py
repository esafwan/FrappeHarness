from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from frappe_harness.compiler import CompilationError, compile_project
from frappe_harness.spec_io import load_project_spec


@dataclass(frozen=True)
class Field:
    field_id: str
    fieldname: str
    label: str
    fieldtype: str
    required: bool = False
    unique: bool = False
    default: object = None
    read_only: bool = False
    hidden: bool = False
    description: str | None = None
    in_list_view: bool = False
    in_standard_filter: bool = False
    options: object = None
    position: int = 0


@dataclass(frozen=True)
class Entity:
    entity_id: str
    doctype_name: str
    label: str
    description: str
    title_field: str
    fields: tuple[Field, ...]
    search_fields: tuple[str, ...] = ()
    list_columns: tuple[str, ...] = ()
    standard_filters: tuple[str, ...] = ()
    default_sort_field: str = "modified"
    default_sort_order: str = "DESC"
    track_changes: bool = True


@dataclass(frozen=True)
class Role:
    role_name: str
    description: str
    permissions: dict[str, dict[str, bool]]


@dataclass(frozen=True)
class ProjectSpec:
    project_id: str
    spec_version: str
    app_slug: str
    app_title: str
    module_name: str
    publisher: str
    publisher_email: str
    target_site: str
    entities: tuple[Entity, ...]
    roles: tuple[Role, ...]


def task_tracker() -> ProjectSpec:
    project = Entity(
        "project", "Project", "Project", "A work project", "project_name",
        (Field("project_name", "project_name", "Project Name", "Data", required=True, position=0, in_list_view=True),),
        ("project_name",), ("project_name",), (),
    )
    task = Entity(
        "task", "Task", "Task", "A work task", "subject",
        (
            Field("status", "status", "Status", "Select", default="Open", options=("Open", "Closed"), position=2, in_standard_filter=True),
            Field("project", "project", "Project", "Link", options="Project", position=1, required=True),
            Field("subject", "subject", "Subject", "Data", required=True, position=0, in_list_view=True),
        ),
        ("subject",), ("subject", "status"), ("status",),
    )
    manager = Role("Task Manager", "May manage work", {"Project": {"read": True, "write": True, "create": True, "delete": True}, "Task": {"read": True, "write": True, "create": True, "delete": True}})
    user = Role("Task User", "May work tasks", {"Project": {"read": True}, "Task": {"read": True, "write": True, "create": True}})
    return ProjectSpec("task_tracker", "1", "task_tracker", "Task Tracker", "Task Tracker", "Harness", "harness@example.test", "dev.local", (task, project), (user, manager))


def test_task_tracker_compile_is_byte_stable_and_owns_every_artifact() -> None:
    first = compile_project(task_tracker())
    second = compile_project(task_tracker())
    assert first.artifacts == second.artifacts
    assert first.spec_hash == second.spec_hash
    manifest = json.loads(first.text("task_tracker/harness/ownership-manifest.json"))
    assert manifest["spec_hash"] == first.spec_hash
    assert "task_tracker/task_tracker/api.py" in first.artifacts
    assert "frontend_manifest" in first.text("task_tracker/task_tracker/api.py")
    assert [entry["path"] for entry in manifest["owned_files"]] == sorted(path for path in first.artifacts if path != "task_tracker/harness/ownership-manifest.json")


def test_frappe_doctype_json_is_structured_and_permissioned() -> None:
    result = compile_project(task_tracker())
    assert result.artifacts["task_tracker/task_tracker/__init__.py"] == b'__version__ = "0.0.1"\n'
    assert result.artifacts["task_tracker/task_tracker/task_tracker/__init__.py"] == b""
    assert result.artifacts["task_tracker/task_tracker/task_tracker/doctype/__init__.py"] == b""
    assert result.artifacts["task_tracker/task_tracker/task_tracker/doctype/task/__init__.py"] == b""
    task_json = json.loads(result.text("task_tracker/task_tracker/task_tracker/doctype/task/task.json"))
    assert task_json["name"] == "Task"
    assert task_json["module"] == "Task Tracker"
    assert task_json["field_order"] == ["subject", "project", "status"]
    assert task_json["fields"][1]["options"] == "Project"
    assert task_json["fields"][2]["options"] == "Open\nClosed"
    by_role = {row["role"]: row for row in task_json["permissions"]}
    assert by_role["Task User"]["delete"] == 0
    assert by_role["Task Manager"]["delete"] == 1


def test_typed_golden_fixture_renders_the_section_19_metadata() -> None:
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    result = compile_project(spec)
    project = json.loads(result.text("task_tracker/task_tracker/task_tracker/doctype/project/project.json"))
    task = json.loads(result.text("task_tracker/task_tracker/task_tracker/doctype/task/task.json"))

    assert project["title_field"] == "title"
    assert project["search_fields"] == "title"
    assert project["sort_field"] == "title"
    assert project["field_order"] == ["title", "description", "active"]

    assert task["title_field"] == "title"
    assert task["search_fields"] == "title"
    assert task["field_order"] == ["title", "description", "project", "status", "priority", "due_date"]
    by_name = {field["fieldname"]: field for field in task["fields"]}
    assert by_name["description"]["fieldtype"] == "Text Editor"
    assert by_name["project"]["options"] == "Project"
    assert by_name["status"]["options"] == "Open\nIn Progress\nCompleted"
    assert by_name["priority"]["default"] == "Medium"
    assert all(by_name[name]["in_list_view"] == 1 for name in ("title", "project", "status", "priority", "due_date"))
    assert all(by_name[name]["in_standard_filter"] == 1 for name in ("project", "status", "priority"))


def test_module_name_derives_the_frappe_module_package_path() -> None:
    spec = task_tracker()
    # Legacy callers may use the display module name.  The generated DocType
    # package must use Frappe's scrubbed import-path form while the metadata
    # continues to identify the display module exactly.
    result = compile_project(ProjectSpec(**{**spec.__dict__, "module_name": "Work Tracker"}))

    assert "task_tracker/task_tracker/work_tracker/doctype/task/task.json" in result.artifacts
    assert "task_tracker/task_tracker/doctype/task/task.json" not in result.artifacts
    assert json.loads(result.text("task_tracker/task_tracker/work_tracker/doctype/task/task.json"))["module"] == "Work Tracker"


def test_mapping_contract_is_accepted_and_order_does_not_affect_output() -> None:
    spec = task_tracker()
    mapping = {
        "project_id": spec.project_id, "spec_version": spec.spec_version, "app_slug": spec.app_slug,
        "app_title": spec.app_title, "module_name": spec.module_name, "publisher": spec.publisher,
        "publisher_email": spec.publisher_email, "target_site": spec.target_site,
        "entities": [
            {**entity.__dict__, "fields": [field.__dict__ for field in reversed(entity.fields)]}
            for entity in reversed(spec.entities)
        ],
        "roles": [role.__dict__ for role in reversed(spec.roles)],
    }
    mapped = compile_project(mapping)
    typed = compile_project(spec)
    # Renderer ordering is deterministic even if equivalent legacy input uses
    # a different declaration order. The persisted spec snapshot/hash remains
    # intentionally different: confirmation identity preserves typed input.
    generated_paths = [path for path in typed.artifacts if not path.startswith("task_tracker/harness/")]
    assert {path: mapped.artifacts[path] for path in generated_paths} == {
        path: typed.artifacts[path] for path in generated_paths
    }


@pytest.mark.parametrize(
    ("field", "message"),
    [
        (Field("bad", "bad", "Bad", "Table", position=4), "Unsupported field type"),
        (Field("bad", "bad", "Bad", "Time", position=4), "Unsupported field type"),
        (Field("bad", "bad", "Bad", "Password", position=4), "Unsupported field type"),
        (Field("bad", "bad", "Bad", "JSON", position=4), "Unsupported field type"),
        (Field("bad", "bad", "Bad", "Link", options="Unknown", position=4), "must target a declared"),
        (Field("bad", "bad", "Bad", "Select", options=("A", "A"), position=4), "duplicate options"),
    ],
)
def test_unsupported_or_unsafe_field_metadata_is_rejected(field: Field, message: str) -> None:
    spec = task_tracker()
    task = spec.entities[0]
    replacement = Entity(**{**task.__dict__, "fields": (*task.fields, field)})
    invalid = ProjectSpec(**{**spec.__dict__, "entities": (replacement, *spec.entities[1:])})
    with pytest.raises(CompilationError, match=message):
        compile_project(invalid)
