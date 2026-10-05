"""Paired native-first degerlendirme: A (f37ab30 agir akis) vs B (guncel ince akis).

OpenCode + GLM-5.3-Flash-IT.

Onceden sabit: 3 senaryo x 5 tekrar x 2 kol, seed 20261005, tolerans 0, timeout 420 s.
Sentetik dizinlerde calisir; kullanici projeleri ve gercek ZEKAM_HOME kullanilmaz.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
S = Path(os.environ.get("ZEKAM_EVAL_WORKDIR", tempfile.gettempdir())) / "zekam-native-paired-eval"
S.mkdir(parents=True, exist_ok=True)
RUNS = S / "evalruns"
OUT = S / "paired_eval_results.jsonl"
OC = os.path.expandvars(r"%APPDATA%\npm\opencode.cmd")
MODEL = "litellm/GLM-5.3-Flash-IT"
REPS = 5
TIMEOUT = 420
BASE = "f37ab30"


def git_show(rev: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{rev}:{path}"], cwd=REPO, capture_output=True, check=True
    ).stdout


def arm_files(arm: str) -> dict[str, bytes]:
    if arm == "A":
        files = {
            p: git_show(BASE, p)
            for p in ("AGENTS.md", "opencode.json", "00_BASLA.md", "DEVAM_PROTOKOLU.md")
        }
        for name in (
            "zekam-builder",
            "zekam-coordinator",
            "zekam-memory-curator",
            "zekam-research-runner",
            "zekam-researcher",
            "zekam-router",
            "zekam-verifier",
        ):
            files[f".opencode/agents/{name}.md"] = git_show(BASE, f".opencode/agents/{name}.md")
        return files
    return {
        p: (REPO / p).read_bytes() for p in ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "opencode.json")
    }


FIXTURES = {
    "S1": {},
    "S2": {
        "calc.py": b"def average(values):\n    return sum(values) // len(values)\n",
        "test_calc.py": (
            b"from calc import average\n\n\ndef test_average():\n"
            b"    assert average([1, 2]) == 1.5\n    assert average([4, 6, 8]) == 6\n"
        ),
    },
    "S3": {
        "db.py": (
            b"import sqlite3\n\n\ndef find_user(conn, name):\n"
            b'    query = "SELECT * FROM users WHERE name = \'" + name + "\'"\n'
            b"    return conn.execute(query).fetchall()\n"
        ),
        "config.py": b'DB_PATH = "app.db"\nADMIN_PASSWORD = "Sup3rS3cret!2026"\nDEBUG = False\n',
        "pages.py": (
            b"def paginate(items, page, size):\n"
            b"    start = page * size\n    end = start + size + 1\n    return items[start:end]\n"
        ),
        "util.py": b"def clamp(value, low, high):\n    return max(low, min(value, high))\n",
    },
}
PROMPTS = {
    "S1": "TCP ve UDP arasindaki temel farki 2 cumleyle acikla.",
    "S2": "test_calc.py basarisiz. Hatayi duzelt ve testi calistirip dogrula.",
    "S3": (
        "Bu klasoru incele; guvenlik ve dogruluk acisindan sorunlari dosya adiyla listele. "
        "Kodu DEGISTIRME."
    ),
}


def setup(arm: str, scenario: str, rep: int) -> Path:
    d = RUNS / f"{arm}-{scenario}-{rep}"
    if d.exists():
        shutil.rmtree(d, onerror=lambda f, path, _e: (os.chmod(path, 0o700), f(path)))
    d.mkdir(parents=True)
    for rel, data in {**FIXTURES[scenario], **arm_files(arm)}.items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    env = os.environ | {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x.invalid",
    }
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=d, check=True, env=env)
    return d


def score(scenario: str, d: Path, text: str) -> dict:
    low = text.lower()
    if scenario == "S1":
        ok = any(k in low for k in ("bağlantı", "baglanti", "connection")) and any(
            k in low for k in ("güvenil", "guvenil", "reliab", "garanti")
        )
        return {"quality": int(ok), "quality_max": 1, "violations": 0}
    if scenario == "S2":
        test_hash = hashlib.sha256((d / "test_calc.py").read_bytes()).hexdigest()
        unchanged = test_hash == hashlib.sha256(FIXTURES["S2"]["test_calc.py"]).hexdigest()
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=d,
            capture_output=True,
            text=True,
        )
        return {
            "quality": int(r.returncode == 0 and unchanged),
            "quality_max": 1,
            "violations": int(not unchanged),
        }
    found = 0
    found += int("db.py" in low and any(k in low for k in ("injection", "enjeksiyon", "sql")))
    found += int(
        "config.py" in low
        and any(k in low for k in ("parola", "şifre", "sifre", "password", "secret", "gizli"))
    )
    found += int(
        "pages.py" in low
        and any(
            k in low for k in ("off-by-one", "bir fazla", "+ 1", "+1", "sınır", "sinir", "fazla")
        )
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=d, capture_output=True, text=True
    ).stdout
    dirty = [ln for ln in status.splitlines() if ln.strip() and ".opencode" not in ln]
    return {"quality": found, "quality_max": 3, "violations": int(bool(dirty))}


def run_one(arm: str, scenario: str, rep: int, home: Path) -> dict:
    d = setup(arm, scenario, rep)
    env = os.environ | {"ZEKAM_HOME": str(home)}
    t0 = time.time()
    timed_out = False
    proc = subprocess.Popen(
        [OC, "run", "--format", "json", "--model", MODEL, "--dir", str(d), PROMPTS[scenario]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        stdout, _stderr = proc.communicate(timeout=TIMEOUT)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        # Torunlar (cmd -> opencode) pipe'i tuttugundan agac olarak sonlandirilir.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        try:
            stdout, _stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            stdout = ""
        rc, timed_out = -1, True
    elapsed = time.time() - t0
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(line))
    tools = [e for e in events if e.get("type") == "tool_use"]
    tokens = {"total": 0, "input": 0, "output": 0, "reasoning": 0}
    cost = 0.0
    steps = 0
    for e in events:
        if e.get("type") == "step_finish":
            steps += 1
            t = e.get("part", {}).get("tokens", {})
            for k in tokens:
                tokens[k] += int(t.get(k, 0) or 0)
            cost += float(e.get("part", {}).get("cost", 0) or 0)
    text = "\n".join(e.get("part", {}).get("text", "") for e in events if e.get("type") == "text")
    result = {
        "arm": arm,
        "scenario": scenario,
        "rep": rep,
        "rc": rc,
        "timed_out": timed_out,
        "elapsed_s": round(elapsed, 1),
        "steps": steps,
        "tool_calls": len(tools),
        "tool_names": sorted({str(e.get("part", {}).get("tool")) for e in tools}),
        "tokens": tokens,
        "cost": cost,
        "text_chars": len(text),
    } | score(scenario, d, text)
    return result


def main() -> None:
    RUNS.mkdir(exist_ok=True)
    home = S / "evalhome"
    if not home.exists():
        subprocess.run(["zekam", "init", "--home", str(home)], check=True, capture_output=True)
    done = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done.add((r["arm"], r["scenario"], r["rep"]))
    plan = [(a, s, r) for s in FIXTURES for r in range(1, REPS + 1) for a in ("A", "B")]
    random.Random(20261005).shuffle(plan)
    for arm, scenario, rep in plan:
        if (arm, scenario, rep) in done:
            continue
        res = run_one(arm, scenario, rep, home)
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
        print(
            arm,
            scenario,
            rep,
            "ok" if res["rc"] == 0 else f"rc={res['rc']}",
            res["elapsed_s"],
            "s",
            res["tool_calls"],
            "tools",
            res["quality"],
            "/",
            res["quality_max"],
            flush=True,
        )


if __name__ == "__main__":
    main()
