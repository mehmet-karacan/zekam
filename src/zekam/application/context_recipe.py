"""Rol bazli, surumlu ve authority-free context recipe registry."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_ranking import (
    ContextCandidateSet,
    ContextCandidateSetIssuer,
    ContextRankingSnapshot,
    ContextRankingSnapshotIssuer,
)
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    ContextManifest,
    ContextOmission,
    OmittedReason,
)
from zekam.domain.context_scoring import (
    CONTEXT_SCORING_POLICY_DIGEST,
    CONTEXT_SCORING_POLICY_VERSION,
)
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed

_PROCESS_PACKET_KEY = secrets.token_bytes(32)


class ContextRecipeRole(StrEnum):
    COORDINATOR = "coordinator"
    RESEARCHER = "researcher"
    BUILDER = "builder"
    VERIFIER = "verifier"


@dataclass(frozen=True, slots=True)
class ContextRecipe:
    recipe_id: str
    version: int
    role: ContextRecipeRole
    allowed_kinds: frozenset[ContextCandidateKind]
    required_kinds: frozenset[ContextCandidateKind]
    maximum_token_budget: int
    per_kind_candidate_limit: int
    per_kind_token_limit: int
    minimum_authority: AuthorityLevel = AuthorityLevel.OBSERVED

    def __post_init__(self) -> None:
        if (
            not self.recipe_id.strip()
            or self.version < 1
            or self.maximum_token_budget < 1
            or self.per_kind_candidate_limit < 1
            or self.per_kind_token_limit < 1
        ):
            raise ValidationFailed("Context recipe kimlik, surum ve pozitif limitler ister")
        if not isinstance(self.role, ContextRecipeRole):
            raise ValidationFailed("Context recipe role registry disinda")
        if not isinstance(self.minimum_authority, AuthorityLevel):
            raise ValidationFailed("Context recipe authority floor gecersiz")
        if not self.required_kinds or not self.required_kinds <= self.allowed_kinds:
            raise ValidationFailed("Required context kind allowed kumesinin alt kumesi olmali")
        if ContextCandidateKind.GENERAL in self.allowed_kinds:
            raise PolicyViolation("Role recipe untyped general context kabul edemez")
        if self.role is ContextRecipeRole.COORDINATOR and self.allowed_kinds & {
            ContextCandidateKind.SOURCE_SLICE,
            ContextCandidateKind.SOURCE_DIFF,
        }:
            raise PolicyViolation("Coordinator source/codebase context alamaz")

    @property
    def recipe_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-context-recipe/v1",
            "recipe_id": self.recipe_id,
            "version": self.version,
            "role": self.role.value,
            "allowed_kinds": sorted(item.value for item in self.allowed_kinds),
            "required_kinds": sorted(item.value for item in self.required_kinds),
            "maximum_token_budget": self.maximum_token_budget,
            "per_kind_candidate_limit": self.per_kind_candidate_limit,
            "per_kind_token_limit": self.per_kind_token_limit,
            "minimum_authority": int(self.minimum_authority),
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class RecipeContextPacket:
    recipe_id: str
    recipe_digest: str
    role: ContextRecipeRole
    requested_token_budget: int
    manifest: ContextManifest
    recipe_excluded: tuple[str, ...]
    issuance_seal: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        parse_digest(self.recipe_digest)
        parse_digest(self.issuance_seal)
        if self.requested_token_budget < self.manifest.token_budget:
            raise ValidationFailed("Recipe effective budget istek budgetini asamaz")
        if self.grants_authority:
            raise PolicyViolation("Context recipe packet authority uretemez")
        if (
            self.manifest.recipe_id != self.recipe_id
            or self.manifest.recipe_digest != self.recipe_digest
            or self.manifest.target_role != self.role.value
        ):
            raise PolicyViolation("Context recipe packet manifest binding drift")

    @property
    def packet_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-recipe-context-packet/v1",
            "recipe_id": self.recipe_id,
            "recipe_digest": self.recipe_digest,
            "role": self.role.value,
            "requested_token_budget": self.requested_token_budget,
            "effective_token_budget": self.manifest.token_budget,
            "manifest_digest": self.manifest.manifest_digest,
            "recipe_excluded": list(self.recipe_excluded),
            "grants_authority": False,
        }


COMMON_REQUIRED = frozenset(
    {ContextCandidateKind.SYSTEM_POLICY, ContextCandidateKind.WORK_CONTRACT}
)


def _recipe(
    recipe_id: str,
    role: ContextRecipeRole,
    allowed: set[ContextCandidateKind],
    required: set[ContextCandidateKind],
    budget: int,
    per_kind_candidates: int,
    per_kind_tokens: int,
) -> ContextRecipe:
    return ContextRecipe(
        recipe_id,
        1,
        role,
        COMMON_REQUIRED | allowed,
        COMMON_REQUIRED | required,
        budget,
        per_kind_candidates,
        per_kind_tokens,
    )


DEFAULT_CONTEXT_RECIPES = (
    _recipe(
        "coordinator-v1",
        ContextRecipeRole.COORDINATOR,
        {
            ContextCandidateKind.RUN_STATUS,
            ContextCandidateKind.CHECKPOINT,
            ContextCandidateKind.EFFECT_RECEIPT,
            ContextCandidateKind.VERIFICATION_RESULT,
            ContextCandidateKind.ARCHITECTURE_RULE,
            ContextCandidateKind.DEPENDENCY_MANIFEST,
            ContextCandidateKind.RESEARCH_EVIDENCE,
        },
        {ContextCandidateKind.RUN_STATUS},
        1800,
        2,
        600,
    ),
    _recipe(
        "researcher-v1",
        ContextRecipeRole.RESEARCHER,
        {
            ContextCandidateKind.RESEARCH_EVIDENCE,
            ContextCandidateKind.CITATION,
            ContextCandidateKind.SOURCE_SLICE,
            ContextCandidateKind.KNOWLEDGE,
            ContextCandidateKind.MEMORY_SUMMARY,
            ContextCandidateKind.ARCHITECTURE_RULE,
            ContextCandidateKind.DEPENDENCY_MANIFEST,
            ContextCandidateKind.CHECKPOINT,
        },
        set(),
        6000,
        8,
        3000,
    ),
    _recipe(
        "builder-v1",
        ContextRecipeRole.BUILDER,
        {
            ContextCandidateKind.ARCHITECTURE_RULE,
            ContextCandidateKind.DEPENDENCY_MANIFEST,
            ContextCandidateKind.SOURCE_SLICE,
            ContextCandidateKind.SOURCE_DIFF,
            ContextCandidateKind.KNOWLEDGE,
            ContextCandidateKind.RESEARCH_EVIDENCE,
            ContextCandidateKind.EFFECT_RECEIPT,
            ContextCandidateKind.TOOL_RESULT_SUMMARY,
            ContextCandidateKind.TEST_EVIDENCE,
            ContextCandidateKind.CHECKPOINT,
        },
        {
            ContextCandidateKind.ARCHITECTURE_RULE,
            ContextCandidateKind.DEPENDENCY_MANIFEST,
            ContextCandidateKind.SOURCE_SLICE,
        },
        12000,
        8,
        4000,
    ),
    _recipe(
        "verifier-v1",
        ContextRecipeRole.VERIFIER,
        {
            ContextCandidateKind.ARCHITECTURE_RULE,
            ContextCandidateKind.DEPENDENCY_MANIFEST,
            ContextCandidateKind.SOURCE_SLICE,
            ContextCandidateKind.SOURCE_DIFF,
            ContextCandidateKind.EFFECT_RECEIPT,
            ContextCandidateKind.VERIFICATION_RESULT,
            ContextCandidateKind.TEST_EVIDENCE,
            ContextCandidateKind.CHECKPOINT,
        },
        {
            ContextCandidateKind.SOURCE_DIFF,
            ContextCandidateKind.EFFECT_RECEIPT,
            ContextCandidateKind.TEST_EVIDENCE,
        },
        8000,
        8,
        3000,
    ),
)


@dataclass(frozen=True, slots=True)
class ContextRecipeRegistry:
    recipes: tuple[ContextRecipe, ...] = DEFAULT_CONTEXT_RECIPES

    def __post_init__(self) -> None:
        if len({item.recipe_id for item in self.recipes}) != len(self.recipes):
            raise ValidationFailed("Context recipe kimlikleri tekil olmali")
        if len({item.role for item in self.recipes}) != len(self.recipes):
            raise ValidationFailed("Her role icin tek current context recipe olmali")

    def for_role(self, role: ContextRecipeRole) -> ContextRecipe:
        match = next((item for item in self.recipes if item.role is role), None)
        if match is None:
            raise NotFound(f"Context recipe bulunamadi: {role.value}")
        return match

    def validate_packet(self, packet: RecipeContextPacket, role: ContextRecipeRole) -> None:
        """Current recipe kimligini ve packet semantigini yeniden dogrular."""

        current = self.for_role(role)
        if not hmac.compare_digest(packet.issuance_seal, self._seal_packet_body(packet.body())):
            raise PolicyViolation("Context recipe packet issuance provenance gecersiz")
        if (
            packet.role is not role
            or packet.recipe_id != current.recipe_id
            or packet.recipe_digest != current.recipe_digest
            or packet.manifest.recipe_digest != current.recipe_digest
            or packet.manifest.target_role != role.value
        ):
            raise PolicyViolation("Context recipe packet stale veya cross-role replay")
        manifest = packet.manifest
        selected = manifest.selected
        if (
            manifest.compiler_version != CONTEXT_SCORING_POLICY_VERSION
            or manifest.scoring_policy_digest != CONTEXT_SCORING_POLICY_DIGEST
            or manifest.compiler_metrics is None
        ):
            raise PolicyViolation("Context recipe packet scoring policy stale veya eksik")
        excluded_omissions = {
            item.candidate_id
            for item in manifest.omitted
            if item.reason is OmittedReason.RECIPE_EXCLUDED
        }
        if tuple(sorted(excluded_omissions)) != tuple(sorted(packet.recipe_excluded)):
            raise PolicyViolation("Context recipe packet excluded partition drift")
        if manifest.token_budget > min(packet.requested_token_budget, current.maximum_token_budget):
            raise PolicyViolation("Context recipe packet effective budget limiti asildi")
        if len({item.candidate_id for item in selected}) != len(selected):
            raise PolicyViolation("Context recipe packet candidate kimlikleri tekil olmali")
        if any(item.kind not in current.allowed_kinds for item in selected):
            raise PolicyViolation("Context recipe packet forbidden kind iceriyor")
        if current.role is ContextRecipeRole.COORDINATOR and any(
            item.kind in {ContextCandidateKind.SOURCE_SLICE, ContextCandidateKind.SOURCE_DIFF}
            for item in selected
        ):
            raise PolicyViolation("Coordinator packet source/codebase context iceremez")
        if any(
            item.reason != "context-score-v2"
            or int(item.authority) < int(current.minimum_authority)
            or item.score[-1] != item.candidate_id
            for item in selected
        ):
            raise PolicyViolation("Context recipe packet selection provenance gecersiz")
        for kind in current.allowed_kinds:
            matching = tuple(item for item in selected if item.kind is kind)
            if len(matching) > current.per_kind_candidate_limit:
                raise PolicyViolation(
                    f"Context recipe packet per-kind candidate limiti asildi: {kind}"
                )
            if sum(item.token_count for item in matching) > current.per_kind_token_limit:
                raise PolicyViolation(f"Context recipe packet per-kind token limiti asildi: {kind}")
        for kind in current.required_kinds:
            matching = tuple(item for item in selected if item.kind is kind)
            if len(matching) != 1 or "required" not in matching[0].reason_codes:
                raise PolicyViolation(f"Context recipe packet required kind tekil olmali: {kind}")

    @staticmethod
    def _seal_packet_body(body: dict[str, Any]) -> str:
        signature = hmac.new(
            _PROCESS_PACKET_KEY,
            digest(body).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256:{signature}"

    def compile(
        self,
        role: ContextRecipeRole,
        candidate_set: ContextCandidateSet,
        *,
        token_budget: int,
        minimum_authority: AuthorityLevel,
        now: dt.datetime,
        ranking_snapshot: ContextRankingSnapshot,
    ) -> RecipeContextPacket:
        ContextCandidateSetIssuer.verify(candidate_set, ranking_snapshot, now=now)
        candidates = candidate_set.candidates
        contents = candidate_set.content_mapping()
        recipe = self.for_role(role)
        if token_budget < 1:
            raise ValidationFailed("Context recipe token budget pozitif olmali")
        eligible = tuple(item for item in candidates if item.kind in recipe.allowed_kinds)
        forbidden_required = tuple(
            item.candidate_id
            for item in candidates
            if item.required and item.kind not in recipe.allowed_kinds
        )
        if forbidden_required:
            raise PolicyViolation("Excluded context candidate required olarak isaretlenemez")
        excluded = tuple(
            sorted(
                item.candidate_id for item in candidates if item.kind not in recipe.allowed_kinds
            )
        )
        missing = recipe.required_kinds - {item.kind for item in eligible}
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise PolicyViolation(f"Context recipe required kind eksik: {names}")
        for kind in recipe.allowed_kinds:
            matching = tuple(item for item in eligible if item.kind is kind)
            groups: dict[tuple[object, ...], ContextCandidate] = {}
            for item in matching:
                key = (
                    item.scope_ref,
                    item.source_revision,
                    item.content_digest,
                    item.applicable_roles,
                )
                previous = groups.get(key)
                if previous is not None and previous.token_count != item.token_count:
                    raise PolicyViolation("Exact duplicate context token count drift")
                groups.setdefault(key, item)
            unique = tuple(groups.values())
            if len(unique) > recipe.per_kind_candidate_limit:
                raise PolicyViolation(f"Context recipe per-kind candidate limiti asildi: {kind}")
            if sum(item.token_count for item in unique) > recipe.per_kind_token_limit:
                raise PolicyViolation(f"Context recipe per-kind token limiti asildi: {kind}")
        for kind in recipe.required_kinds:
            if sum(item.kind is kind for item in eligible) != 1:
                raise PolicyViolation(f"Context recipe required kind tekil olmali: {kind}")

        required_ids: set[str] = set()
        for kind in recipe.required_kinds:
            required_matches = [item for item in eligible if item.kind is kind]
            selected = sorted(
                required_matches,
                key=lambda item: (
                    -item.score(now)[0],
                    -item.score(now)[1],
                    item.candidate_id,
                ),
            )[0]
            required_ids.add(selected.candidate_id)
        typed = tuple(
            replace(item, required=item.required or item.candidate_id in required_ids)
            for item in eligible
        )
        all_typed = tuple(
            next(
                (
                    typed_item
                    for typed_item in typed
                    if typed_item.candidate_id == item.candidate_id
                ),
                item,
            )
            for item in candidates
        )
        ContextRankingSnapshotIssuer.verify(ranking_snapshot, now=now)
        request = ranking_snapshot.request
        if request.role != role.value:
            raise PolicyViolation("Context ranking snapshot role drift")
        manifest = compile_context_v2(
            all_typed,
            ranking_request=request,
            token_budget=min(token_budget, recipe.maximum_token_budget),
            minimum_authority=max(minimum_authority, recipe.minimum_authority),
            now=now,
            recipe_id=recipe.recipe_id,
            recipe_digest=recipe.recipe_digest,
            target_role=role.value,
            pre_omitted=tuple(
                ContextOmission(
                    item.candidate_id,
                    OmittedReason.RECIPE_EXCLUDED,
                    item.token_count,
                )
                for item in candidates
                if item.kind not in recipe.allowed_kinds
            ),
            contents=contents,
            ranking_snapshot_digest=ranking_snapshot.snapshot_digest,
            candidate_set_digest=candidate_set.candidate_set_digest,
        )
        for kind in recipe.allowed_kinds:
            selected_matching = tuple(item for item in manifest.selected if item.kind is kind)
            if len(selected_matching) > recipe.per_kind_candidate_limit:
                raise PolicyViolation(f"Context recipe per-kind candidate limiti asildi: {kind}")
            if sum(item.token_count for item in selected_matching) > recipe.per_kind_token_limit:
                raise PolicyViolation(f"Context recipe per-kind token limiti asildi: {kind}")
        packet_body = {
            "schema": "zekam-recipe-context-packet/v1",
            "recipe_id": recipe.recipe_id,
            "recipe_digest": recipe.recipe_digest,
            "role": role.value,
            "requested_token_budget": token_budget,
            "effective_token_budget": manifest.token_budget,
            "manifest_digest": manifest.manifest_digest,
            "recipe_excluded": list(excluded),
            "grants_authority": False,
        }
        return RecipeContextPacket(
            recipe.recipe_id,
            recipe.recipe_digest,
            role,
            token_budget,
            manifest,
            excluded,
            self._seal_packet_body(packet_body),
        )
