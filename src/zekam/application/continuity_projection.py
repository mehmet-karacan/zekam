"""Privacy-filtered continuity projection and deterministic hydration recipes.

The builders in this module are deliberately read-only.  They select already
canonical records, produce a repeatable Markdown bundle/receipt and calculate a
bounded hydration recipe.  Neither result grants authority or mutates a store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.application.markdown_projection import build_markdown_projection
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.markdown_projection import MarkdownProjectionBundle, ProjectionRecord
from zekam.domain.session_continuity import DataClassification, DigestReference

_PUBLIC_PROJECTION_CLASSES = frozenset({DataClassification.PUBLIC, DataClassification.INTERNAL})
_PROJECTION_SENSITIVE = re.compile(
    r"(?:api[-_ ]?key|secret|credential|password|parola|private[-_ ]?key|"
    r"bearer\s+[A-Za-z0-9._-]{8,}|"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)


class HydrationPriority(StrEnum):
    MUST_LOAD = "must-load"
    SHOULD_LOAD_IF_RELEVANT = "should-load-if-relevant"
    RETRIEVE_ON_DEMAND = "retrieve-on-demand"
    NEVER_AUTO_LOAD = "never-auto-load"


class ProjectionAudience(StrEnum):
    PUBLIC = "public"
    LOCAL_INTERNAL = "local-internal"


class HydrationCategory(StrEnum):
    REPOSITORY_IDENTITY = "repository-identity"
    INSTRUCTION_HIERARCHY = "instruction-hierarchy"
    ACTIVE_WORK = "active-work"
    POLICY = "policy"
    SOURCE_REVISION = "source-revision"
    CHECKPOINT = "checkpoint"
    RECOVERY_BLOCKER = "recovery-blocker"
    HUMAN_DECISION = "human-decision"
    DURABLE_RULE = "durable-rule"
    RELEVANT_FAILURE = "relevant-failure"
    VALIDATED_SKILL = "validated-skill"
    KNOWLEDGE_ARTICLE = "knowledge-article"
    HISTORICAL_DECISION = "historical-decision"
    DAYLOG = "daylog"
    RELATED_GRAPH_NODE = "related-graph-node"
    RAW_TRANSCRIPT = "raw-transcript"
    RAW_PRIVATE_LOG = "raw-private-log"
    SECRET = "secret"
    DIAGNOSTIC_PAYLOAD = "diagnostic-payload"
    UNRELATED_PROJECT = "unrelated-project"


_CATEGORY_PRIORITY = {
    HydrationCategory.REPOSITORY_IDENTITY: HydrationPriority.MUST_LOAD,
    HydrationCategory.INSTRUCTION_HIERARCHY: HydrationPriority.MUST_LOAD,
    HydrationCategory.ACTIVE_WORK: HydrationPriority.MUST_LOAD,
    HydrationCategory.POLICY: HydrationPriority.MUST_LOAD,
    HydrationCategory.SOURCE_REVISION: HydrationPriority.MUST_LOAD,
    HydrationCategory.CHECKPOINT: HydrationPriority.MUST_LOAD,
    HydrationCategory.RECOVERY_BLOCKER: HydrationPriority.MUST_LOAD,
    HydrationCategory.HUMAN_DECISION: HydrationPriority.SHOULD_LOAD_IF_RELEVANT,
    HydrationCategory.DURABLE_RULE: HydrationPriority.SHOULD_LOAD_IF_RELEVANT,
    HydrationCategory.RELEVANT_FAILURE: HydrationPriority.SHOULD_LOAD_IF_RELEVANT,
    HydrationCategory.VALIDATED_SKILL: HydrationPriority.SHOULD_LOAD_IF_RELEVANT,
    HydrationCategory.KNOWLEDGE_ARTICLE: HydrationPriority.RETRIEVE_ON_DEMAND,
    HydrationCategory.HISTORICAL_DECISION: HydrationPriority.RETRIEVE_ON_DEMAND,
    HydrationCategory.DAYLOG: HydrationPriority.RETRIEVE_ON_DEMAND,
    HydrationCategory.RELATED_GRAPH_NODE: HydrationPriority.RETRIEVE_ON_DEMAND,
    HydrationCategory.RAW_TRANSCRIPT: HydrationPriority.NEVER_AUTO_LOAD,
    HydrationCategory.RAW_PRIVATE_LOG: HydrationPriority.NEVER_AUTO_LOAD,
    HydrationCategory.SECRET: HydrationPriority.NEVER_AUTO_LOAD,
    HydrationCategory.DIAGNOSTIC_PAYLOAD: HydrationPriority.NEVER_AUTO_LOAD,
    HydrationCategory.UNRELATED_PROJECT: HydrationPriority.NEVER_AUTO_LOAD,
}


@dataclass(frozen=True, slots=True)
class ClassifiedProjectionRecord:
    record: ProjectionRecord
    classification: DataClassification

    def __post_init__(self) -> None:
        self.record.__post_init__()
        if not isinstance(self.classification, DataClassification):
            raise ValidationFailed("projection classification registry disinda")


@dataclass(frozen=True, slots=True)
class ProjectionExclusion:
    record_digest: str
    reason_code: str

    def __post_init__(self) -> None:
        parse_digest(self.record_digest)

    def as_dict(self) -> dict[str, str]:
        return {
            "record_digest": self.record_digest,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ProjectionGenerationReceipt:
    project_id: str
    source_snapshot_digest: str
    source_head: str
    migration_head: str
    database_revision_digest: str
    projection_digest: str
    record_count: int
    excluded_by_classification: int
    fresh: bool
    audience: ProjectionAudience
    privacy_policy_digest: str
    read_only: bool = True
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.source_snapshot_digest,
            self.database_revision_digest,
            self.projection_digest,
            self.privacy_policy_digest,
        ):
            parse_digest(value)
        if not self.project_id.strip() or not self.source_head.strip():
            raise ValidationFailed("projection receipt kimligi bos olamaz")
        if not isinstance(self.audience, ProjectionAudience):
            raise ValidationFailed("projection audience registry disinda")
        if not self.migration_head.isdigit():
            raise ValidationFailed("projection migration head sayisal olmali")
        if self.record_count < 1 or self.excluded_by_classification < 0:
            raise ValidationFailed("projection receipt sayimlari gecersiz")
        if not self.read_only or self.grants_authority:
            raise PolicyViolation("projection salt okunur ve authority-free olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-projection-generation-receipt/v1",
            "project_id": self.project_id,
            "source_snapshot_digest": self.source_snapshot_digest,
            "source_head": self.source_head,
            "migration_head": self.migration_head,
            "database_revision_digest": self.database_revision_digest,
            "projection_digest": self.projection_digest,
            "record_count": self.record_count,
            "excluded_by_classification": self.excluded_by_classification,
            "fresh": self.fresh,
            "audience": self.audience.value,
            "privacy_policy_digest": self.privacy_policy_digest,
            "read_only": True,
            "grants_authority": False,
        }

    @property
    def receipt_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ContinuityProjectionRecipe:
    bundle: MarkdownProjectionBundle
    exclusions: tuple[ProjectionExclusion, ...]
    receipt: ProjectionGenerationReceipt

    def __post_init__(self) -> None:
        self.bundle.__post_init__()
        expected = tuple(
            sorted(
                self.exclusions,
                key=lambda item: (item.record_digest, item.reason_code),
            )
        )
        if expected != self.exclusions:
            raise ValidationFailed("projection exclusions deterministik sirada olmali")
        if self.receipt.projection_digest != self.bundle.projection_digest:
            raise ValidationFailed("projection receipt bundle ile uyusmuyor")


def build_continuity_projection_recipe(
    project_id: str,
    records: tuple[ClassifiedProjectionRecord, ...],
    *,
    source_head: str,
    migration_head: str,
    database_revision_digest: str,
    expected_source_head: str,
    expected_migration_head: str,
    expected_database_revision_digest: str,
    audience: ProjectionAudience = ProjectionAudience.PUBLIC,
) -> ContinuityProjectionRecipe:
    """Filter private records and build one deterministic projection snapshot."""

    parse_digest(database_revision_digest)
    parse_digest(expected_database_revision_digest)
    ordered = tuple(
        sorted(records, key=lambda item: (item.record.entity_type, item.record.entity_id))
    )
    identities = tuple((item.record.entity_type, item.record.entity_id) for item in ordered)
    if len(identities) != len(set(identities)):
        raise ValidationFailed("projection input identities tekil olmali")
    allowed_classes = (
        frozenset({DataClassification.PUBLIC})
        if audience is ProjectionAudience.PUBLIC
        else _PUBLIC_PROJECTION_CLASSES
    )
    privacy_policy_digest = digest(
        {
            "audience": audience.value,
            "allowed_classifications": sorted(item.value for item in allowed_classes),
            "excluded_classifications": sorted(
                item.value for item in DataClassification if item not in allowed_classes
            ),
        }
    )

    def eligible(item: ClassifiedProjectionRecord) -> bool:
        return item.classification in allowed_classes and not _PROJECTION_SENSITIVE.search(
            str(item.record.as_dict())
        )

    included = tuple(item.record for item in ordered if eligible(item))
    exclusions = tuple(
        ProjectionExclusion(
            item.record.record_digest,
            (
                "classification-excluded"
                if item.classification not in allowed_classes
                else "content-policy-excluded"
            ),
        )
        for item in ordered
        if not eligible(item)
    )
    exclusions = tuple(sorted(exclusions, key=lambda item: (item.record_digest, item.reason_code)))
    if not included:
        raise PolicyViolation("privacy filtresi sonrasinda projection kaydi kalmadi")
    bundle = build_markdown_projection(project_id, included)
    fresh = (
        source_head == expected_source_head
        and migration_head == expected_migration_head
        and database_revision_digest == expected_database_revision_digest
    )
    receipt = ProjectionGenerationReceipt(
        project_id=project_id,
        source_snapshot_digest=bundle.source_snapshot_digest,
        source_head=source_head,
        migration_head=migration_head,
        database_revision_digest=database_revision_digest,
        projection_digest=bundle.projection_digest,
        record_count=len(included),
        excluded_by_classification=len(exclusions),
        fresh=fresh,
        audience=audience,
        privacy_policy_digest=privacy_policy_digest,
    )
    return ContinuityProjectionRecipe(bundle, exclusions, receipt)


@dataclass(frozen=True, slots=True)
class HydrationItem:
    item_id: str
    category: HydrationCategory
    content_ref: str
    source: DigestReference
    classification: DataClassification
    token_cost: int
    relevant: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.category, HydrationCategory) or not isinstance(
            self.classification, DataClassification
        ):
            raise ValidationFailed("hydration category/classification registry disinda")
        if not self.item_id.strip() or not self.content_ref.strip():
            raise ValidationFailed("hydration item kimligi/ref bos olamaz")
        if "\\" in self.content_ref or self.content_ref.startswith(("/", "~")):
            raise ValidationFailed("hydration content ref portable olmali")
        if self.token_cost <= 0:
            raise ValidationFailed("hydration token maliyeti pozitif olmali")

    @property
    def priority(self) -> HydrationPriority:
        return _CATEGORY_PRIORITY[self.category]

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "category": str(self.category),
            "priority": str(self.priority),
            "content_ref": self.content_ref,
            "source": self.source.as_dict(),
            "classification": self.classification.value,
            "token_cost": self.token_cost,
            "relevant": self.relevant,
        }


@dataclass(frozen=True, slots=True)
class HydrationOmission:
    item_id: str
    reason_code: str

    def as_dict(self) -> dict[str, str]:
        return {"item_id": self.item_id, "reason_code": self.reason_code}


@dataclass(frozen=True, slots=True)
class HydrationRecipe:
    selected: tuple[HydrationItem, ...]
    omissions: tuple[HydrationOmission, ...]
    token_budget: int
    tokens_used: int
    required_complete: bool
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.token_budget <= 0 or not 0 <= self.tokens_used <= self.token_budget:
            raise ValidationFailed("hydration budget/sayim gecersiz")
        if not self.required_complete or self.grants_authority:
            raise PolicyViolation("hydration recipe required seti tam ve authority-free olmali")
        selected_ids = tuple(item.item_id for item in self.selected)
        omitted_ids = tuple(item.item_id for item in self.omissions)
        if len({*selected_ids, *omitted_ids}) != len(selected_ids) + len(omitted_ids):
            raise ValidationFailed("hydration item birden fazla sonuca giremez")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-hydration-recipe/v1",
            "selected": [item.as_dict() for item in self.selected],
            "omissions": [item.as_dict() for item in self.omissions],
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "required_complete": True,
            "grants_authority": False,
        }

    @property
    def recipe_digest(self) -> str:
        return digest(self.as_dict())


def build_hydration_recipe(
    items: tuple[HydrationItem, ...],
    *,
    token_budget: int,
    allowed_classifications: frozenset[DataClassification] = _PUBLIC_PROJECTION_CLASSES,
) -> HydrationRecipe:
    """Build deterministic selection; required overflow always fails closed."""

    if token_budget <= 0:
        raise ValidationFailed("hydration token budget pozitif olmali")
    ordered = tuple(sorted(items, key=lambda item: (str(item.priority), item.item_id)))
    ids = tuple(item.item_id for item in ordered)
    if len(ids) != len(set(ids)):
        raise ValidationFailed("hydration item kimlikleri tekil olmali")

    if not allowed_classifications:
        raise ValidationFailed("hydration allowed classification seti bos olamaz")
    if any(not isinstance(item, DataClassification) for item in allowed_classifications):
        raise ValidationFailed("hydration allowed classification registry disinda")
    required = tuple(item for item in ordered if item.priority is HydrationPriority.MUST_LOAD)
    if any(item.classification not in allowed_classifications for item in required):
        raise PolicyViolation("required hydration classification policy tarafindan reddedildi")
    required_tokens = sum(item.token_cost for item in required)
    if required_tokens > token_budget:
        raise PolicyViolation("required hydration seti butceye sigmiyor; silent truncation yasak")

    selected = list(required)
    omissions: list[HydrationOmission] = []
    used = required_tokens
    for item in ordered:
        if item.priority is HydrationPriority.MUST_LOAD:
            continue
        if item.classification not in allowed_classifications:
            omissions.append(HydrationOmission(item.item_id, "classification-excluded"))
            continue
        if item.priority is HydrationPriority.NEVER_AUTO_LOAD:
            omissions.append(HydrationOmission(item.item_id, "never-auto-load"))
            continue
        if item.priority is HydrationPriority.RETRIEVE_ON_DEMAND:
            omissions.append(HydrationOmission(item.item_id, "retrieve-on-demand"))
            continue
        if not item.relevant:
            omissions.append(HydrationOmission(item.item_id, "not-relevant"))
            continue
        if used + item.token_cost > token_budget:
            omissions.append(HydrationOmission(item.item_id, "optional-budget-exhausted"))
            continue
        selected.append(item)
        used += item.token_cost

    return HydrationRecipe(tuple(selected), tuple(omissions), token_budget, used, True)
