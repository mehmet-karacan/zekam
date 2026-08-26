#!/usr/bin/env python3
"""Zekam nihai uygulama paketinin yapisal butunlugunu dogrular."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML gerekli. Kurulum: python -m pip install 'PyYAML>=6.0'") from exc

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
RESULT_PATH = ROOT / "VALIDATION_RESULT.json"
PHASE_BASELINE_PATH = ROOT / "kalite" / "PHASE_PROJECTION_BASELINE.json"

REQUIRED_FILES = [
    "README.md",
    "00_BASLA.md",
    "DEVAM_PROTOKOLU.md",
    "NIHAI_UYGULAMA_PROMPTU.md",
    "GLOBAL_DEFINITION_OF_DONE.md",
    "PROJE_MANIFESTI.yaml",
    "AKTIF_GOREV.yaml",
    "AGENTS.md",
    "CLAUDE.md",
    "opencode.json",
    ".ai/repository-context.json",
    "mimari/ANA_MIMARI.md",
    "harness/GERCEK_AGENT_HARNESS.md",
    "bellek/BELLEK_MIMARISI_MEM0_UYARLAMASI.md",
    "modeller/KANONIK_MODEL_ENVANTERI.yaml",
    "kalite/UYGULAMA_IS_GRAFIGI.yaml",
    "kalite/PHASE_PROJECTION_BASELINE.json",
    "kalite/GLOBAL_DOD.yaml",
    "kalite/commit-template.txt",
    "referanslar/context-vault/AKTIF_GOREV_FOR_CONTEXT_VAULT.md",
]

CANONICAL_SCAN_DIRS = [
    ROOT,
    ROOT / "mimari",
    ROOT / "harness",
    ROOT / "bellek",
    ROOT / "modeller",
    ROOT / "bilgi",
    ROOT / "guvenlik",
    ROOT / "operasyon",
    ROOT / "kalite",
    ROOT / "schemas",
    ROOT / ".opencode",
    ROOT / ".ai",
]
EXCLUDED_SCAN_PARTS = {
    "referanslar",
    "yerel-referanslar",
    "VALIDATION_RESULT.json",
    "PACKAGE_MANIFEST.json",
    "SHA256SUMS.txt",
}
FORBIDDEN_CANONICAL_TERMS = [
    "z-control-plane",
    "Z Control Plane",
    "zctl",
]
IDENTITY_TEXT_SUFFIXES = frozenset(
    {".json", ".md", ".py", ".sql", ".toml", ".txt", ".yaml", ".yml"}
)
IDENTITY_TEXT_NAMES = frozenset({".dockerignore", ".gitignore"})
IDENTITY_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".zekam",
        "__pycache__",
        "yerel-referanslar",
    }
)
IDENTITY_EXCLUDED_FILES = frozenset(
    {"PACKAGE_MANIFEST.json", "SHA256SUMS.txt", "VALIDATION_RESULT.json"}
)
_REMOVED_PRODUCT_TOKEN = "".join(chr(item) for item in (101, 110, 97, 105))
_REMOVED_PRODUCT_PATTERN = re.compile(
    rf"(?i)(?<![A-Za-z]){re.escape(_REMOVED_PRODUCT_TOKEN)}(?![A-Za-z])"
)
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|password|passwd|secret)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def schema_root_is_strict(document: dict[str, Any]) -> bool:
    """Prove that every root union branch rejects unknown object fields."""

    definitions = document.get("$defs", {})

    def strict(node: Any, seen: frozenset[str] = frozenset()) -> bool:
        if not isinstance(node, dict):
            return False
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.rsplit("/", maxsplit=1)[-1]
            if name in seen:
                return False
            return strict(definitions.get(name), seen | {name})
        for keyword in ("anyOf", "oneOf"):
            branches = node.get(keyword)
            if isinstance(branches, list) and branches:
                return all(strict(branch, seen) for branch in branches)
        return node.get("type") == "object" and node.get("additionalProperties") is False

    return strict(document)


def projection_digest(values: set[str]) -> str:
    payload = json.dumps(sorted(values), ensure_ascii=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def validate_portable_phase_baseline(
    phases: list[dict[str, Any]], baseline: dict[str, Any]
) -> tuple[int, list[str]]:
    findings: list[str] = []
    if baseline.get("schema") != "zekam-phase-projection-baseline/v1":
        return 0, ["Portable phase projection schema gecersiz"]
    rows = baseline.get("phases")
    if not isinstance(rows, list):
        return 0, ["Portable phase projection listesi eksik"]
    indexed = {str(row.get("id")): row for row in rows if isinstance(row, dict)}
    if len(indexed) != len(rows):
        findings.append("Portable phase projection duplicate ID iceriyor")
    checked = 0
    for phase in phases:
        phase_id = str(phase["id"])
        row = indexed.get(phase_id)
        if row is None:
            findings.append(f"Portable phase projection eksik: {phase_id}")
            continue
        completed = {str(task["id"]) for task in phase["tasks"] if task.get("state") == "completed"}
        all_tasks = {str(task["id"]) for task in phase["tasks"]}
        pending = all_tasks - completed
        if row.get("completed_digest") != projection_digest(completed):
            findings.append(f"Portable completed projection drift: {phase_id}")
        if row.get("pending_digest") != projection_digest(pending):
            findings.append(f"Portable pending projection drift: {phase_id}")
        checked += 1
    if set(indexed) != {str(phase["id"]) for phase in phases}:
        findings.append("Portable phase projection faz kumesi graph ile uyusmuyor")
    return checked, findings


def canonical_files() -> list[Path]:
    found: set[Path] = set()
    for base in CANONICAL_SCAN_DIRS:
        if not base.exists():
            continue
        iterable = list(base.glob("*")) if base == ROOT else list(base.rglob("*"))
        for path in iterable:
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if any(part in EXCLUDED_SCAN_PARTS for part in rel.parts):
                continue
            if path.suffix.lower() in {".md", ".yaml", ".yml", ".json", ".txt"}:
                found.add(path)
    return sorted(found)


def identity_surface_files(root: Path = ROOT) -> list[Path]:
    """Git-tracked yuzeyi, local/runtime dizinlerini okumadan listeler."""
    if (root / ".git").exists():
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=True,
            capture_output=True,
        )
        candidates = (
            root / raw.decode("utf-8", errors="surrogateescape")
            for raw in completed.stdout.split(b"\0")
            if raw
        )
    else:
        candidates = root.rglob("*")

    found: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if path.name in IDENTITY_EXCLUDED_FILES:
            continue
        if any(part in IDENTITY_EXCLUDED_PARTS for part in relative.parts):
            continue
        found.append(path)
    return sorted(found)


def removed_product_identity_hits(root: Path = ROOT) -> list[str]:
    """Provider protocol adlarini karistirmadan kaldirilan urun kimligini bulur."""
    hits: list[str] = []
    for path in identity_surface_files(root):
        relative = path.relative_to(root).as_posix()
        if _REMOVED_PRODUCT_PATTERN.search(relative):
            hits.append(relative)
            continue
        if (
            path.name not in IDENTITY_TEXT_NAMES
            and path.suffix.lower() not in IDENTITY_TEXT_SUFFIXES
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _REMOVED_PRODUCT_PATTERN.search(text):
            hits.append(relative)
    return hits


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    missing = [name for name in REQUIRED_FILES if not (ROOT / name).is_file()]
    checks["required_files"] = {"expected": len(REQUIRED_FILES), "missing": missing}
    if missing:
        errors.append(f"Zorunlu dosyalar eksik: {missing}")

    try:
        manifest = load_yaml(ROOT / "PROJE_MANIFESTI.yaml")
        checks["project_identity"] = {
            "name": manifest["project"]["name"],
            "slug": manifest["project"]["slug"],
            "cli": manifest["project"]["cli"],
        }
        if manifest["project"]["name"] != "Zekam":
            errors.append("Kanonik proje adi Zekam degil.")
        if manifest["project"]["slug"] != "zekam" or manifest["project"]["cli"] != "zekam":
            errors.append("Repository/package/CLI zekam kimligiyle uyusmuyor.")
        sub = manifest["subagents"]
        if sub["agentic_operations_minimum"] != 1:
            errors.append("Agentic minimum subagent 1 olmali.")
        if sub["coordinator_counts_as_subagent"] is not False:
            errors.append("Koordinator subagent sayilmamali.")
        if sub["fixed_global_maximum"] is not None:
            errors.append("Sabit global maksimum null olmali.")
        if sub["single_builder_per_writable_logical_resource"] is not True:
            errors.append("Tek builder invariant'i eksik.")
    except Exception as exc:
        errors.append(f"PROJE_MANIFESTI.yaml okunamadi: {exc}")

    try:
        inventory = load_yaml(ROOT / "modeller/KANONIK_MODEL_ENVANTERI.yaml")
        models = inventory["models"]
        ids = [model["model_id"] for model in models]
        checks["model_inventory"] = {
            "declared_count": inventory.get("canonical_count"),
            "actual_count": len(models),
            "unique_ids": len(set(ids)),
            "technical_profile_count": inventory.get("technical_profile_count"),
        }
        if len(models) != 20 or len(set(ids)) != 20:
            errors.append("Model envanteri exact 20 unique Model ID icermeli.")
        if inventory.get("technical_profile_count") != 19:
            errors.append("19 teknik profil farki korunmamis.")
        text = (ROOT / "modeller/KANONIK_MODEL_ENVANTERI.yaml").read_text(encoding="utf-8")
        if "http://" in text or "https://" in text or "api_base:" in text:
            errors.append("Aktif model envanterinde ham endpoint bulunuyor.")
        for model in models:
            if not str(model.get("endpoint_ref", "")).startswith("model-endpoint:"):
                errors.append(f"EndpointRef eksik: {model.get('model_id')}")
            if not str(model.get("credential_ref", "")).startswith("model-credential:"):
                errors.append(f"CredentialRef eksik: {model.get('model_id')}")
    except Exception as exc:
        errors.append(f"Model envanteri okunamadi: {exc}")

    try:
        graph = load_yaml(ROOT / "kalite/UYGULAMA_IS_GRAFIGI.yaml")
        phases = graph["phases"]
        tasks = [task for phase in phases for task in phase["tasks"]]
        checks["implementation_graph"] = {
            "phase_count": len(phases),
            "task_count_declared": graph.get("task_count"),
            "task_count_actual": len(tasks),
        }
        if len(phases) < 18:
            errors.append("Uygulama is grafigi en az 18 faz icermeli.")
        if len(tasks) < 90:
            errors.append("Uygulama is grafigi en az 90 actionable task icermeli.")
        ids = [task["id"] for task in tasks]
        if len(ids) != len(set(ids)):
            errors.append("Is grafiginde duplicate task ID var.")
        known = set(ids)
        for task in tasks:
            if task.get("agentic") and task.get("minimum_subagents", 0) < 1:
                errors.append(f"Agentic task subagent ihlali: {task['id']}")
            if task.get("coordinator_counts_as_subagent") is not False:
                errors.append(f"Koordinator sayim ihlali: {task['id']}")
            for dep in task.get("dependencies", []):
                if dep not in known:
                    errors.append(f"Bilinmeyen dependency {dep} -> {task['id']}")

        baseline = load_json(PHASE_BASELINE_PATH)
        portable_count, projection_findings = validate_portable_phase_baseline(phases, baseline)
        local_projection_count = 0
        for phase in phases:
            phase_id = str(phase["id"])
            phase_task_ids = {str(task["id"]) for task in phase["tasks"]}
            expected_completed = {
                str(task["id"]) for task in phase["tasks"] if task.get("state") == "completed"
            }
            expected_pending = phase_task_ids - expected_completed
            projections = (
                (
                    (ROOT / ".zekam" / "phases" / f"{phase_id}-kanit.json",),
                    "completed_tasks",
                    "pending_tasks",
                ),
                (
                    (ROOT / ".zekam" / "checkpoints" / f"{phase_id}-checkpoint.json",),
                    "completed_steps",
                    "pending_steps",
                ),
                (
                    (ROOT / ".zekam" / "continuity" / f"{phase_id}-continuity.json",),
                    "completed",
                    "pending",
                ),
            )
            for candidates, completed_key, pending_key in projections:
                path = next((candidate for candidate in candidates if candidate.is_file()), None)
                if path is None:
                    continue
                local_projection_count += 1
                projection = load_json(path)
                actual_completed = {
                    str(item) for item in projection.get(completed_key, ())
                } & phase_task_ids
                actual_pending = {
                    str(item) for item in projection.get(pending_key, ())
                } & phase_task_ids
                if actual_completed != expected_completed or actual_pending != expected_pending:
                    projection_findings.append(
                        f"{path.relative_to(ROOT)} graph task durumuyla uyusmuyor"
                    )
        checks["phase_projection_consistency"] = {
            "checked": portable_count + local_projection_count,
            "portable_checked": portable_count,
            "local_checked": local_projection_count,
            "findings": projection_findings,
        }
        errors.extend(projection_findings)
    except Exception as exc:
        errors.append(f"Uygulama is grafigi okunamadi: {exc}")

    try:
        dod = load_yaml(ROOT / "kalite/GLOBAL_DOD.yaml")
        checks["global_dod"] = {
            "criterion_count_declared": dod.get("criterion_count"),
            "criterion_count_actual": len(dod.get("criteria", [])),
        }
        if len(dod.get("criteria", [])) < 75:
            errors.append("Global DoD yeterli kapsama sahip degil.")
    except Exception as exc:
        errors.append(f"GLOBAL_DOD.yaml okunamadi: {exc}")

    json_files = sorted((ROOT / "schemas").glob("*.json"))
    json_errors: list[str] = []
    for path in json_files:
        try:
            data = load_json(path)
            if not schema_root_is_strict(data):
                warnings.append(f"Schema root strict degil: {path.name}")
        except Exception as exc:
            json_errors.append(f"{path.name}: {exc}")
    checks["schemas"] = {"count": len(json_files), "errors": json_errors}
    if len(json_files) < 12:
        errors.append("En az 12 strict schema bekleniyor.")
    errors.extend(json_errors)

    yaml_files = [
        ROOT / "PROJE_MANIFESTI.yaml",
        ROOT / "AKTIF_GOREV.yaml",
        ROOT / "modeller/KANONIK_MODEL_ENVANTERI.yaml",
        ROOT / "kalite/UYGULAMA_IS_GRAFIGI.yaml",
        ROOT / "kalite/GLOBAL_DOD.yaml",
    ]
    yaml_errors: list[str] = []
    for path in yaml_files:
        try:
            load_yaml(path)
        except Exception as exc:
            yaml_errors.append(f"{path.name}: {exc}")
    checks["yaml"] = {"count": len(yaml_files), "errors": yaml_errors}
    errors.extend(yaml_errors)

    try:
        from zekam.application.memory_policy import load_memory_policy
        from zekam.application.memory_routing import load_memory_routing_policy
        from zekam.application.source_security import (
            apply_secret_scan_allowlist,
            scan_git_security,
        )
        from zekam.application.source_security_policy import load_secret_scan_allowlist

        memory_policy = load_memory_policy()
        routing_policy = load_memory_routing_policy()
        security_report = apply_secret_scan_allowlist(
            scan_git_security(ROOT), load_secret_scan_allowlist()
        )
        checks["memory_continuity_policy"] = {
            "classification_count": len(memory_policy.classifications),
            "initial_mode": memory_policy.initial_mode.value,
            "remote_calls_default": memory_policy.remote_calls_default,
            "policy_digest": memory_policy.policy_digest,
        }
        checks["memory_routing_policy"] = {
            "workload_count": len(routing_policy.routes),
            "provider_calls_default": routing_policy.provider_calls_default,
            "policy_digest": routing_policy.policy_digest,
        }
        checks["git_security"] = security_report.as_dict()
        if not security_report.passed:
            errors.append(
                "Git security gate basarisiz: "
                f"{len(security_report.findings)} bulgu, "
                f"history_complete={security_report.history_complete}"
            )
    except Exception as exc:
        errors.append(f"Memory continuity policy/security gate okunamadi: {type(exc).__name__}")

    template = (ROOT / "kalite/commit-template.txt").read_bytes()
    try:
        template.decode("ascii")
        ascii_ok = True
    except UnicodeDecodeError:
        ascii_ok = False
        errors.append("Commit template ASCII-only degil.")
    checks["commit_template_ascii"] = ascii_ok

    term_hits: list[str] = []
    secret_hits: list[str] = []
    for path in canonical_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT)
        for term in FORBIDDEN_CANONICAL_TERMS:
            if term in text:
                term_hits.append(f"{rel}: {term}")
        # Acikca ornek/placeholder SecretRef metinleri false positive olmamasi icin
        # yalniz uzun, deger atanmis canary kaliplarini tara.
        for pattern_index, pattern in enumerate(SECRET_PATTERNS, start=1):
            match = pattern.search(text)
            if match:
                value = match.group(0)
                if "SecretRef" not in value and "credential_ref" not in value:
                    fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
                    secret_hits.append(f"{rel}:pattern-{pattern_index}:{fingerprint}")
    checks["forbidden_canonical_terms"] = term_hits
    if term_hits:
        errors.append(f"Eski kanonik adlar aktif belgelerde bulundu: {term_hits[:10]}")
    identity_hits = removed_product_identity_hits()
    checks["removed_product_identity_hits"] = identity_hits
    if identity_hits:
        errors.append(f"Kaldirilan urun kimligi aktif yuzeylerde bulundu: {identity_hits[:10]}")
    checks["secret_pattern_hits"] = secret_hits
    if secret_hits:
        errors.append(f"Olası secret degeri aktif belgelerde bulundu: {secret_hits[:10]}")

    context_task = ROOT / "referanslar/context-vault/AKTIF_GOREV_FOR_CONTEXT_VAULT.md"
    if context_task.exists():
        context_text = context_task.read_text(encoding="utf-8")
        required_context_terms = [
            "openai/BAAI/bge-m3",
            "1024",
            "Hybrid",
            "OCR",
            "Repository",
            "Global Definition of Done",
        ]
        missing_terms = [
            term for term in required_context_terms if term.lower() not in context_text.lower()
        ]
        checks["context_vault_reference"] = {"missing_terms": missing_terms}
        if missing_terms:
            warnings.append(f"Context Vault referansinda beklenen terimler eksik: {missing_terms}")

    result = {
        "schema": "zekam-package-validation/v1",
        # Portable kayit: makineye ozel absolute path yazilmaz.
        "root": ROOT.name,
        "status": "passed" if not errors else "failed",
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
    }
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
