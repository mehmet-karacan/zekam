"""RAG/router probe fixture kabulu (RAG26-R09): 14 probe, offline ve kaynaga dogrulamali."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from zekam.application.request_routing import (
    RegisteredProject,
    load_project_families,
    route_request,
)
from zekam.application.secret_detection import scan_text
from zekam.domain.retrieval import extract_identifiers

FIXTURE = Path(__file__).parents[1] / "fixtures" / "rag_router_probe_v1.json"
SOURCE_ROOT = Path(os.environ.get("ZEKAM_PROBE_GPU_FUSION_ROOT", r"C:\innova\projeler\gpu-fusion"))
PROJECTS = (
    RegisteredProject("gpu-fusion", ("gpu",)),
    RegisteredProject("sky-spring-ui", ("sky-ui",)),
    RegisteredProject("sky-microservis", ("sky-backend",)),
)

_doc: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
_cases: list[dict[str, Any]] = _doc["cases"]
_by_id = {case["id"]: case for case in _cases}
_needs_source = pytest.mark.skipif(
    not (SOURCE_ROOT / ".git").exists(), reason="gpu-fusion kaynak koku bu makinede yok"
)


def _gold(case: dict[str, Any]) -> dict[str, Any]:
    gold = case["gold"]
    return _by_id[gold["same_as"]]["gold"] if "same_as" in gold else gold


def _read(relative: str) -> str:
    return (SOURCE_ROOT / relative).read_text(encoding="utf-8", errors="replace")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(SOURCE_ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


@pytest.mark.unit
def test_fixture_covers_the_fourteen_probes_with_unique_ids() -> None:
    ids = [case["id"] for case in _cases]
    assert len(ids) == 14 and len(set(ids)) == 14
    assert {"Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8", "Q9", "Q10"} <= set(ids)
    assert {"Q1-TR-CURLY", "Q5-TR", "Q6-TR", "Q10-NUMBER"} <= set(ids)
    assert _doc["grants_authority"] is False
    assert len(_doc["source_revision"]) == 40


@pytest.mark.unit
def test_q5_and_q6_are_distinct_questions_not_a_diacritic_pair() -> None:
    assert _by_id["Q5"]["question"] != _by_id["Q6"]["question"]
    assert _gold(_by_id["Q5"])["git_commit_prefixes"] != _gold(_by_id["Q6"])["git_commit_prefixes"]
    assert _gold(_by_id["Q5-TR"]) == _gold(_by_id["Q5"])
    assert _gold(_by_id["Q6-TR"]) == _gold(_by_id["Q6"])


@pytest.mark.unit
@pytest.mark.parametrize("case", _cases, ids=[case["id"] for case in _cases])
def test_route_expectations_hold_offline(case: dict[str, Any]) -> None:
    expected = case["route"]
    if expected is None:
        return  # proje adi yok: harness `--project` ile kapsam verir, router denenmez.
    route = route_request(
        case["question"], catalog=load_project_families(), registered_projects=PROJECTS
    )
    assert route.status == expected["status"]
    assert route.intent == expected["intent"]
    assert route.strategy == expected["strategy"]
    assert list(route.project_refs) == expected["project_refs"]


@pytest.mark.unit
@pytest.mark.parametrize("case", _cases, ids=[case["id"] for case in _cases])
def test_no_question_produces_a_word_internal_apostrophe_phrase(case: dict[str, Any]) -> None:
    for identifier in extract_identifiers(case["question"]):
        assert " " not in identifier or identifier in case["question"]
        assert not identifier.startswith(("inde ", "da ", "lari"))


@pytest.mark.unit
def test_port_number_probe_is_not_a_jira_reference() -> None:
    route = route_request(
        _by_id["Q10-NUMBER"]["question"],
        catalog=load_project_families(),
        registered_projects=PROJECTS,
    )
    assert route.jira_issue_key is None
    assert route.intent == "project-question"


@_needs_source
@pytest.mark.unit
def test_gold_revision_is_an_ancestor_of_the_local_source_head() -> None:
    revision = _doc["source_revision"]
    assert (
        subprocess.run(
            ["git", "-C", str(SOURCE_ROOT), "merge-base", "--is-ancestor", revision, "HEAD"],
            check=False,
        ).returncode
        == 0
    )


@_needs_source
@pytest.mark.unit
@pytest.mark.parametrize("case", _cases, ids=[case["id"] for case in _cases])
def test_gold_tokens_and_files_exist_in_the_local_source(case: dict[str, Any]) -> None:
    gold = _gold(case)
    files = gold.get("files", [])
    for relative in (*files, *gold.get("profile_files", [])):
        assert (SOURCE_ROOT / relative).is_file(), relative
    tokens = gold.get("tokens", [])
    if tokens:
        corpus = "\n".join(_read(relative) for relative in files)
        for token in tokens:
            assert token in corpus, (case["id"], token)
    ordered = gold.get("ordered_tokens", [])
    if ordered:
        text = _read(files[0])
        chain = [
            re.search(rf"\.(?:start|next)\s*\(\s*{re.escape(name)}\w*Step\(\)", text)
            for name in ordered
        ]
        assert all(chain), "step .start/.next zincirinde yok"
        positions = [match.start() for match in chain if match]
        assert positions == sorted(positions), "step sirasi kaynaktaki .start/.next sirasi degil"


@_needs_source
@pytest.mark.unit
def test_q1_job_names_are_defined_across_the_job_config_files() -> None:
    gold = _gold(_by_id["Q1"])
    corpus = "\n".join(_read(relative) for relative in gold["files"])
    for token in gold["tokens"]:
        assert f'JobBuilder("{token}"' in corpus, token


@_needs_source
@pytest.mark.unit
def test_q4_commits_carry_the_issue_key_and_are_separate_changes() -> None:
    gold = _gold(_by_id["Q4"])
    for prefix in gold["git_commit_prefixes"]:
        subject = _git("log", "-1", "--format=%s", prefix).strip()
        assert gold["issue_key"] in subject
    assert len(set(gold["git_commit_prefixes"])) == 2


@_needs_source
@pytest.mark.unit
def test_q7_negative_evidence_has_no_messaging_listeners_in_source() -> None:
    gold = _gold(_by_id["Q7"])
    for token in gold["absent_tokens"]:
        found = subprocess.run(
            ["git", "-C", str(SOURCE_ROOT), "grep", "-l", token],
            capture_output=True,
            text=True,
            check=False,
        )
        assert found.stdout.strip() == "", token


@_needs_source
@pytest.mark.unit
def test_q10_config_files_are_excluded_from_the_index_by_the_secret_scan() -> None:
    """Q10'un gold'u config dosyalarinda; bu dosyalar sifre kalibi tasidigi icin indekslenmez."""
    gold = _gold(_by_id["Q10"])
    assert _by_id["Q10"]["index_policy"] == "excluded-by-secret-scan"
    flagged = [
        relative
        for relative in gold["profile_files"]
        if scan_text(_read(relative), relative_path=relative)
    ]
    assert flagged, "hicbir profil dosyasi secret taramasina takilmiyor; politika notu guncel degil"
