"""Adapter testleri icin gercek bir alt surec istemcisi.

Zekam calisma zamaninin parcasi degildir; yalnizca `tests/` altinda kullanilir.
Davranis `ZEKAM_FAKE_CLIENT_MODE` ile secilir: success, bad-json, unknown-outcome,
failed veya hang.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assignment-id", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--instruction-digest", required=True)
    parser.add_argument("--context-digest", required=True)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()

    mode = os.environ.get("ZEKAM_FAKE_CLIENT_MODE", "success")
    if mode == "hang":
        time.sleep(60)
        return 0
    if mode == "bad-json":
        sys.stdout.write("bu JSON degil")
        return 0
    if mode == "unknown-outcome":
        sys.stdout.write(json.dumps({"outcome": "harika", "payload": {}}))
        return 0
    if mode == "failed":
        sys.stdout.write(
            json.dumps(
                {"outcome": "failed", "exit_code": 2, "payload": {}, "failure_category": "adapter"}
            )
        )
        return 2

    sys.stdout.write(
        json.dumps(
            {
                "outcome": "success",
                "exit_code": 0,
                "payload": {
                    "role": arguments.role,
                    "instruction_digest": arguments.instruction_digest,
                    "context_digest": arguments.context_digest,
                    "finding_count": 2,
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
