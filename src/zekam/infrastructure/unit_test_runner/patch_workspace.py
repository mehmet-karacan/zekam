"""Gercek proje kokunde atomik test-patch uygulama ve sahiplik kontrollu geri alma (W05).

- ``apply``: once TUM preimage'lar dogrulanir (yeni dosya: var olmamali; var olan: digest
  eslesmeli). Biri uymazsa hicbir sey yazilmaz (``PreimageMismatch``). Yazim temp dosya +
  ``os.replace`` iledir; yazim ortasinda hata olursa o ana kadar uygulananlar geri alinir.
- ``revert``: yalniz dosya hala gorevin yazdigi digest'teyse eski haline doner; baska bir
  digest (kullanici duzenlemesi) varsa dokunulmaz ve ``foreign_modified`` olarak raporlanir.
Kopya, mirror veya worktree yoktur; yollar ``resolve_inside`` ile korunur.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from zekam.application.unit_test_patch import (
    AppliedFile,
    AppliedPatch,
    PreimageMismatch,
    RevertReceipt,
    TestOnlyPolicy,
    TestPatch,
    fold_path,
)
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.unit_test_runner.path_safety import is_link_or_junction, resolve_inside


def _current_digest(path: Path) -> str | None:
    if not os.path.lexists(path):
        return None
    if is_link_or_junction(path) or not path.is_file():
        raise PolicyViolation("Hedef dosya degil ya da baglanti")
    return digest_of_bytes(path.read_bytes())


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".zekam-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


@dataclass(frozen=True, slots=True)
class FileSystemTestPatchWorkspace:
    root: Path

    def digest_paths(self, paths: Iterable[str]) -> dict[str, str | None]:
        return {p: _current_digest(resolve_inside(self.root, p)) for p in paths}

    def read_bytes(self, paths: Iterable[str]) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for path in paths:
            target = resolve_inside(self.root, path)
            if _current_digest(target) is not None:
                result[path] = target.read_bytes()
        return result

    def _revalidate_real_path(
        self, change_path: str, target: Path, modifies_existing: bool, policy: TestOnlyPolicy | None
    ) -> None:
        """Apply aninda (TOCTOU): hedefin COZULMUS gercek yolu istenenle ayni ve korumasiz olmali.

        8.3 kisa adlari, symlink/junction veya ara dizin takma adlari gercek hedefi
        degistirebilir; gercek yol (``realpath``) istenen yolla (buyuk-kucuk harf duyarsiz)
        eslesmiyorsa veya korumali kumeye dusuyorsa yazilmaz.
        """

        base = os.path.realpath(self.root)
        real = os.path.realpath(target)
        try:
            relative = Path(real).relative_to(base).as_posix()
        except ValueError as exc:
            raise PolicyViolation("Hedefin gercek yolu proje kokunun disinda") from exc
        if fold_path(relative) != fold_path(change_path):
            raise PolicyViolation("Hedefin gercek yolu istenen yolla ayni degil (takma ad)")
        if policy is not None:
            policy.check_path(relative, modifies_existing=modifies_existing)

    def apply(self, patch: TestPatch, *, policy: TestOnlyPolicy | None = None) -> AppliedPatch:
        targets = {f.path: resolve_inside(self.root, f.path) for f in patch.files}
        for change in patch.files:
            if policy is not None:
                policy.check_path(change.path, modifies_existing=change.preimage_digest is not None)
            self._revalidate_real_path(
                change.path, targets[change.path], change.preimage_digest is not None, policy
            )
        originals: dict[str, bytes | None] = {}
        for change in patch.files:
            current = _current_digest(targets[change.path])
            if current != change.preimage_digest:
                raise PreimageMismatch(change.path)
            originals[change.path] = None if current is None else targets[change.path].read_bytes()
        applied: list[AppliedFile] = []
        try:
            for change in sorted(patch.files, key=lambda f: f.path):
                _atomic_write(targets[change.path], change.content_bytes)
                applied.append(
                    AppliedFile(change.path, change.preimage_digest, change.postimage_digest)
                )
        except BaseException:
            for item in applied:
                original = originals[item.path]
                if original is None:
                    with contextlib.suppress(OSError):
                        targets[item.path].unlink()
                else:
                    with contextlib.suppress(OSError):
                        _atomic_write(targets[item.path], original)
            raise
        return AppliedPatch(tuple(applied))

    def revert(self, applied: AppliedPatch, *, preimages: Mapping[str, bytes]) -> RevertReceipt:
        restored: list[str] = []
        foreign: list[str] = []
        original: list[str] = []
        failed: list[str] = []
        for item in applied.files:
            target = resolve_inside(self.root, item.path)
            try:
                current = _current_digest(target)
                if current == item.preimage_digest:
                    original.append(item.path)
                    continue
                if current != item.postimage_digest:
                    foreign.append(item.path)
                    continue
                if item.preimage_digest is None:
                    target.unlink()
                else:
                    data = preimages.get(item.path)
                    if data is None or digest_of_bytes(data) != item.preimage_digest:
                        failed.append(item.path)
                        continue
                    _atomic_write(target, data)
                restored.append(item.path)
            except (OSError, PolicyViolation):
                failed.append(item.path)
        return RevertReceipt(tuple(restored), tuple(foreign), tuple(original), tuple(failed))
