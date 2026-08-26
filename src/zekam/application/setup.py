"""Yeni makine icin acik, idempotent Zekam kurulum plani."""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SetupStep:
    """Shell yorumlamasi gerektirmeyen tek kurulum adimi."""

    step_id: str
    argv: tuple[str, ...]
    mutates: bool
    description: str

    def as_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "argv": list(self.argv),
            "mutates": self.mutates,
            "description": self.description,
        }


def build_setup_plan(*, platform: str | None = None) -> tuple[SetupStep, ...]:
    """Platforma uygun, kararli yeni-makine kurulum sirasini uretir."""

    current_platform = sys.platform if platform is None else platform
    steps: list[SetupStep] = []
    if current_platform == "win32":
        steps.append(
            SetupStep(
                step_id="windows-git-ca",
                argv=("git", "config", "--global", "http.sslBackend", "schannel"),
                mutates=True,
                description="Git HTTPS icin Windows sertifika deposunu kullanir",
            )
        )
    steps.extend(
        (
            SetupStep(
                "home-layout",
                ("init", "--persistence", "postgresql"),
                True,
                "ZEKAM_HOME yerlesimini idempotent kurar",
            ),
            SetupStep(
                "database-migrations",
                ("db", "upgrade", "--uygula"),
                True,
                "Eksik tablo, fonksiyon ve diger migration nesnelerini uygular",
            ),
            SetupStep(
                "default-policy",
                ("policy", "init", "--uygula"),
                True,
                "Varsayilan policy ve capability kayitlarini kurar",
            ),
            SetupStep(
                "model-inventory",
                ("model", "inventory", "--uygula"),
                True,
                "Kanonik model envanterini kurar",
            ),
            SetupStep(
                "scheduler-jobs",
                ("scheduler", "init", "--uygula"),
                True,
                "Zorunlu scheduler islerini kurar",
            ),
            SetupStep(
                "final-doctor",
                ("doctor", "--json"),
                False,
                "Kurulum sonucunu salt okunur dogrular",
            ),
        )
    )
    return tuple(steps)
