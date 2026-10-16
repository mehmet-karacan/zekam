"""Bounded full test-tree evidence captured around the actual Maven process."""

from zekam.application.knowledge_file_plane import assert_public_safe_projection
from zekam.domain.canonical import canonical_bytes, parse_digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.unit_test_engineering import normalize_relative_source
from zekam.infrastructure.unit_test_runner.tree_fingerprint import (
    fingerprint_tree,
    MAX_TEST_TREE_FILES,
)

CONTRACT = "zekam-unit-test-measured-tree/v1"


def measured_tree(before, after, modules):
    body = {
        "contract": CONTRACT,
        "module_dirs": list(modules),
        "files": [list(pair) for pair in sorted(before.items())],
        "before_digest": fingerprint_tree(before),
        "after_digest": None if after is None else fingerprint_tree(after),
        "state": "readback-unavailable"
        if after is None
        else "stable"
        if before == after
        else "drifted",
        "grants_authority": False,
    }
    validate_measured_tree(body)
    return body


def validate_measured_tree(body):
    if not isinstance(body, dict) or set(body) != {
        "contract",
        "module_dirs",
        "files",
        "before_digest",
        "after_digest",
        "state",
        "grants_authority",
    }:
        raise PolicyViolation("Measured test tree exact object required")
    modules, files = body["module_dirs"], body["files"]
    if body["contract"] != CONTRACT or body["grants_authority"] is not False:
        raise PolicyViolation("Measured test tree contract drift")
    if (
        not isinstance(modules, list)
        or not 0 < len(modules) <= 128
        or any(not isinstance(m, str) for m in modules)
        or len(set(modules)) != len(modules)
    ):
        raise PolicyViolation("Measured test tree module budget/identity invalid")
    for module in modules:
        if module:
            normalize_relative_source(module)
    if not isinstance(files, list) or len(files) > MAX_TEST_TREE_FILES:
        raise PolicyViolation("Measured test tree file budget exceeded")
    tree = {}
    for pair in files:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], str)
        ):
            raise PolicyViolation("Measured test tree exact path/digest required")
        path, value = pair
        normalize_relative_source(path)
        parse_digest(value)
        if path in tree or not any(
            path.startswith((module + "/" if module else "") + "src/test/") for module in modules
        ):
            raise PolicyViolation("Measured test tree path scope/duplicate drift")
        tree[path] = value
    if files != sorted(files) or fingerprint_tree(tree) != body["before_digest"]:
        raise PolicyViolation("Measured test tree content fingerprint drift")
    if body["after_digest"] is not None:
        parse_digest(body["after_digest"])
    if (
        body["state"] == "stable"
        and body["after_digest"] == body["before_digest"]
        or body["state"] == "drifted"
        and body["after_digest"] is not None
        and body["after_digest"] != body["before_digest"]
        or body["state"] == "readback-unavailable"
        and body["after_digest"] is None
    ):
        assert_public_safe_projection(
            canonical_bytes(body), relative_path="measured-test-tree.json"
        )
        return tree
    raise PolicyViolation("Measured test tree state drift")
