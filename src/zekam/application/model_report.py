"""Gunluk model saglik raporu.

Ayni kanit iki bicimde sunulur:

- **Turkce Markdown**: insan icin
- **JSON**: makine icin

Ikisi de ayni `evidence_digest` degerine baglanir; boylece "raporda soyle
yaziyordu" iddiasi kanonik kayitla karsilastirilabilir. Rapor secret, ham
endpoint veya prompt icerigi tasimaz.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.model_health import ProbeStatus
from zekam.domain.model_inventory import (
    CANONICAL_MODEL_COUNT,
    TECHNICAL_PROFILE_COUNT,
    HealthState,
    ModelRecord,
)

REPORT_SCHEMA = "zekam-model-health-report/v1"


@dataclass(frozen=True, slots=True)
class ModelHealthSummary:
    """Tek bir modelin rapor satiri."""

    model_id: str
    access_name: str
    modality: str
    health_state: str
    benchmark_state: str
    enabled: bool
    quarantined: bool
    has_technical_profile: bool
    verified_capability_count: int
    last_probe_status: str | None
    stale: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "access_name": self.access_name,
            "modality": self.modality,
            "health_state": self.health_state,
            "benchmark_state": self.benchmark_state,
            "enabled": self.enabled,
            "quarantined": self.quarantined,
            "has_technical_profile": self.has_technical_profile,
            "verified_capability_count": self.verified_capability_count,
            "last_probe_status": self.last_probe_status,
            "stale": self.stale,
        }


@dataclass(frozen=True, slots=True)
class ModelHealthReport:
    """Gunluk rapor. Markdown ve JSON ayni kanittan uretilir."""

    schema: str
    report_date: dt.date
    rows: tuple[ModelHealthSummary, ...]
    canonical_count: int
    technical_profile_count: int
    expected_canonical: int = CANONICAL_MODEL_COUNT
    expected_technical: int = TECHNICAL_PROFILE_COUNT

    @property
    def counts(self) -> dict[str, int]:
        """Durum dagilimi."""
        distribution: dict[str, int] = {}
        for row in self.rows:
            distribution[row.health_state] = distribution.get(row.health_state, 0) + 1
        return dict(sorted(distribution.items()))

    @property
    def quarantined(self) -> tuple[ModelHealthSummary, ...]:
        return tuple(row for row in self.rows if row.quarantined)

    @property
    def stale(self) -> tuple[ModelHealthSummary, ...]:
        return tuple(row for row in self.rows if row.stale)

    @property
    def missing_technical_profile(self) -> tuple[ModelHealthSummary, ...]:
        return tuple(row for row in self.rows if not row.has_technical_profile)

    @property
    def profile_gap(self) -> int:
        """20 kanonik kayit ile 19 teknik profil arasindaki gorunur fark."""
        return self.canonical_count - self.technical_profile_count

    def evidence(self) -> dict[str, Any]:
        """Digest hesaplanan ortak kanit govdesi."""
        return {
            "schema": self.schema,
            "report_date": self.report_date,
            "canonical_count": self.canonical_count,
            "technical_profile_count": self.technical_profile_count,
            "expected_canonical": self.expected_canonical,
            "expected_technical": self.expected_technical,
            "counts": self.counts,
            "rows": [row.as_dict() for row in self.rows],
        }

    @property
    def evidence_digest(self) -> str:
        return digest(self.evidence())

    def as_json(self) -> str:
        """Makine okunur bicim."""
        return canonical_json(self.evidence() | {"evidence_digest": self.evidence_digest})

    def as_markdown(self) -> str:
        """Insan okunur Turkce bicim."""
        lines: list[str] = [
            f"# Model Sağlık Raporu — {self.report_date.isoformat()}",
            "",
            f"Kanıt digest: `{self.evidence_digest}`",
            "",
            "## Özet",
            "",
            f"- Kanonik kayıt: **{self.canonical_count}** (beklenen {self.expected_canonical})",
            (
                f"- Teknik profil: **{self.technical_profile_count}** "
                f"(beklenen {self.expected_technical})"
            ),
            f"- Görünür profil farkı: **{self.profile_gap}**",
            f"- Karantinada: **{len(self.quarantined)}**",
            f"- Yeniden test gereken (stale): **{len(self.stale)}**",
            "",
            "## Durum dağılımı",
            "",
            "| Durum | Adet |",
            "|---|---|",
        ]
        lines.extend(f"| `{state}` | {count} |" for state, count in self.counts.items())

        lines.extend(
            [
                "",
                "## Modeller",
                "",
                "| Erişim adı | Modalite | Sağlık | Benchmark | Teknik profil"
                " | Doğrulanmış yetenek |",
                "|---|---|---|---|---|---|",
            ]
        )
        for row in self.rows:
            profile = "var" if row.has_technical_profile else "**yok**"
            lines.append(
                f"| `{row.access_name}` | {row.modality} | `{row.health_state}` "
                f"| `{row.benchmark_state}` | {profile} | {row.verified_capability_count} |"
            )

        if self.missing_technical_profile:
            lines.extend(
                [
                    "",
                    "## Teknik profili olmayan kayıtlar",
                    "",
                    "Bu kayıtlar için API adresi, çalışma modu ve desteklenen parametreler",
                    "ayrıca doğrulanmalıdır. Fark gizlenmez.",
                    "",
                ]
            )
            lines.extend(
                f"- `{row.access_name}` ({row.model_id})" for row in self.missing_technical_profile
            )

        if self.quarantined:
            lines.extend(["", "## Karantinadaki modeller", ""])
            lines.extend(
                f"- `{row.access_name}` — `{row.health_state}`" for row in self.quarantined
            )

        lines.extend(
            [
                "",
                "---",
                "",
                "Sağlık başarısı yetenek kanıtı değildir; yalnızca benchmark uygunluğu sağlar.",
                "Bu rapor secret, ham endpoint veya prompt içeriği taşımaz.",
                "",
            ]
        )
        return "\n".join(lines)

    @property
    def markdown_digest(self) -> str:
        return digest(self.as_markdown())

    @property
    def json_digest(self) -> str:
        return digest(self.as_json())

    def summary(self) -> dict[str, Any]:
        """Veritabanina yazilan ozet."""
        return {
            "canonical_count": self.canonical_count,
            "technical_profile_count": self.technical_profile_count,
            "profile_gap": self.profile_gap,
            "counts": self.counts,
            "quarantined": [row.model_id for row in self.quarantined],
            "stale": [row.model_id for row in self.stale],
        }


def build_report(
    records: Sequence[ModelRecord],
    *,
    report_date: dt.date,
    stale_model_ids: Sequence[str] = (),
    last_probe_status: dict[str, ProbeStatus] | None = None,
) -> ModelHealthReport:
    """Envanter kayitlarindan gunluk rapor uretir."""
    stale = set(stale_model_ids)
    statuses = last_probe_status or {}
    rows = tuple(
        ModelHealthSummary(
            model_id=record.model_id,
            access_name=record.access_name,
            modality=record.modality.value,
            health_state=record.health_state.value,
            benchmark_state=record.benchmark_state.value,
            enabled=record.enabled,
            quarantined=record.health_state is HealthState.QUARANTINED,
            has_technical_profile=record.has_technical_profile,
            verified_capability_count=len(record.capabilities_verified),
            last_probe_status=(
                statuses[record.model_id].value if record.model_id in statuses else None
            ),
            stale=record.model_id in stale,
        )
        for record in sorted(records, key=lambda item: item.inventory_index)
    )
    return ModelHealthReport(
        schema=REPORT_SCHEMA,
        report_date=report_date,
        rows=rows,
        canonical_count=len(rows),
        technical_profile_count=sum(1 for row in rows if row.has_technical_profile),
    )
