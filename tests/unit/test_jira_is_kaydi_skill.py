from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from zekam.application.skill_packages import build_projection_plan
from zekam.domain.client_integration import ClientIntegrationPolicy
from zekam.domain.skill_package import SkillPackage

sys.dont_write_bytecode = True

ROOT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "zekam"
    / "skills"
    / "jira-is-kaydi"
)


def _module(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _reference(tmp_path: Path) -> Path:
    target = tmp_path / "JIRA_FORMAT_REFERENCE.md"
    target.write_bytes((ROOT / "references" / target.name).read_bytes())
    return target


def _valid_html(refresh: ModuleType, *, footer: str = "") -> bytes:
    headings = "".join(
        f"<h2>{name.title()}</h2><p>Notation Comment {name} örneği</p>"
        for name in refresh.EXPECTED_SECTIONS
    )
    syntax = (
        "<h1>Text Formatting Notation Help</h1>"
        "<p>h1. Biggest heading</p><p>*strong*</p><p>[#anchor]</p>"
        "<p>||heading 1||heading 2||</p><p>{noformat}</p><p>\\X</p>"
    )
    return (
        f"<html><body>{syntax}{headings}<pre>SELECT 1\n  FROM DUAL;</pre>"
        f"<footer>{footer}</footer></body></html>"
    ).encode()


@pytest.fixture
def content() -> ModuleType:
    return _module("jira_skill_content", "scripts/jira_content.py")


@pytest.fixture
def refresh() -> ModuleType:
    return _module("jira_skill_refresh", "scripts/refresh_jira_format_reference.py")


def test_skill_package_is_portable_and_permission_free() -> None:
    package = SkillPackage.read_directory(ROOT.resolve())

    assert package.name == "jira-is-kaydi"
    assert package.declared_allowed_tools is None
    assert set(package.files) == {
        "SKILL.md",
        "references/JIRA_FORMAT_REFERENCE.md",
        "references/kurum-icerik-standardi.md",
        "references/ortam-profili.md",
        "scripts/jira_content.py",
        "scripts/refresh_jira_format_reference.py",
    }
    assert package.package_digest.startswith("sha256:")


def test_skill_projects_to_opencode_codex_and_claude_code(tmp_path: Path) -> None:
    package = SkillPackage.read_directory(ROOT.resolve())
    project = (tmp_path / "project").resolve()
    project.mkdir()
    plan = build_projection_plan(
        project,
        package,
        policy=ClientIntegrationPolicy(opencode=True, codex=True, claude_code=True),
    )

    assert [target["relative_path"] for target in plan.targets] == [
        ".opencode/skills/jira-is-kaydi",
        ".agents/skills/jira-is-kaydi",
        ".claude/skills/jira-is-kaydi",
    ]
    assert all(target["enabled"] is True for target in plan.targets)
    assert all(target["state"] == "new" for target in plan.targets)


def test_reference_payload_hash_matches_metadata() -> None:
    document = (ROOT / "references" / "JIRA_FORMAT_REFERENCE.md").read_text(
        encoding="utf-8"
    )
    payload = document.split("<!-- BEGIN_REFERENCE_PAYLOAD -->\n", 1)[1].split(
        "<!-- END_REFERENCE_PAYLOAD -->", 1
    )[0]
    metadata = dict(
        line.split(":", 1)
        for line in document.split("---\n", 2)[1].splitlines()
        if ":" in line
    )

    assert metadata["reference_content_sha256"].strip(" '") == hashlib.sha256(
        payload.encode()
    ).hexdigest()


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("SKYRSM-5499 için bir yorum hazırla", "draft"),
        ("SKYRSM-5499 kaydına bu yorumu ekle", "explicit-write"),
        ("SKYRSM-5499 nedir?", "read"),
        ("Hazırladığın taslağı Jira'ya ekle", "explicit-write"),
        ("Jira'ya ekleme, yalnız taslak hazırla", "draft"),
        ("Yorum eklemek istemiyorum", "read"),
        ("Bu kaydı oluşturma", "read"),
    ],
)
def test_intent_keeps_draft_and_write_separate(
    content: ModuleType, prompt: str, expected: str
) -> None:
    assert content.classify_intent(prompt) == expected


