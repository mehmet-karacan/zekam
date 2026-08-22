#!/usr/bin/env python
"""Yerel kalite kapilarini calistirir ve yeniden uretilebilir kanit uretir.

Kullanim:

    python scripts/kalite.py                 # butun kapilar
    python scripts/kalite.py lint tip        # secili kapilar
    python scripts/kalite.py --kanit-yok     # kanit dosyasi yazmadan

Kanit dosyasi `.zekam/evidence/` altina yazilir; bu dizin Git'e eklenmez.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / ".zekam" / "evidence"

#: Kapi adi -> calistirilacak komut (yorumlayici `python` ile degistirilir).
GATES: dict[str, list[str]] = {
    "bicim": ["python", "-m", "ruff", "format", "--check", "."],
    "lint": ["python", "-m", "ruff", "check", "."],
    "tip": ["python", "-m", "mypy"],
    "test": ["python", "-m", "pytest", "-q"],
    # Bagimlilik audit: editable kurulan Zekam'nin kendisi atlanir.
    "bagimlilik": [
        "python",
        "-m",
        "pip_audit",
        "--skip-editable",
        "--progress-spinner",
        "off",
    ],
    # Olu kod: yalniz yuksek guvenli bulgular kapi olur. Protokolun dayattigi
    # kullanilmayan parametreler alt cizgi onekiyle isaretlenir.
    "olu-kod": ["python", "-m", "vulture", "src/zekam", "--min-confidence", "80"],
}

#: Bu kapilar dis ag erisimi ister; cevrimdisi ortamda acikca atlanabilir.
NETWORK_GATES = frozenset({"bagimlilik"})


def _interpreter() -> str:
    return sys.executable


def _run(command: Sequence[str]) -> dict[str, Any]:
    resolved = [_interpreter() if part == "python" else part for part in command]
    started = dt.datetime.now(dt.UTC)
    completed = subprocess.run(
        resolved,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    finished = dt.datetime.now(dt.UTC)
    output = completed.stdout + completed.stderr
    return {
        "command": " ".join(command),
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "output_digest": hashlib.sha256(output.encode("utf-8", "replace")).hexdigest(),
        "output_tail": output.strip().splitlines()[-20:],
    }


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zekam kalite kapilari")
    parser.add_argument(
        "gates",
        nargs="*",
        choices=list(GATES),
        help=f"Calistirilacak kapilar ({', '.join(GATES)}); bos ise hepsi",
    )
    parser.add_argument("--kanit-yok", action="store_true", help="Kanit dosyasi yazma")
    parser.add_argument("--gorev", default=None, help="Kanit dosyasina yazilacak task kimligi")
    parser.add_argument(
        "--cevrimdisi",
        action="store_true",
        help=f"Ag erisimi isteyen kapilari atla ({', '.join(sorted(NETWORK_GATES))})",
    )
    arguments = parser.parse_args(argv)

    selected = list(arguments.gates) or list(GATES)
    skipped: list[str] = []
    if arguments.cevrimdisi:
        # Atlanan kapi gorunur kalir: kanit dosyasinda ve ciktida raporlanir.
        skipped = [name for name in selected if name in NETWORK_GATES]
        selected = [name for name in selected if name not in NETWORK_GATES]
    results = [_run(GATES[name]) | {"gate": name} for name in selected]
    passed = all(result["passed"] for result in results)

    report: dict[str, Any] = {
        "schema": "zekam-quality-evidence/v1",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "task": arguments.gorev,
        "passed": passed,
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "postgres_test_target": bool(os.environ.get("ZEKAM_TEST_DATABASE_HOST")),
        },
        "git": {
            "head": _git("rev-parse", "HEAD") or None,
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or None,
            "dirty": bool(_git("status", "--porcelain")),
        },
        "gates": results,
        "skipped_gates": skipped,
    }

    for name in skipped:
        print(f"[ATLANDI] {name}: cevrimdisi mod")
    for result in results:
        status = "GECTI" if result["passed"] else "KALDI"
        print(f"[{status}] {result['gate']}: {result['command']}")
        if not result["passed"]:
            for line in result["output_tail"]:
                print(f"    {line}")

    if not arguments.kanit_yok:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"{arguments.gorev or 'kalite'}-{stamp}.json"
        target = EVIDENCE_DIR / name
        target.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"Kanit: {target.relative_to(ROOT).as_posix()}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
