"""Install or verify the pinned local multilingual-e5-small ONNX bundle.

The model is deliberately kept outside the repository in the user's local
application data directory.  Downloads are revision-pinned, streamed to a
temporary sibling, checked by size and SHA256, and published with an atomic
replace.  An existing verified installation is never downloaded again.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import stat
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

MODEL_REPOSITORY = "Xenova/multilingual-e5-small"
MODEL_REF = "intfloat/multilingual-e5-small"
MODEL_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
MODEL_FILE = "onnx/model_int8.onnx"
MANIFEST_SCHEMA = "zekam-local-embedding-install/v1"
HF_BASE = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}"
MAX_MANIFEST_BYTES = 256 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60

# These are the exact files required by the tokenizers/ONNX CPU runtime.  The
# model file is the only large artifact; all records remain public model
# metadata and contain no user content.
PINNED_FILES: tuple[tuple[str, int, str], ...] = (
    (
        "config.json",
        658,
        "cb99455288675345e1a4f411438d5d0adbba5fbd3a67ea4fb03c015433b996c1",
    ),
    (
        "quant_config.json",
        674,
        "59d175f15264115f18c698d76e443b5d49fc6c8c599911c421405ef4f236e87d",
    ),
    (
        "sentencepiece.bpe.model",
        5_069_051,
        "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
    ),
    (
        "special_tokens_map.json",
        167,
        "d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7",
    ),
    (
        "tokenizer_config.json",
        443,
        "a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b",
    ),
    (
        "tokenizer.json",
        17_082_730,
        "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    ),
    (
        MODEL_FILE,
        118_054_593,
        "4d24e2bc01a447951524466ef533e52944bf48509e6552810bcee1a2711cb02c",
    ),
)


def _default_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        parent = Path(local_app_data)
    elif os.name == "nt":
        parent = Path.home() / "AppData" / "Local"
    else:
        parent = Path.home() / ".local" / "share"
    return parent / "Zekam" / "models" / MODEL_REPOSITORY.replace("/", "-") / MODEL_REVISION


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & 0x400
    )


def _has_reparse_ancestor(path: Path) -> bool:
    """Reject a model root whose existing parent chain crosses a reparse point."""

    cursor = path
    while True:
        if _is_reparse(cursor):
            return True
        parent = cursor.parent
        if parent == cursor:
            return False
        cursor = parent


def _assert_safe_root(root: Path, *, create: bool) -> Path:
    if not root.is_absolute() or "\x00" in str(root):
        raise ValueError("model root absolute olmali")
    if _has_reparse_ancestor(root):
        raise ValueError("model root parent symlink/reparse olamaz")
    if root.exists() and not root.is_dir():
        raise ValueError("model root regular dizin olmali")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if _is_reparse(root) or not root.is_dir():
        if create:
            raise ValueError("model root regular dizin olmali")
        raise ValueError("model root bulunamadi veya regular dizin degil")
    return root


def _record_map() -> dict[str, dict[str, Any]]:
    return {
        relative: {
            "path": relative,
            "size_bytes": size,
            "sha256": sha,
            "upstream_sha256": sha,
        }
        for relative, size, sha in PINNED_FILES
    }


def _manifest_body(records: dict[str, dict[str, Any]], verified_at: str) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "source": f"https://huggingface.co/{MODEL_REPOSITORY}",
        "repository": MODEL_REPOSITORY,
        "distribution": MODEL_REPOSITORY,
        "source_revision": MODEL_REVISION,
        "revision": MODEL_REVISION,
        "model_ref": MODEL_REF,
        "model_file": MODEL_FILE,
        "files": [records[path] for path, _, _ in PINNED_FILES],
        "verified_at_utc": verified_at,
    }


def _read_manifest(root: Path) -> dict[str, Any] | None:
    path = root / "install-manifest.json"
    if not path.is_file() or _is_reparse(path) or path.stat().st_size > MAX_MANIFEST_BYTES:
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    return document


def _safe_relative(relative: str) -> PurePosixPath:
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or not relative
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("pinned model yolu root-relative olmali")
    return path


def _target(root: Path, relative: str) -> Path:
    parts = _safe_relative(relative).parts
    candidate = root.joinpath(*parts)
    cursor = root
    for part in parts:
        cursor = cursor / part
        if cursor.exists() and _is_reparse(cursor):
            raise ValueError("pinned model yolu symlink/reparse olamaz")
    return candidate


def _sha256(path: Path) -> tuple[int, str]:
    size = 0
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            value.update(chunk)
    return size, value.hexdigest()


def verify_installation(root: Path) -> dict[str, Any]:
    """Return a bounded verification report; never downloads or mutates."""

    if not root.is_absolute():
        raise ValueError("model root absolute olmali")
    if not root.exists():
        return {"status": "missing", "root": str(root), "missing": ["install-manifest.json"]}
    root = _assert_safe_root(root, create=False)
    manifest = _read_manifest(root)
    if manifest is None:
        return {"status": "missing", "root": str(root), "missing": ["install-manifest.json"]}
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("repository") != MODEL_REPOSITORY
        or manifest.get("revision") != MODEL_REVISION
        or manifest.get("model_ref") != MODEL_REF
        or manifest.get("model_file") != MODEL_FILE
    ):
        return {"status": "invalid", "root": str(root), "reason": "manifest-identity"}
    records = {
        item.get("path"): item
        for item in manifest.get("files", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    missing: list[str] = []
    mismatched: list[str] = []
    for relative, size, sha in PINNED_FILES:
        path = _target(root, relative)
        record = records.get(relative)
        if record is None or record.get("size_bytes") != size or record.get("sha256") != sha:
            mismatched.append(relative)
            continue
        try:
            if _is_reparse(path) or not path.is_file():
                missing.append(relative)
                continue
            actual_size, actual_sha = _sha256(path)
        except OSError:
            missing.append(relative)
            continue
        if actual_size != size or actual_sha != sha:
            mismatched.append(relative)
    status = "verified" if not missing and not mismatched else "invalid"
    return {
        "status": status,
        "root": str(root),
        "model_ref": MODEL_REF,
        "revision": MODEL_REVISION,
        "missing": sorted(missing),
        "mismatched": sorted(mismatched),
    }


def _download(relative: str, target: Path, size: int, sha: str) -> None:
    _safe_relative(relative)
    encoded = urllib.parse.quote(relative, safe="/")
    url = f"{HF_BASE}/{encoded}?download=true"
    request = urllib.request.Request(url, headers={"User-Agent": "zekam-local-e5-installer/1"})
    temporary: Path | None = None
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{target.name}.", suffix=".partial", dir=target.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                count = 0
                value = hashlib.sha256()
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > size:
                        raise ValueError(f"download size exceeds pin: {relative}")
                    stream.write(chunk)
                    value.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        if count != size or value.hexdigest() != sha:
            raise ValueError(f"download digest mismatch: {relative}")
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _write_manifest(root: Path, document: dict[str, Any]) -> None:
    payload = (json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".install-manifest.", suffix=".partial", dir=root, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "install-manifest.json")
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def install(root: Path) -> dict[str, Any]:
    root = _assert_safe_root(root, create=True)
    current = verify_installation(root)
    if current["status"] == "verified":
        return current | {"downloaded": [], "reused": True}
    if current.get("mismatched"):
        raise ValueError("existing pinned model file digest mismatch; refusing overwrite")
    records = _record_map()
    downloaded: list[str] = []
    for relative, size, sha in PINNED_FILES:
        path = _target(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and not _is_reparse(path):
            actual_size, actual_sha = _sha256(path)
            if actual_size == size and actual_sha == sha:
                continue
            raise ValueError(f"existing pinned model file digest mismatch; refusing overwrite: {relative}")
        _download(relative, path, size, sha)
        downloaded.append(relative)
    _write_manifest(
        root,
        _manifest_body(records, dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")),
    )
    result = verify_installation(root)
    if result["status"] != "verified":
        raise ValueError("local E5 installation verification failed")
    return result | {"downloaded": downloaded, "reused": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_default_root())
    parser.add_argument("--verify", action="store_true", help="only verify; never download")
    args = parser.parse_args(argv)
    root = args.root.absolute()
    result = verify_installation(root) if args.verify else install(root)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
