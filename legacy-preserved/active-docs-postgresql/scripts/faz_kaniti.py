#!/usr/bin/env python
"""Faz kanit raporu, checkpoint ve continuity packet uretir.

Kullanim:

    python scripts/faz_kaniti.py --faz ZEKAM-P00 --gorevler ZEKAM-P00-T01 ZEKAM-P00-T02 ...

Uretilen dosyalar `.zekam/` altina yazilir ve Git'e eklenmez. Kanonik kayit runtime
uygulandiginda PostgreSQL Work Graph olur; bu dosyalar bootstrap donemi icin
kanit ve devamlilik kaydidir.

Bu kayitlar yetki devretmez: `grants_authority` her zaman `false`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ZEKAM_DIR = ROOT / ".zekam"
CHECKPOINT_DIR = ZEKAM_DIR / "checkpoints"
CONTINUITY_DIR = ZEKAM_DIR / "continuity"
PHASE_DIR = ZEKAM_DIR / "phases"
EVIDENCE_DIR = ZEKAM_DIR / "evidence"

PROJECT_ID = "zekam"
WORK_ITEM_ID = "ZEKAM-BOOTSTRAP-001"


def _canonical_json(document: Any) -> str:
    """Kararli JSON gosterimi.

    P01-T01 ile kanonik JSON kutuphanesi uygulandiginda bu yardimci onun yerine
    gecer; sozlesme (sorted keys, ayirici, NaN reddi) ayni kalir.
    """
    return json.dumps(
        document,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(document: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


def _source_revision() -> str:
    head = _git("rev-parse", "HEAD")
    if head:
        return head
    tree = _git("write-tree")
    return f"uncommitted-tree:{tree}" if tree else "uncommitted:no-index"


def _latest_quality_evidence(phase: str) -> dict[str, Any] | None:
    if not EVIDENCE_DIR.is_dir():
        return None
    candidates = sorted(EVIDENCE_DIR.glob(f"{phase}-*.json"))
    if not candidates:
        return None
    document: dict[str, Any] = json.loads(candidates[-1].read_text(encoding="utf-8"))
    return {
        "file": candidates[-1].relative_to(ROOT).as_posix(),
        "passed": document["passed"],
        "gates": {gate["gate"]: gate["exit_code"] for gate in document["gates"]},
        "digest": _digest(document),
    }


def _changed_files() -> list[str]:
    tracked = _git("diff", "--name-only", "HEAD").splitlines()
    untracked = _git("ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({*tracked, *untracked})


def build_records(
    *,
    phase: str,
    tasks: Sequence[str],
    pending: Sequence[str],
    next_safe_action: str,
    now: dt.datetime,
) -> dict[str, dict[str, Any]]:
    """Faz raporu, checkpoint ve continuity packet belgelerini uretir."""
    source_revision = _source_revision()
    quality = _latest_quality_evidence(phase)
    changed = _changed_files()

    phase_report = {
        "schema": "zekam-phase-evidence/v1",
        "phase": phase,
        "project_id": PROJECT_ID,
        "work_item_id": WORK_ITEM_ID,
        "generated_at": now.isoformat(),
        "completed_tasks": list(tasks),
        "pending_tasks": list(pending),
        "source_revision": source_revision,
        "changed_file_count": len(changed),
        "changed_files_digest": _digest(changed),
        "quality_evidence": quality,
        "grants_authority": False,
    }

    checkpoint = {
        "schema_version": 1,
        "checkpoint_id": f"{phase}-checkpoint",
        "project_id": PROJECT_ID,
        "work_item_id": WORK_ITEM_ID,
        "plan_revision_id": f"{phase}-plan-1",
        "run_id": f"{phase}-bootstrap-run",
        "plan_steps": [*tasks, *pending],
        "completed_steps": list(tasks),
        "pending_steps": list(pending),
        "step_results": [{"step_id": task, "result_digest": _digest(task)} for task in tasks],
        "source_revision": source_revision,
        "context_manifest_digest": _digest(
            {"phase": phase, "tasks": list(tasks), "source_revision": source_revision}
        ),
        "journal_head_digest": _digest({"phase": phase, "tasks": list(tasks), "truncated": False}),
        "logical_resources": [f"project:{PROJECT_ID}", f"path:{PROJECT_ID}:{phase.lower()}"],
        "next_safe_action": next_safe_action,
        "created_at": now.isoformat(),
        "grants_authority": False,
    }
    checkpoint["checkpoint_digest"] = _digest(checkpoint)

    packet = {
        "schema_version": 1,
        "packet_id": f"{phase}-continuity",
        "project_id": PROJECT_ID,
        "work_item_id": WORK_ITEM_ID,
        "goal": "GLOBAL_DEFINITION_OF_DONE.md icindeki butun kriterleri kanitli tamamla",
        "status": "active" if pending else "completed",
        "current_step": pending[0] if pending else phase,
        "completed": list(tasks),
        "pending": list(pending),
        "decisions": [],
        "risks": [],
        "first_reads": [
            "00_BASLA.md",
            "DEVAM_PROTOKOLU.md",
            "NIHAI_UYGULAMA_PROMPTU.md",
            "kalite/UYGULAMA_IS_GRAFIGI.yaml",
        ],
        "next_safe_actions": [next_safe_action],
        "authoritative_refs": [
            {
                "kind": "source",
                "ref": "kalite/UYGULAMA_IS_GRAFIGI.yaml",
                "digest": _digest("kalite/UYGULAMA_IS_GRAFIGI.yaml"),
            },
            {
                "kind": "source",
                "ref": "GLOBAL_DEFINITION_OF_DONE.md",
                "digest": _digest("GLOBAL_DEFINITION_OF_DONE.md"),
            },
        ],
        "source_revision": source_revision,
        "created_at": now.isoformat(),
        "grants_authority": False,
        "carries_active_lease": False,
        "approval_inherited": False,
    }
    packet["packet_digest"] = _digest(packet)

    return {"phase_report": phase_report, "checkpoint": checkpoint, "continuity_packet": packet}


def _write(target: Path, document: dict[str, Any]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zekam faz kaniti ve devamlilik kaydi")
    parser.add_argument("--faz", required=True, help="Faz kimligi, ornek ZEKAM-P00")
    parser.add_argument("--gorevler", nargs="+", required=True, help="Tamamlanan task kimlikleri")
    parser.add_argument("--bekleyen", nargs="*", default=[], help="Bekleyen task kimlikleri")
    parser.add_argument("--sonraki", required=True, help="Bir sonraki guvenli aksiyon")
    arguments = parser.parse_args(argv)

    now = dt.datetime.now(dt.UTC)
    records = build_records(
        phase=arguments.faz,
        tasks=arguments.gorevler,
        pending=arguments.bekleyen,
        next_safe_action=arguments.sonraki,
        now=now,
    )

    written = [
        _write(PHASE_DIR / f"{arguments.faz}-kanit.json", records["phase_report"]),
        _write(CHECKPOINT_DIR / f"{arguments.faz}-checkpoint.json", records["checkpoint"]),
        _write(CONTINUITY_DIR / f"{arguments.faz}-continuity.json", records["continuity_packet"]),
    ]
    for path in written:
        print(path.relative_to(ROOT).as_posix())

    quality = records["phase_report"]["quality_evidence"]
    if quality is None:
        print("UYARI: kalite kaniti bulunamadi; once scripts/kalite.py calistirin", file=sys.stderr)
        return 1
    if not quality["passed"]:
        print("HATA: kalite kapilari gecmedi", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
