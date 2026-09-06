from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _read(url: str, *, timeout: float = 1.0) -> tuple[int, dict[str, str], bytes]:
    with urlopen(url, timeout=timeout) as response:
        headers = {key.casefold(): value for key, value in response.headers.items()}
        return int(response.status), headers, response.read()


def test_ui_serve_real_http_is_read_only_and_degrades_without_realm(tmp_path: Path) -> None:
    port = _free_port()
    executable = shutil.which("zekam")
    assert executable is not None, "installed zekam console script is required"
    process = subprocess.Popen(
        (
            executable,
            "ui",
            "serve",
            "--home",
            str(tmp_path / "home"),
            "--port",
            str(port),
            "--refresh-ms",
            "500",
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 12
        while True:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                raise AssertionError(f"ui serve erken kapandi: {stdout[-200:]} {stderr[-200:]}")
            try:
                status, _, body = _read(f"{base}/api/observatory/health")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("ui serve 12 saniyede hazir olmadi") from None
                time.sleep(0.1)

        assert status == 200
        assert json.loads(body)["read_only"] is True
        index_status, _, index = _read(f"{base}/")
        snapshot_status, headers, snapshot_body = _read(
            f"{base}/api/observatory/snapshot",
            timeout=10,
        )
        snapshot = json.loads(snapshot_body)

        assert index_status == 200
        assert snapshot_status == 200
        assert "Zekam Canlı Yürütme Gözleme Merkezi" in index.decode("utf-8")
        assert snapshot["runtime"] == {"available": False, "detail": "local-core-unavailable"}
        assert snapshot["read_only"] is True
        assert snapshot["grants_authority"] is False
        assert headers["cache-control"] == "no-store"
        assert str(tmp_path) not in json.dumps(snapshot, ensure_ascii=False)

        request = Request(f"{base}/api/observatory/snapshot", data=b"{}", method="POST")
        try:
            urlopen(request, timeout=1)
        except HTTPError as exc:
            assert exc.code == 405
        else:
            raise AssertionError("observatory mutation istegini reddetmedi")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
