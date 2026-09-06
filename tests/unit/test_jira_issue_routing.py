from __future__ import annotations

import pytest

from zekam.application.jira_issue_routing import resolve_jira_issue
from zekam.domain.errors import ValidationFailed


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("gpu projesindeki 5661 taskin detaylari", "SKYRSM-5661"),
        ("GPU 5661 detaylari", "SKYRSM-5661"),
        ("Gelir Paylaşımı Uygulaması 5661", "SKYRSM-5661"),
        ("TTBP 11306", "TLCSKY-11306"),
        ("Türk Telekom Bayi Portalı 11306", "TLCSKY-11306"),
        ("sky projesindeki 11261 task", "TLCSKY-11261"),
        ("TLCSKY-11261 detaylari", "TLCSKY-11261"),
        ("PROJ-123 detaylari", "PROJ-123"),
    ],
)
def test_jira_issue_cozumu(query: str, expected: str) -> None:
    assert resolve_jira_issue(query).issue_key == expected


def test_belirsiz_sayi_ve_proje_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        resolve_jira_issue("5661 detaylari")
    with pytest.raises(ValidationFailed):
        resolve_jira_issue("gpu 5661 ve 5662")
    with pytest.raises(ValidationFailed):
        resolve_jira_issue("GPU TLCSKY-11261")
    with pytest.raises(ValidationFailed):
        resolve_jira_issue("PROJ-1 ve ABC-2 detaylari")


@pytest.mark.parametrize("retired_alias", ["GPO", "GOP"])
def test_retired_gpu_aliases_do_not_resolve_jira_issue(retired_alias: str) -> None:
    with pytest.raises(ValidationFailed):
        resolve_jira_issue(f"{retired_alias} 5661 detaylari")
