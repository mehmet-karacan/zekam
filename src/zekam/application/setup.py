"""Yeni makine icin acik, idempotent Zekam kurulum plani."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from zekam.domain.canonical import digest

SETUP_PLAN_SCHEMA = "zekam-setup-plan/v2"
SETUP_PLAN_GUARANTEES: dict[str, str] = {
    "fresh_home_publish": "atomic",
    "replay": "idempotent",
    "apply_binding": "exact-plan-digest",
    "network": "not-required",
    "docker": "not-required",
}


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


def build_setup_plan(
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> tuple[SetupStep, ...]:
    """Platforma uygun, kararli yeni-makine kurulum sirasini uretir."""

    current_platform = sys.platform if platform is None else platform
    home_arguments = ("--home", str(home)) if home is not None else ()
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
                "local-home-bootstrap",
                ("init", "--persistence", "sqlite", *home_arguments),
                True,
                "SQLite ZEKAM_HOME'u atomik yayinlar ve yerel store'lari idempotent kurar",
            ),
            SetupStep(
                "final-local-core-doctor",
                ("doctor", "--category", "sqlite", "--json", *home_arguments),
                False,
                "Tum composed local store semantic fingerprintlerini dogrular",
            ),
        )
    )
    return tuple(steps)


def setup_plan_digest(steps: tuple[SetupStep, ...]) -> str:
    """Exact sirayi ve argv degerlerini tek canonical digest'e baglar."""

    return digest(setup_plan_payload(steps))


def setup_plan_payload(steps: tuple[SetupStep, ...]) -> dict[str, object]:
    """Digest'e giren public setup plan govdesini uretir."""

    return {
        "schema": SETUP_PLAN_SCHEMA,
        "guarantees": SETUP_PLAN_GUARANTEES,
        "steps": [step.as_dict() for step in steps],
    }
