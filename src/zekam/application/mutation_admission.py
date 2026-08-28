"""Fail-closed CLI mutation admission metadata and backend capability gate.

The registry does not mint authority and does not replace a command's exact
plan, authorization, claim or receipt checks.  Its exemptions only prevent a
bootstrap deadlock in the ordering of the *existing hydration* prerequisite.
An exempt command still needs the backend capability and every authorization
required by its own application service.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ZekamError

CLI_MUTATION_REGISTRY_META_KEY = "zekam.cli-mutation-admission-registry"
_APPLY_PARAMETER_NAMES = ("apply", "uygula")
_CLOSE_RECEIPT_COMMANDS = frozenset({("close", "apply"), ("memory", "close-apply")})
_MAX_CLOSE_RECEIPT_BYTES = 1_048_576


def _close_receipt_identity(
    command_path: tuple[str, ...],
    parameters: Mapping[str, Any],
) -> dict[str, str]:
    """Read only the bounded exact identity carried by a mutating close receipt."""

    if command_path not in _CLOSE_RECEIPT_COMMANDS or not any(
        bool(parameters.get(name, False)) for name in _APPLY_PARAMETER_NAMES
    ):
        return {}
    value = parameters.get("input_file")
    if value is None:
        return {}
    if not isinstance(value, (str, Path)):
        raise PolicyViolation("Close input receipt path tipi gecersiz")
    try:
        with Path(value).open("rb") as stream:
            payload = stream.read(_MAX_CLOSE_RECEIPT_BYTES + 1)
        if len(payload) > _MAX_CLOSE_RECEIPT_BYTES:
            raise PolicyViolation("Close input receipt bounded boyutu asti")
        document = json.loads(payload.decode("utf-8"))
    except PolicyViolation:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("Close input receipt exact JSON olarak okunamadi") from exc
    if not isinstance(document, dict):
        raise PolicyViolation("Close input receipt JSON object olmali")
    try:
        session_ref = document["session_id"]
        client_ref = document["client_id"]
        if not isinstance(session_ref, str) or not isinstance(client_ref, str):
            raise ValueError("session/client text olmali")
        return {
            "project_ref": str(UUID(str(document["project_id"]))),
            "work_ref": str(UUID(str(document["work_item_id"]))),
            "run_ref": str(UUID(str(document["run_id"]))),
            "session_ref": session_ref,
            "client_ref": client_ref,
        }
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PolicyViolation("Close input receipt exact execution kimligi ister") from exc


class MutationAdmissionExemption(StrEnum):
    """Narrow reasons for skipping only the existing-hydration prerequisite."""

    BOOTSTRAP = "bootstrap"
    CONTROL_PLANE = "control-plane"
    HYDRATION = "hydration"
    LOCAL_EFFECT = "local-effect"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class CliMutationTargetHints:
    """Exact target refs parsed by Typer; absent hints never widen a DB lookup."""

    project_ref: str | None = None
    work_ref: str | None = None
    run_ref: str | None = None
    session_ref: str | None = None
    client_ref: str | None = None
    authorization_ref: str | None = None
    candidate_ref: str | None = None
    trace_ref: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.project_ref,
            self.work_ref,
            self.run_ref,
            self.session_ref,
            self.client_ref,
            self.authorization_ref,
            self.candidate_ref,
            self.trace_ref,
        ):
            if value is not None and (not value or value != value.strip()):
                raise PolicyViolation("CLI mutation target ref bos veya padded olamaz")

    @classmethod
    def from_parameters(
        cls,
        command_path: tuple[str, ...],
        parameters: Mapping[str, Any],
    ) -> CliMutationTargetHints:
        def first(names: tuple[str, ...]) -> str | None:
            for name in names:
                value = parameters.get(name)
                if isinstance(value, UUID):
                    return str(value)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        project_names = ["project_id", "project_uuid", "project", "proje"]
        work_names = ["work_item_id", "work_id", "work", "reference"]
        if command_path and command_path[0] in {"oracle", "project"}:
            project_names.append("query")
        if command_path == ("work", "relate"):
            work_names.append("source")
        close_identity = _close_receipt_identity(command_path, parameters)

        def exact(cli_value: str | None, receipt_name: str) -> str | None:
            receipt_value = close_identity.get(receipt_name)
            if cli_value is not None and receipt_value is not None and cli_value != receipt_value:
                raise PolicyViolation("CLI ve close receipt execution kimligi uyusmuyor")
            return cli_value or receipt_value

        return cls(
            project_ref=exact(first(tuple(project_names)), "project_ref"),
            work_ref=exact(first(tuple(work_names)), "work_ref"),
            run_ref=exact(first(("run_id",)), "run_ref"),
            session_ref=exact(
                first(("session_id", "session", "client_session")),
                "session_ref",
            ),
            client_ref=exact(first(("client_id", "client")), "client_ref"),
            authorization_ref=first(("authorization_id",)),
            candidate_ref=first(("candidate_id",)),
            trace_ref=first(("trace_id",)),
        )

    def merge_exact(self, other: CliMutationTargetHints) -> CliMutationTargetHints:
        """Merge independently captured hints without permitting scope widening."""

        values: dict[str, str | None] = {}
        for name in (
            "project_ref",
            "work_ref",
            "run_ref",
            "session_ref",
            "client_ref",
            "authorization_ref",
            "candidate_ref",
            "trace_ref",
        ):
            parsed = getattr(self, name)
            captured = getattr(other, name)
            if parsed is not None and captured is not None and parsed != captured:
                raise PolicyViolation("CLI parsed hedefi immutable evidence ile uyusmuyor")
            values[name] = parsed or captured
        return CliMutationTargetHints(**values)


@dataclass(frozen=True, slots=True)
class CliMutationEvidence:
    """Immutable, authority-free evidence captured before one CLI effect."""

    kind: str
    evidence_digest: str
    target_hints: CliMutationTargetHints = CliMutationTargetHints()
    event_type: str | None = None
    sequence: int | None = None
    previous_digest: str | None = None
    canonical_input: str | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.kind.strip() or self.kind != self.kind.strip():
            raise PolicyViolation("CLI mutation evidence kind bos veya padded olamaz")
        parse_digest(self.evidence_digest)
        if self.previous_digest is not None:
            parse_digest(self.previous_digest)
        if self.sequence is not None and self.sequence < 1:
            raise PolicyViolation("CLI mutation evidence sequence pozitif olmali")
        if self.grants_authority:
            raise PolicyViolation("CLI mutation evidence authority uretemez")
        if self.kind == "opencode-forward-event":
            if self.canonical_input is None:
                raise PolicyViolation("OpenCode forward evidence canonical input ister")
            try:
                document = json.loads(self.canonical_input)
            except json.JSONDecodeError as exc:
                raise PolicyViolation("OpenCode forward evidence JSON gecersiz") from exc
            if not isinstance(document, dict):
                raise PolicyViolation("OpenCode forward evidence object olmali")
            body = {key: value for key, value in document.items() if key != "event_digest"}
            if (
                document.get("event_digest") != self.evidence_digest
                or digest(body) != self.evidence_digest
                or body.get("schema") != "zekam-opencode-lifecycle-event/v2"
                or body.get("event_type") != self.event_type
                or type(body.get("sequence")) is not int
                or body.get("sequence") != self.sequence
                or body.get("previous_digest") != self.previous_digest
                or body.get("session_id") != self.target_hints.session_ref
                or body.get("grants_authority") is not False
            ):
                raise PolicyViolation("OpenCode forward evidence immutable event binding drift")

    @property
    def exact_first_session_created(self) -> bool:
        return (
            self.kind == "opencode-forward-event"
            and self.event_type == "session.created"
            and self.sequence == 1
            and self.previous_digest is None
            and self.canonical_input is not None
        )


@dataclass(frozen=True, slots=True)
class CliMutationRule:
    command_path: tuple[str, ...]
    requires_full_continuity: bool
    exemption: MutationAdmissionExemption | None = None
    always_mutating: bool = False
    read_only_parameter: str | None = None

    def __post_init__(self) -> None:
        if not self.command_path or any(not item.strip() for item in self.command_path):
            raise PolicyViolation("CLI mutation command path bos olamaz")
        if self.read_only_parameter is not None and not self.always_mutating:
            raise PolicyViolation("CLI mutation read-only parameter always-mutating rule ister")
        if self.exemption is not None and self.command_path not in {
            **_EXEMPT_COMMANDS,
            **_LOCAL_EFFECT_COMMANDS,
        }:
            raise PolicyViolation("CLI mutation exemption allowlist disinda")

    def is_mutating(self, parameters: Mapping[str, Any]) -> bool:
        if self.always_mutating:
            return not (
                self.read_only_parameter is not None
                and bool(parameters.get(self.read_only_parameter, False))
            )
        # Typer currently exposes the Python argument name (``apply``), while
        # direct registry callers may use the public option name (``uygula``).
        # Treat either true value as mutating so an alias mismatch can never
        # downgrade an effect to a read-only invocation.
        return any(bool(parameters.get(name, False)) for name in _APPLY_PARAMETER_NAMES)


@dataclass(frozen=True, slots=True)
class CliMutationAdmission:
    command_path: tuple[str, ...]
    mutating: bool
    requires_full_continuity: bool
    requires_existing_hydration: bool
    exemption: MutationAdmissionExemption | None
    target_hints: CliMutationTargetHints
    evidence: CliMutationEvidence | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.command_path:
            raise PolicyViolation("CLI mutation admission command path ister")
        if self.grants_authority:
            raise PolicyViolation("CLI mutation admission authority uretemez")
        if self.exemption is not None and self.requires_existing_hydration:
            raise PolicyViolation("CLI mutation exemption hydration prerequisite'i kaldirmali")
        if self.requires_existing_hydration and not self.requires_full_continuity:
            raise PolicyViolation("Hydration prerequisite yalniz full-continuity mutation ister")
        if not isinstance(self.target_hints, CliMutationTargetHints):
            raise PolicyViolation("CLI mutation admission typed target hints ister")
        if self.evidence is not None:
            self.evidence.__post_init__()

    @property
    def command(self) -> str:
        return " ".join(self.command_path)


@dataclass(frozen=True, slots=True)
class CliMutationInvocationSnapshot:
    """One immutable parsed invocation reused by admission and its effect."""

    admission: CliMutationAdmission

    def __post_init__(self) -> None:
        self.admission.__post_init__()


@dataclass(frozen=True, slots=True)
class ActiveRuntimeContinuityIdentity:
    """The one live execution identity used by the common hydration gate."""

    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    session_id: str
    client_id: str

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.client_id.strip():
            raise PolicyViolation("Aktif runtime continuity session/client kimligi bos olamaz")


# This is intentionally command-exact.  The Codex tick processes one governed
# ClaimedWork entry and its lifecycle service rechecks the live claim/envelope/
# authorization for non-session-start events.  Worker run/tick are scheduled
# catch-up surfaces; their handlers retain their own exact effect admission.
_EXEMPT_COMMANDS: dict[tuple[str, ...], MutationAdmissionExemption] = {
    ("auth", "revoke"): MutationAdmissionExemption.CONTROL_PLANE,
    ("init",): MutationAdmissionExemption.BOOTSTRAP,
    ("db", "upgrade"): MutationAdmissionExemption.BOOTSTRAP,
    ("doctor",): MutationAdmissionExemption.CONTROL_PLANE,
    ("knowledge", "ingest"): MutationAdmissionExemption.CONTROL_PLANE,
    ("knowledge", "vector-index"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "benchmark"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "campaign", "authorize"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "campaign", "run"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "capability", "authorize"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "capability", "run"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "health"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "inventory"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "opencode-embedding-probe"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "provider-authorize"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "provider-live-run"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "report"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "route", "decide"): MutationAdmissionExemption.CONTROL_PLANE,
    ("model", "route", "prepare"): MutationAdmissionExemption.CONTROL_PLANE,
    ("opencode", "spool-cleanup"): MutationAdmissionExemption.CONTROL_PLANE,
    ("oracle", "index"): MutationAdmissionExemption.CONTROL_PLANE,
    ("policy", "init"): MutationAdmissionExemption.BOOTSTRAP,
    ("project", "add"): MutationAdmissionExemption.BOOTSTRAP,
    ("project", "index"): MutationAdmissionExemption.CONTROL_PLANE,
    ("project", "integrate"): MutationAdmissionExemption.CONTROL_PLANE,
    ("project", "rebind"): MutationAdmissionExemption.CONTROL_PLANE,
    ("project", "remove"): MutationAdmissionExemption.CONTROL_PLANE,
    ("project", "restore"): MutationAdmissionExemption.CONTROL_PLANE,
    ("project", "scan"): MutationAdmissionExemption.CONTROL_PLANE,
    ("research", "report", "rebuild"): MutationAdmissionExemption.CONTROL_PLANE,
    ("scheduler", "init"): MutationAdmissionExemption.BOOTSTRAP,
    ("secret", "add"): MutationAdmissionExemption.CONTROL_PLANE,
    ("secret", "revoke"): MutationAdmissionExemption.CONTROL_PLANE,
    ("setup",): MutationAdmissionExemption.BOOTSTRAP,
    ("trace", "purge-expired"): MutationAdmissionExemption.CONTROL_PLANE,
    ("work", "create"): MutationAdmissionExemption.BOOTSTRAP,
    ("work", "relate"): MutationAdmissionExemption.CONTROL_PLANE,
    ("memory", "upgrade-apply-shadow"): MutationAdmissionExemption.BOOTSTRAP,
    ("memory", "hydration-apply"): MutationAdmissionExemption.HYDRATION,
    ("memory", "gap-repair-apply"): MutationAdmissionExemption.RECOVERY,
    ("worker", "codex-lifecycle-tick"): MutationAdmissionExemption.HYDRATION,
    ("worker", "client-runtime-bootstrap"): MutationAdmissionExemption.CONTROL_PLANE,
    ("worker", "lifecycle-template-prepare"): MutationAdmissionExemption.CONTROL_PLANE,
    ("worker", "lifecycle-template-tick"): MutationAdmissionExemption.CONTROL_PLANE,
    ("worker", "reconcile-failed-receipt"): MutationAdmissionExemption.RECOVERY,
    ("worker", "recovery-authorize"): MutationAdmissionExemption.RECOVERY,
    ("worker", "reconcile-recovery"): MutationAdmissionExemption.RECOVERY,
    ("worker", "run"): MutationAdmissionExemption.RECOVERY,
    ("worker", "tick"): MutationAdmissionExemption.RECOVERY,
}

# These effects are deliberately local and authority-free.  They must remain
# available before canonical hydration (hook/event durability) or outside the
# Work Graph (deterministic backup/protocol artifacts), but they are still
# explicit mutating surfaces and therefore may not be invisible to the common
# registry.
_LOCAL_EFFECT_COMMANDS: dict[tuple[str, ...], MutationAdmissionExemption] = {
    ("backup", "create"): MutationAdmissionExemption.LOCAL_EFFECT,
    ("backup", "verify"): MutationAdmissionExemption.LOCAL_EFFECT,
    ("client", "hook"): MutationAdmissionExemption.LOCAL_EFFECT,
    ("opencode", "event"): MutationAdmissionExemption.LOCAL_EFFECT,
    ("protocol", "generate-json-schema"): MutationAdmissionExemption.LOCAL_EFFECT,
    ("protocol", "generate-typescript"): MutationAdmissionExemption.LOCAL_EFFECT,
}

# These lifecycle commands mutate canonical PostgreSQL without an ``--uygula``
# switch.  Local authority-free spooling (``client hook`` / ``opencode event``)
# is intentionally absent: it remains an offline durable observation, not a
# canonical full-continuity mutation.
_ALWAYS_MUTATING_FULL_CONTINUITY: frozenset[tuple[str, ...]] = frozenset(
    {
        ("opencode", "pre-compact"),
        ("opencode", "forward"),
        ("trace", "reduce"),
    }
)

_FULL_CONTINUITY_FAMILIES = frozenset(
    {"client", "close", "lifecycle", "memory", "opencode", "session", "worker"}
)


class CliMutationAdmissionRegistry:
    """Classify one parsed CLI invocation without granting execution authority."""

    def __init__(self) -> None:
        rules: list[CliMutationRule] = [
            CliMutationRule(
                ("init",),
                requires_full_continuity=False,
                exemption=MutationAdmissionExemption.BOOTSTRAP,
                always_mutating=True,
                read_only_parameter="dry_run",
            ),
            CliMutationRule(
                ("db", "upgrade"),
                requires_full_continuity=False,
                exemption=MutationAdmissionExemption.BOOTSTRAP,
            ),
        ]
        for command_path, exemption in _EXEMPT_COMMANDS.items():
            if command_path in {("init",), ("db", "upgrade")}:
                continue
            rules.append(
                CliMutationRule(
                    command_path,
                    requires_full_continuity=True,
                    exemption=exemption,
                    always_mutating=command_path in _ALWAYS_MUTATING_FULL_CONTINUITY,
                )
            )
        rules.extend(
            CliMutationRule(
                command_path,
                requires_full_continuity=False,
                exemption=exemption,
                always_mutating=True,
            )
            for command_path, exemption in _LOCAL_EFFECT_COMMANDS.items()
        )
        rules.extend(
            CliMutationRule(path, requires_full_continuity=True, always_mutating=True)
            for path in sorted(_ALWAYS_MUTATING_FULL_CONTINUITY - frozenset(_EXEMPT_COMMANDS))
        )
        self._rules = {rule.command_path: rule for rule in rules}
        if len(self._rules) != len(rules):
            raise PolicyViolation("CLI mutation registry duplicate command path")

    @property
    def exemptions(self) -> tuple[tuple[tuple[str, ...], MutationAdmissionExemption], ...]:
        return tuple(
            sorted(
                {**_EXEMPT_COMMANDS, **_LOCAL_EFFECT_COMMANDS}.items(),
                key=lambda item: item[0],
            )
        )

    @property
    def always_mutating_commands(self) -> tuple[tuple[str, ...], ...]:
        """Reviewed real leaves that mutate without an apply/uygula parameter."""

        return tuple(sorted(_ALWAYS_MUTATING_FULL_CONTINUITY))

    def classify(
        self,
        command_path: tuple[str, ...],
        parameters: Mapping[str, Any] | None = None,
        *,
        evidence: CliMutationEvidence | None = None,
    ) -> CliMutationAdmission:
        path = tuple(item.strip() for item in command_path if item.strip())
        if not path:
            path = ("realm-session",)
        values = parameters or {}
        rule = self._rules.get(path)
        if rule is None:
            mutating = any(bool(values.get(name, False)) for name in _APPLY_PARAMETER_NAMES)
            # An unknown apply surface is mutation-capable until an explicit,
            # reviewed exception proves otherwise.  Command families may never
            # silently downgrade a new leaf to the non-continuity profile.
            requires_full_continuity = mutating or path[0] in _FULL_CONTINUITY_FAMILIES
            exemption = None
        else:
            mutating = rule.is_mutating(values)
            requires_full_continuity = rule.requires_full_continuity
            exemption = rule.exemption if mutating else None
        target_hints = CliMutationTargetHints.from_parameters(path, values)
        if evidence is not None:
            evidence.__post_init__()
            target_hints = target_hints.merge_exact(evidence.target_hints)
        # OpenCode may bootstrap only the exact immutable first session-created
        # event.  The command itself remains non-exempt and every later event
        # must pass normal hydration admission.
        if path == ("opencode", "forward") and evidence is not None:
            if evidence.exact_first_session_created:
                exemption = MutationAdmissionExemption.BOOTSTRAP
            else:
                exemption = None
        return CliMutationAdmission(
            command_path=path,
            mutating=mutating,
            requires_full_continuity=requires_full_continuity,
            requires_existing_hydration=(
                mutating and requires_full_continuity and exemption is None
            ),
            exemption=exemption,
            target_hints=target_hints,
            evidence=evidence,
        )

    def snapshot(
        self,
        command_path: tuple[str, ...],
        parameters: Mapping[str, Any] | None = None,
        *,
        evidence: CliMutationEvidence | None = None,
    ) -> CliMutationInvocationSnapshot:
        return CliMutationInvocationSnapshot(
            self.classify(command_path, parameters, evidence=evidence)
        )


DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY = CliMutationAdmissionRegistry()


def assert_local_effect_admission(command_path: tuple[str, ...]) -> CliMutationAdmission:
    """Fail closed unless the invocation is one reviewed authority-free local effect."""

    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(command_path)
    if (
        command_path not in _LOCAL_EFFECT_COMMANDS
        or not admission.mutating
        or admission.requires_full_continuity
        or admission.requires_existing_hydration
        or admission.exemption is not MutationAdmissionExemption.LOCAL_EFFECT
        or admission.grants_authority
    ):
        raise PolicyViolation("CLI local effect exact reviewed admission ister")
    return admission


def assert_cli_mutation_admission(
    *,
    backend: str,
    supports_full_continuity: bool,
    admission: CliMutationAdmission,
    realm_session_required: bool = False,
) -> None:
    """Apply the common CLI gate without replacing leaf authorization checks."""

    admission.__post_init__()
    assert_full_continuity_backend(
        backend=backend,
        supports_full_continuity=supports_full_continuity,
        admission=admission,
        realm_session_required=realm_session_required,
    )


def assert_full_continuity_backend(
    *,
    backend: str,
    supports_full_continuity: bool,
    admission: CliMutationAdmission,
    realm_session_required: bool = False,
) -> None:
    """Reject unsupported backends before a connection or mutation is attempted."""

    if supports_full_continuity:
        return
    if admission.mutating and admission.requires_full_continuity:
        suffix = ""
        if admission.exemption is not None:
            suffix = (
                f"; {admission.exemption.value} exemption yalniz hydration siralamasidir, "
                "backend veya authorization yetkisi vermez"
            )
        raise ZekamError(
            f"{admission.command} mutation full-continuity destekleyen PostgreSQL backend "
            f"ister{suffix}; PostgreSQL'e fallback yok"
        )
    if realm_session_required:
        raise ZekamError(
            f"Realm session backend={backend} ile desteklenmiyor; "
            "full-continuity icin PostgreSQL gerekir ve fallback yok"
        )
