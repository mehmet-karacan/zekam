"""Logical resource ayristirma ve kilit catisma kurallari."""

from __future__ import annotations

import pytest

from zekam.domain.errors import ValidationFailed
from zekam.domain.resources import (
    LockMode,
    LogicalResource,
    ResourceRequest,
    conflicting_pairs,
    conflicts,
    has_internal_conflict,
    lock_order,
    parse_requests,
)

pytestmark = pytest.mark.unit


def read(text: str) -> ResourceRequest:
    return ResourceRequest.parse(text, LockMode.READ)


def write(text: str) -> ResourceRequest:
    return ResourceRequest.parse(text, LockMode.WRITE)


# -- ayristirma ---------------------------------------------------------------------


def test_project_resource_is_parsed() -> None:
    resource = LogicalResource.parse("project:zekam")
    assert resource.kind == "project"
    assert resource.project == "zekam"
    assert resource.text == "project:zekam"


def test_path_resource_keeps_subpath() -> None:
    resource = LogicalResource.parse("path:zekam:src/zekam/domain/work.py")
    assert resource.rest == "src/zekam/domain/work.py"
    assert resource.path_parts == ("src", "zekam", "domain", "work.py")


def test_db_object_resource_keeps_full_rest() -> None:
    resource = LogicalResource.parse("db-object:zekam:postgresql:table:payments")
    assert resource.rest == "postgresql:table:payments"
    assert resource.project == "zekam"


def test_global_resource_has_no_project() -> None:
    assert LogicalResource.parse("provider:anthropic:messages").project is None
    assert LogicalResource.parse("model-benchmark:opus:genel").project is None


def test_path_is_normalized() -> None:
    assert LogicalResource.parse("path:zekam:./src//a.py").rest == "src/a.py"


@pytest.mark.parametrize(
    "text",
    [
        "path:zekam:/mutlak/yol",
        "path:zekam:C:/Users/kisi",
        "path:zekam:../disari",
        "path:zekam:src/../../disari",
        "path:zekam:src\\a.py",
    ],
)
def test_unsafe_paths_are_rejected(text: str) -> None:
    with pytest.raises(ValidationFailed):
        LogicalResource.parse(text)


@pytest.mark.parametrize("text", ["", "   ", "bilinmeyen:zekam", "path", "path:zekam:"])
def test_invalid_resources_are_rejected(text: str) -> None:
    with pytest.raises(ValidationFailed):
        LogicalResource.parse(text)


def test_resource_with_space_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        LogicalResource.parse("path:zekam:src/a b.py")


# -- catisma kurallari ---------------------------------------------------------------


def test_two_reads_never_conflict() -> None:
    assert not conflicts(read("path:zekam:a.py"), read("path:zekam:a.py"))
    assert not conflicts(read("project:zekam"), read("path:zekam:a.py"))


def test_same_resource_with_a_write_conflicts() -> None:
    assert conflicts(write("path:zekam:a.py"), read("path:zekam:a.py"))
    assert conflicts(write("path:zekam:a.py"), write("path:zekam:a.py"))


def test_different_paths_do_not_conflict() -> None:
    assert not conflicts(write("path:zekam:a.py"), write("path:zekam:b.py"))


def test_parent_and_child_paths_conflict() -> None:
    assert conflicts(write("path:zekam:src"), read("path:zekam:src/a.py"))
    assert conflicts(read("path:zekam:src"), write("path:zekam:src/a.py"))


def test_similar_prefix_is_not_a_child() -> None:
    # `src2` bir `src` alt dizini degildir.
    assert not conflicts(write("path:zekam:src"), write("path:zekam:src2/a.py"))


def test_project_write_conflicts_with_everything_in_the_project() -> None:
    assert conflicts(write("project:zekam"), read("path:zekam:a.py"))
    assert conflicts(write("project:zekam"), read("work:zekam:123"))
    assert conflicts(read("project:zekam"), write("db-object:zekam:postgresql:table:t"))


def test_different_projects_do_not_conflict() -> None:
    assert not conflicts(write("project:zekam"), write("project:gpu"))
    assert not conflicts(write("path:zekam:a.py"), write("path:gpu:a.py"))


def test_global_resources_conflict_only_on_exact_match() -> None:
    assert conflicts(write("provider:anthropic:messages"), read("provider:anthropic:messages"))
    assert not conflicts(write("provider:anthropic:messages"), read("provider:anthropic:embed"))


def test_db_objects_conflict_only_on_exact_match() -> None:
    first = write("db-object:zekam:postgresql:table:payments")
    second = read("db-object:zekam:postgresql:table:orders")
    assert not conflicts(first, second)
    assert conflicts(first, read("db-object:zekam:postgresql:table:payments"))


# -- yardimcilar ----------------------------------------------------------------------


def test_conflicting_pairs_reports_every_clash() -> None:
    wanted = [write("path:zekam:src/a.py"), write("path:zekam:baska.py")]
    held = [read("path:zekam:src")]
    pairs = conflicting_pairs(wanted, held)
    assert len(pairs) == 1
    assert pairs[0][0].resource.text == "path:zekam:src/a.py"


def test_internal_conflict_is_detected() -> None:
    assert has_internal_conflict([write("path:zekam:src"), write("path:zekam:src/a.py")])
    assert not has_internal_conflict([write("path:zekam:a.py"), write("path:zekam:b.py")])


def test_lock_order_is_stable_and_lexical() -> None:
    requests = [write("path:zekam:z.py"), read("path:zekam:a.py"), write("project:zekam")]
    ordered = lock_order(requests)
    assert [item.resource.text for item in ordered] == [
        "path:zekam:a.py",
        "path:zekam:z.py",
        "project:zekam",
    ]
    assert lock_order(ordered) == ordered


def test_parse_requests_marks_modes() -> None:
    requests = parse_requests(read=("path:zekam:a.py",), write=("path:zekam:b.py",))
    modes = {item.resource.text: item.mode for item in requests}
    assert modes["path:zekam:a.py"] is LockMode.READ
    assert modes["path:zekam:b.py"] is LockMode.WRITE


def test_request_reports_write_flag() -> None:
    assert write("path:zekam:a.py").is_write
    assert not read("path:zekam:a.py").is_write