def test_summary_preserves_technical_tokens_and_turkish_connectors(
    content: ModuleType,
) -> None:
    summary = content.build_summary(
        "ODI akışları ve RTXIX_SYSADM ile extraction_id kontrolü",
        source_kind="Talep",
        source_id="923105",
    )

    assert summary == (
        "Talep ID: 923105 - ODI Akışları ve RTXIX_SYSADM ile extraction_id Kontrolü"
    )


def test_summary_accepts_254_and_rejects_255_without_truncation(
    content: ModuleType,
) -> None:
    content.validate_summary("A" * 254)

    with pytest.raises(ValueError, match="1-254"):
        content.validate_summary("A" * 255)


@pytest.mark.parametrize(
    "value",
    [
        "h2. Başlık",
        "# Başlık",
        "**Kalın**",
        "*Kalın*",
        "_İtalik_",
        "{quote}Alıntı{quote}",
        "!görsel.png!",
        "[^ek.txt]",
        "Başlık:{quote}x{quote}",
        "{{teknik}}",
        "??alıntı??",
        "[Ad|https://example.invalid]",
        "[Ad](https://example.invalid)",
    ],
)
def test_summary_rejects_markup(content: ModuleType, value: str) -> None:
    with pytest.raises(ValueError, match="düz metin"):
        content.validate_summary(value)


def test_empty_title_is_controlled_validation_error(content: ModuleType) -> None:
    with pytest.raises(ValueError, match="boş olamaz"):
        content.build_summary("   ", source_kind=None, source_id=None)


def test_refresh_ttl_is_strictly_more_than_thirty_days(refresh: ModuleType) -> None:
    now = dt.datetime(2026, 9, 16, tzinfo=dt.UTC)
    base = {
        "bootstrap_required": "false",
        "refresh_after_days": "30",
        "next_retry_after": None,
    }
    for delta, expected in (
        (dt.timedelta(days=29), False),
        (dt.timedelta(days=30), False),
        (dt.timedelta(days=30, seconds=1), True),
    ):
        metadata = base | {
            "last_checked_at": (now - delta).isoformat().replace("+00:00", "Z")
        }
        assert refresh.should_refresh(metadata, now)[0] is expected


def test_bootstrap_refresh_does_not_wait_for_ttl(refresh: ModuleType) -> None:
    required, reason = refresh.should_refresh(
        {
            "bootstrap_required": "true",
            "refresh_after_days": "30",
            "last_checked_at": None,
            "next_retry_after": None,
        },
        dt.datetime(2026, 9, 16, tzinfo=dt.UTC),
    )

    assert (required, reason) == (True, "bootstrap-required")


def test_failure_keeps_known_good_payload_and_starts_cooldown(
    refresh: ModuleType, tmp_path: Path
) -> None:
    target = _reference(tmp_path)
    before = target.read_text(encoding="utf-8")
    checked_before = refresh.parse_metadata(before)["last_checked_at"]
    payload_before = before.split("<!-- BEGIN_REFERENCE_PAYLOAD -->", 1)[1].split(
        "<!-- END_REFERENCE_PAYLOAD -->", 1
    )[0]

    def fail(_metadata: dict[str, str | None]) -> object:
        raise refresh.URLError("offline")

    result = refresh.refresh(
        target,
        now=dt.datetime(2026, 9, 16, tzinfo=dt.UTC),
        force=True,
        fetcher=fail,
    )
    after = target.read_text(encoding="utf-8")
    payload_after = after.split("<!-- BEGIN_REFERENCE_PAYLOAD -->", 1)[1].split(
        "<!-- END_REFERENCE_PAYLOAD -->", 1
    )[0]
    metadata = refresh.parse_metadata(after)

    assert result["status"] == "failed"
    assert payload_after == payload_before
    assert metadata["last_checked_at"] == checked_before
    assert metadata["next_retry_after"] == "2026-09-17T00:00:00Z"
    assert refresh.should_refresh(
        metadata, dt.datetime(2026, 9, 16, 1, tzinfo=dt.UTC)
    ) == (False, "retry-cooldown")


