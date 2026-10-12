"""Read-only precedence for project signals in a natural-language request.

Work and session candidates are evidence *claims* at this layer. A caller must
read back the operational Work record and source binding before setting their
verified flags. This module never reads a database, file, or provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zekam.application.request_routing import RequestRoute
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.identifiers import validate_slug


@dataclass(frozen=True, slots=True)
class ContextSignal:
    """A project claim and whether an external readback verified its source."""

    project_ref: str
    verified: bool

    def __post_init__(self) -> None:
        validate_slug(self.project_ref)


@dataclass(frozen=True, slots=True)
class NaturalRequestScope:
    status: str
    project_refs: tuple[str, ...]
    reason_codes: tuple[str, ...]
    jira_issue_key: str | None
    selected_by: str | None
    route_digest: str

    @property
    def plan_available(self) -> bool:
        return self.status == "selected"

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": "zekam-natural-request-scope/v1",
            "status": self.status,
            "project_refs": list(self.project_refs),
            "reason_codes": list(self.reason_codes),
            "jira_issue_key": self.jira_issue_key,
            "selected_by": self.selected_by,
            "route_digest": self.route_digest,
            "plan_available": self.plan_available,
            "read_only": True,
            "grants_authority": False,
        }
        return body | {"scope_digest": digest(body)}


def resolve_natural_request_scope(
    route: RequestRoute,
    *,
    registered_project_refs: frozenset[str],
    explicit_project_ref: str | None = None,
    work: ContextSignal | None = None,
    session: ContextSignal | None = None,
) -> NaturalRequestScope:
    """Resolve verified signals without silently overriding a conflicting one.

    The route is the only source of Jira identity. A bare number (including a
    port) cannot become an issue key here. A multi-project route remains scoped
    fan-out only when the upstream strategy is ``parallel-project-rag``.
    """

    if explicit_project_ref is not None:
        validate_slug(explicit_project_ref)
    for ref in registered_project_refs:
        validate_slug(ref)
    route_digest = str(route.as_dict()["route_digest"])

    def result(
        status: str,
        refs: tuple[str, ...],
        reasons: tuple[str, ...],
        selected_by: str | None = None,
    ) -> NaturalRequestScope:
        return NaturalRequestScope(
            status=status,
            project_refs=refs,
            reason_codes=reasons,
            jira_issue_key=route.jira_issue_key if status == "selected" else None,
            selected_by=selected_by,
            route_digest=route_digest,
        )

    if route.status == "clarification-required":
        return result("clarification-required", (), route.reason_codes)
    if route.status not in {"selected", "general"}:
        raise ValidationFailed("Bilinmeyen route status")

    for label, signal in (("work", work), ("session", session)):
        if signal is not None and not signal.verified:
            return result("clarification-required", (), (f"{label}-binding-unverified",))

    signals: dict[str, tuple[str, ...]] = {}
    if explicit_project_ref is not None:
        signals["explicit"] = (explicit_project_ref,)
    if route.project_refs:
        signals["route"] = route.project_refs
    if work is not None:
        signals["work"] = (work.project_ref,)
    if session is not None:
        signals["session"] = (session.project_ref,)

    for label, refs in signals.items():
        if any(ref not in registered_project_refs for ref in refs):
            return result("clarification-required", (), (f"{label}-project-unregistered",))

    if route.status == "general":
        if signals:
            return result("clarification-required", (), ("general-project-signal-conflict",))
        return result("general", (), ("no-project-signal",))

    route_refs = tuple(dict.fromkeys(route.project_refs))
    if not route_refs:
        return result("clarification-required", (), ("route-project-missing",))
    if len(route_refs) > 1 and route.strategy != "parallel-project-rag":
        return result("clarification-required", (), ("multi-project-route-ambiguous",))

    route_set = set(route_refs)
    for label in ("explicit", "work", "session"):
        candidate_refs = signals.get(label)
        if candidate_refs is not None and candidate_refs[0] not in route_set:
            return result("clarification-required", (), (f"{label}-route-conflict",))

    single_signals = {
        signals[label][0] for label in ("explicit", "work", "session") if label in signals
    }
    if len(single_signals) > 1:
        return result("clarification-required", (), ("context-project-conflict",))

    if len(route_refs) > 1 and route.intent in {"code-change", "code-review", "test"}:
        return result("clarification-required", (), ("multi-project-mutation-ambiguous",))
    if explicit_project_ref is not None:
        return result("selected", (explicit_project_ref,), (), "explicit")
    if len(route_refs) > 1:
        return result("selected", route_refs, (), "route-fanout")
    return result("selected", route_refs, (), "route")
