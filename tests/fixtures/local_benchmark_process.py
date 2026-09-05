"""Test-only JSON process implementing tested and verifier contracts."""

from __future__ import annotations

import json
import sys

request = json.load(sys.stdin)
if request["schema"] == "zekam-benchmark-tested-request/v1":
    response = {
        "schema": "zekam-benchmark-tested-result/v1",
        "model_id": request["model_id"],
        "status": "passed",
        "parse_ok": True,
        "format_ok": True,
        "evidence_ok": True,
        "quality": 0.9,
        "reliability": 0.9,
        "tool_correctness": 1.0,
        "recovery": 1.0,
        "latency_ms": 4,
        "input_tokens": 8,
        "output_tokens": 3,
        "actual_cost": 0.0,
        "response": {
            "case": request["fixture"]["case_id"],
            "model": request["model_id"],
            "answer": "ok",
        },
    }
elif request["schema"] == "zekam-benchmark-verifier-request/v1":
    response = {
        "schema": "zekam-benchmark-verifier-result/v1",
        "tested_model_id": request["tested_model_id"],
        "verifier_model_id": request["verifier_model_id"],
        "tested_response_digest": request["tested_response_digest"],
        "approved": True,
        "evidence": {"contract": "independent-check"},
    }
    if "--stale" in sys.argv:
        response["tested_response_digest"] = "sha256:" + "0" * 64
else:
    raise SystemExit(2)
json.dump(response, sys.stdout)