def test_successful_bootstrap_persists_deterministic_baseline(
    refresh: ModuleType, tmp_path: Path
) -> None:
    target = _reference(tmp_path)
    html = _valid_html(refresh)

    def fetch(_metadata: dict[str, str | None]) -> object:
        return refresh.FetchResult(200, refresh.SOURCE_URL, html, '"etag-1"', None)

    result = refresh.refresh(
        target,
        now=dt.datetime(2026, 9, 16, tzinfo=dt.UTC),
        force=True,
        fetcher=fetch,
    )
    document = target.read_text(encoding="utf-8")
    metadata = refresh.parse_metadata(document)
    baseline = refresh._baseline_text(document)

    assert result["status"] == "changed"
    assert metadata["bootstrap_required"] == "false"
    assert metadata["last_checked_at"] == "2026-09-16T00:00:00Z"
    assert baseline is not None
    parsed = json.loads(baseline)
    assert parsed["normalizer_version"] == refresh.NORMALIZER_VERSION
    assert "SELECT 1\n  FROM DUAL;" in parsed["normalized_text"]
    expected_hash = hashlib.sha256((baseline + "\n").encode()).hexdigest()
    assert metadata["source_content_sha256"] == expected_hash


def test_304_without_baseline_is_failure(refresh: ModuleType, tmp_path: Path) -> None:
    target = _reference(tmp_path)
    document = target.read_text(encoding="utf-8")
    document = refresh._BASELINE.sub(
        lambda match: match.group(1) + "null" + match.group(2), document, count=1
    )
    document = refresh._replace_meta(
        document,
        {
            "source_content_sha256": None,
            "bootstrap_required": True,
            "last_checked_at": None,
        },
    )
    target.write_text(document, encoding="utf-8", newline="\n")

    def not_modified(_metadata: dict[str, str | None]) -> object:
        return refresh.FetchResult(304, refresh.SOURCE_URL, None, '"etag-1"', None)

    result = refresh.refresh(
        target,
        now=dt.datetime(2026, 9, 16, tzinfo=dt.UTC),
        fetcher=not_modified,
    )

    assert result["status"] == "failed"
    assert refresh.parse_metadata(target.read_text(encoding="utf-8"))[
        "bootstrap_required"
    ] == "true"


def test_normalizer_ignores_footer_but_preserves_code_indentation(
    refresh: ModuleType,
) -> None:
    first = refresh.normalize_source(_valid_html(refresh, footer="A"))
    second = refresh.normalize_source(_valid_html(refresh, footer="B"))

    assert first == second
    assert "SELECT 1\n  FROM DUAL;" in first["normalized_text"]


def test_access_denied_page_with_section_words_is_rejected(refresh: ModuleType) -> None:
    sections = " ".join(refresh.EXPECTED_SECTIONS)
    payload = f"<html><body><h1>Access denied</h1>{sections}</body></html>".encode()

    with pytest.raises(ValueError, match="access or challenge"):
        refresh.normalize_source(payload)


def test_reference_hash_drift_is_not_reported_fresh(
    refresh: ModuleType, tmp_path: Path
) -> None:
    target = _reference(tmp_path)
    document = target.read_text(encoding="utf-8").replace(
        "Jira Text Formatting Notation", "Jira X Text Formatting Notation", 1
    )
    target.write_text(document, encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="payload hash mismatch"):
        refresh.validate_reference(target.read_text(encoding="utf-8"))


def test_rehashed_but_empty_baseline_is_rejected(
    refresh: ModuleType, tmp_path: Path
) -> None:
    target = _reference(tmp_path)
    document = target.read_text(encoding="utf-8")
    empty = json.dumps(
        {
            "schema": "zekam-jira-format-source-baseline/v1",
            "normalizer_version": refresh.NORMALIZER_VERSION,
            "sections": [],
            "normalized_text": "",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    document = refresh._BASELINE.sub(
        lambda match: match.group(1) + empty + match.group(2), document, count=1
    )
    document = refresh._replace_meta(
        document,
        {
            "source_content_sha256": hashlib.sha256((empty + "\n").encode()).hexdigest()
        },
    )

    with pytest.raises(ValueError, match="sections invalid"):
        refresh.validate_reference(document)


def test_valid_304_updates_check_time_without_changing_baseline(
    refresh: ModuleType, tmp_path: Path
) -> None:
    target = _reference(tmp_path)
    before = target.read_text(encoding="utf-8")
    baseline_before = refresh._baseline_text(before)
    changed_before = refresh.parse_metadata(before)["last_changed_at"]

    def not_modified(metadata: dict[str, str | None]) -> object:
        return refresh.FetchResult(
            304,
            refresh.SOURCE_URL,
            None,
            metadata.get("etag"),
            metadata.get("last_modified"),
        )

    result = refresh.refresh(
        target,
        now=dt.datetime(2026, 9, 17, tzinfo=dt.UTC),
        force=True,
        fetcher=not_modified,
    )
    after = target.read_text(encoding="utf-8")

    assert result["status"] == "not-modified"
    assert refresh._baseline_text(after) == baseline_before
    assert refresh.parse_metadata(after)["last_changed_at"] == changed_before


def test_same_source_updates_check_time_not_revision_or_change_time(
    refresh: ModuleType, tmp_path: Path
) -> None:
    target = _reference(tmp_path)
    html = _valid_html(refresh)

    def fetch(_metadata: dict[str, str | None]) -> object:
        return refresh.FetchResult(200, refresh.SOURCE_URL, html, '"etag-1"', None)

    first = refresh.refresh(
        target,
        now=dt.datetime(2026, 9, 17, tzinfo=dt.UTC),
        force=True,
        fetcher=fetch,
    )
    first_meta = refresh.parse_metadata(target.read_text(encoding="utf-8"))
    second = refresh.refresh(
        target,
        now=dt.datetime(2026, 9, 18, tzinfo=dt.UTC),
        force=True,
        fetcher=fetch,
    )
    second_meta = refresh.parse_metadata(target.read_text(encoding="utf-8"))

    assert first["status"] == "changed"
    assert second["status"] == "unchanged"
    assert second_meta["reference_revision"] == first_meta["reference_revision"]
    assert second_meta["last_changed_at"] == first_meta["last_changed_at"]
    assert second_meta["last_checked_at"] == "2026-09-18T00:00:00Z"


def test_existing_refresh_lock_prevents_second_writer(
    refresh: ModuleType, tmp_path: Path
) -> None:
    target = _reference(tmp_path)
    lock = target.with_suffix(target.suffix + ".lock")
    lock.write_text("held", encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="in progress"):
            refresh.refresh(
                target,
                now=dt.datetime(2026, 9, 17, tzinfo=dt.UTC),
                force=True,
            )
    finally:
        lock.unlink()


def test_atomic_readback_drift_restores_previous_reference(
    refresh: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _reference(tmp_path)
    before = target.read_bytes()
    document = target.read_text(encoding="utf-8")
    updated = refresh._replace_meta(document, {"last_check_status": "not-modified"})
    real_replace = refresh.os.replace
    swaps = 0

    def corrupt_first_swap(source: str, destination: str) -> None:
        nonlocal swaps
        real_replace(source, destination)
        if Path(destination) == target and swaps == 0:
            target.write_text("drift", encoding="utf-8")
        swaps += 1

    monkeypatch.setattr(refresh.os, "replace", corrupt_first_swap)
    with pytest.raises(OSError, match="previous copy restored"):
        refresh._atomic_write(target, updated)

    assert target.read_bytes() == before


def test_normalizer_change_requires_explicit_rebaseline(
    refresh: ModuleType, tmp_path: Path
) -> None:
    target = _reference(tmp_path)
    document = refresh._replace_meta(
        target.read_text(encoding="utf-8"), {"normalizer_version": "older-v0"}
    )
    baseline = refresh._baseline_text(document)
    assert baseline is not None
    parsed = json.loads(baseline)
    parsed["normalizer_version"] = "older-v0"
    altered = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    document = refresh._BASELINE.sub(
        lambda match: match.group(1) + altered + match.group(2), document, count=1
    )
    document = refresh._replace_meta(
        document,
        {
            "source_content_sha256": hashlib.sha256(
                (altered + "\n").encode()
            ).hexdigest()
        },
    )
    target.write_text(document, encoding="utf-8", newline="\n")

    result = refresh.refresh(
        target, now=dt.datetime(2026, 9, 17, tzinfo=dt.UTC)
    )

    assert result == {
        "status": "rebaseline-required",
        "reason": "normalizer-version-changed",
        "network_calls": 0,
    }
