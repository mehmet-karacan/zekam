"""Kaynak kesfi: yoksayma, sinirlar, ikili tespiti ve agac digest'i."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from zekam.application.source_discovery import (
    BINARY_PROBE_BYTES,
    DiscoveryPolicy,
    FileKind,
    InventoryEntry,
    InventoryStatus,
    SkipReason,
    StopReason,
    assert_within_root,
    compute_tree_digest,
    discover,
)
from zekam.domain.canonical import DIGEST_PREFIX
from zekam.domain.errors import PolicyViolation

pytestmark = pytest.mark.unit


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    root = tmp_path / "ornek"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "node_modules" / "paket").mkdir(parents=True)
    (root / "src" / "ana.py").write_text("print('merhaba')\n", encoding="utf-8")
    (root / "tests" / "test_ana.py").write_text("def test_ana(): pass\n", encoding="utf-8")
    (root / "README.md").write_text("# Ornek\n", encoding="utf-8")
    (root / "node_modules" / "paket" / "index.js").write_text(
        "module.exports={}\n", encoding="utf-8"
    )
    (root / "gecici.log").write_text("kayit\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    return root


def _paths(root: Path) -> set[str]:
    return {item.relative_path for item in discover(root).files}


def test_source_files_are_discovered(sample_project: Path) -> None:
    found = _paths(sample_project)
    assert "src/ana.py" in found
    assert "tests/test_ana.py" in found
    assert "README.md" in found


def test_system_denied_directory_is_excluded(sample_project: Path) -> None:
    found = _paths(sample_project)
    assert not any(path.startswith("node_modules") for path in found)


def test_generated_typescript_build_info_is_excluded(sample_project: Path) -> None:
    generated = sample_project / "gpu-ui" / "tsconfig.tsbuildinfo"
    generated.parent.mkdir()
    generated.write_text('{"program":{"fileNames":[]}}', encoding="utf-8")

    report = discover(sample_project)

    assert "gpu-ui/tsconfig.tsbuildinfo" not in {item.relative_path for item in report.files}
    assert "gpu-ui/tsconfig.tsbuildinfo" in {
        item.relative_path for item in report.skipped_by(SkipReason.IGNORED)
    }


def test_gitignore_is_applied(sample_project: Path) -> None:
    assert "gecici.log" not in _paths(sample_project)


def test_zekamignore_extends_gitignore(sample_project: Path) -> None:
    (sample_project / ".zekamignore").write_text("README.md\n", encoding="utf-8")
    assert "README.md" not in _paths(sample_project)


def test_removed_product_ignore_file_is_not_read(sample_project: Path) -> None:
    removed_slug = "".join(chr(item) for item in (101, 110, 97, 105))
    (sample_project / f".{removed_slug}ignore").write_text("README.md\n", encoding="utf-8")
    assert "README.md" in _paths(sample_project)


def test_skipped_paths_carry_reason(sample_project: Path) -> None:
    report = discover(sample_project)
    reasons = {item.reason for item in report.skipped}
    assert SkipReason.IGNORED in reasons


def test_discovery_never_writes_to_source(sample_project: Path) -> None:
    before = {path: path.stat().st_mtime_ns for path in sorted(sample_project.rglob("*"))}
    discover(sample_project)
    after = {path: path.stat().st_mtime_ns for path in sorted(sample_project.rglob("*"))}
    assert before == after
    assert set(before) == set(after)


def test_tree_digest_is_stable_across_runs(sample_project: Path) -> None:
    assert discover(sample_project).tree_digest == discover(sample_project).tree_digest


def test_tree_digest_changes_with_content(sample_project: Path) -> None:
    before = discover(sample_project).tree_digest
    (sample_project / "src" / "ana.py").write_text("print('degisti')\n", encoding="utf-8")
    assert discover(sample_project).tree_digest != before


def test_tree_digest_changes_when_file_is_added(sample_project: Path) -> None:
    before = discover(sample_project).tree_digest
    (sample_project / "src" / "yeni.py").write_text("x = 1\n", encoding="utf-8")
    assert discover(sample_project).tree_digest != before


def test_tree_digest_of_empty_set_is_defined() -> None:
    assert compute_tree_digest([]).startswith("sha256:")


def test_binary_file_is_recorded_but_not_scanned(sample_project: Path) -> None:
    (sample_project / "veri.bin").write_bytes(b"\x00\x01\x02binary")
    report = discover(sample_project)
    binary = next(item for item in report.files if item.relative_path == "veri.bin")
    assert binary.is_text is False


def test_large_file_is_skipped(sample_project: Path) -> None:
    (sample_project / "buyuk.txt").write_text("x" * 5000, encoding="utf-8")
    report = discover(sample_project, policy=DiscoveryPolicy(max_file_bytes=1000))
    assert "buyuk.txt" in {item.relative_path for item in report.skipped_by(SkipReason.TOO_LARGE)}


def test_file_count_limit_truncates(sample_project: Path) -> None:
    report = discover(sample_project, policy=DiscoveryPolicy(max_total_files=1))
    assert report.truncated
    assert report.file_count == 1
    assert report.skipped_by(SkipReason.LIMIT_REACHED)


def test_total_byte_limit_truncates(sample_project: Path) -> None:
    report = discover(sample_project, policy=DiscoveryPolicy(max_total_bytes=10))
    assert report.truncated


def test_file_with_secret_is_excluded_from_index(sample_project: Path) -> None:
    (sample_project / "ayarlar.py").write_text('api_key = "abcdefgh12345678"\n', encoding="utf-8")
    report = discover(sample_project)
    assert "ayarlar.py" not in {item.relative_path for item in report.files}
    assert report.secrets
    assert report.secrets[0].relative_path == "ayarlar.py"


def test_yaml_with_unquoted_secret_is_excluded_from_index(sample_project: Path) -> None:
    (sample_project / "application.yaml").write_text(
        "datasource:\n  password: synthetic-value-12345\n",
        encoding="utf-8",
    )
    report = discover(sample_project)
    assert "application.yaml" not in {item.relative_path for item in report.files}
    assert any(item.relative_path == "application.yaml" for item in report.secrets)


def test_secret_scan_can_be_disabled(sample_project: Path) -> None:
    (sample_project / "ayarlar.py").write_text('api_key = "abcdefgh12345678"\n', encoding="utf-8")
    report = discover(sample_project, policy=DiscoveryPolicy(scan_secrets=False))
    assert "ayarlar.py" in {item.relative_path for item in report.files}
    assert report.secrets == ()


def test_report_never_contains_secret_value(sample_project: Path) -> None:
    (sample_project / "ayarlar.py").write_text('api_key = "SuperGizliDeger123"\n', encoding="utf-8")
    assert "SuperGizliDeger123" not in repr(discover(sample_project).as_dict())


def test_extension_counts_are_reported(sample_project: Path) -> None:
    extensions = discover(sample_project).extensions
    assert extensions["py"] == 2
    assert extensions["md"] == 1


def test_root_must_be_directory(tmp_path: Path) -> None:
    target = tmp_path / "dosya.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises((PolicyViolation, NotADirectoryError, FileNotFoundError)):
        discover(target)


def test_assert_within_root_accepts_child(tmp_path: Path) -> None:
    child = tmp_path / "alt"
    child.mkdir()
    assert assert_within_root(child, tmp_path) == child.resolve()


def test_assert_within_root_rejects_sibling(tmp_path: Path) -> None:
    root = tmp_path / "kok"
    root.mkdir()
    sibling = tmp_path / "kardes"
    sibling.mkdir()
    with pytest.raises(PolicyViolation):
        assert_within_root(sibling, root)


@pytest.mark.skipif(os.name == "nt", reason="Windows'ta symlink olusturmak yonetici yetkisi ister")
def test_symlink_is_not_followed(sample_project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "disarida"
    outside.mkdir()
    (outside / "sizinti.txt").write_text("gizli", encoding="utf-8")
    (sample_project / "baglanti").symlink_to(outside, target_is_directory=True)
    report = discover(sample_project)
    assert "baglanti" in {item.relative_path for item in report.skipped_by(SkipReason.SYMLINK)}
    assert not any("sizinti" in item.relative_path for item in report.files)


# --- Metadata envanteri (WP-04.01/02) ------------------------------------------


def _make_junction(link: Path, target: Path) -> bool:
    """Windows'ta `mklink /J` ile junction olusturmaya calisir; yetki yoksa False."""
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and link.exists()


def test_inventory_records_ignored_directories_as_metadata(sample_project: Path) -> None:
    # node_modules, .git ve .env icerik olarak okunmaz ama metadata envanterinde
    # yolu/turu/buyukluguyla gorunur. Yoksayilan agacin ICINDAKI dosyalar da
    # (orn. .git/config, node_modules/paket/index.js) metadata olarak tasinir.
    (sample_project / ".git" / "objects").mkdir(parents=True)
    (sample_project / ".git" / "config").write_text("[core]\n  bare = false\n", encoding="utf-8")
    (sample_project / ".env").write_text("DB_PASSWORD=SyntheticEnvDeger123\n", encoding="utf-8")

    report = discover(sample_project)
    by_path = {item.relative_path: item for item in report.inventory}

    assert "node_modules" in by_path
    assert by_path["node_modules"].kind is FileKind.DIRECTORY
    assert by_path["node_modules"].skip_reason is SkipReason.IGNORED
    assert ".git" in by_path
    assert by_path[".git"].kind is FileKind.DIRECTORY
    assert by_path[".git"].skip_reason is SkipReason.IGNORED
    assert ".env" in by_path
    assert by_path[".env"].kind is FileKind.FILE
    assert by_path[".env"].skip_reason is SkipReason.IGNORED

    # Yoksayilan agacin icindeki DOSYALAR da envanterde metadata olarak gorunur.
    assert ".git/config" in by_path
    git_cfg = by_path[".git/config"]
    assert git_cfg.kind is FileKind.FILE
    assert git_cfg.size_bytes > 0
    assert git_cfg.is_text is None  # icerik okunmadi
    assert git_cfg.skip_reason is SkipReason.IGNORED

    assert "node_modules/paket/index.js" in by_path
    nm_idx = by_path["node_modules/paket/index.js"]
    assert nm_idx.kind is FileKind.FILE
    assert nm_idx.size_bytes > 0
    assert nm_idx.is_text is None  # icerik okunmadi
    assert nm_idx.skip_reason is SkipReason.IGNORED

    # icerik eligibility degismez: yoksayilan girdiler indeks dosyalari disinda.
    indexed = {item.relative_path for item in report.files}
    assert ".env" not in indexed
    assert ".git/config" not in indexed
    assert "node_modules/paket/index.js" not in indexed
    assert not any(path.startswith("node_modules/") for path in indexed)
    # envanter COMPLETE calisir: sinir yok, okunamayan dizin yok.
    assert report.inventory_status is InventoryStatus.COMPLETE
    assert report.inventory_stop_reason is None
    assert report.inventory_checkpoint == ()


def test_ignored_tree_metadata_collected_without_content_read(sample_project: Path) -> None:
    # Yoksayilan agac icine metadata icin inilir ama icerik ASLA acilmaz.
    # Path.open icin sayici kur; .git/config ve node_modules/paket/index.js
    # icerigi okunmadan envantere girse bile open sayisi 0 kalir.
    (sample_project / ".git" / "objects").mkdir(parents=True)
    (sample_project / ".git" / "config").write_text("gizli-git-config-deger\n", encoding="utf-8")
    # node_modules/paket/index.js zaten fixture'da mevcut.

    open_calls: list[str] = []

    original_open = Path.open

    def _counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        open_calls.append(self.name)
        return original_open(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "open", _counting_open)
        report = discover(sample_project)

    # .git/config ve node_modules/paket/index.js envanterde metadata olarak.
    by_path = {item.relative_path: item for item in report.inventory}
    assert ".git/config" in by_path
    assert "node_modules/paket/index.js" in by_path
    # Icerikleri okunmadi: is_text None, digest yok (metadata only).
    assert by_path[".git/config"].is_text is None
    assert by_path["node_modules/paket/index.js"].is_text is None
    # Yoksayilan agac icerigi ASLA acilmaz.
    assert "config" not in open_calls
    assert "index.js" not in open_calls
    # Secret degeri envantere/rapora yazilmaz.
    assert "gizli-git-config-deger" not in repr(report.as_dict())
    # Eligible dosyalar (src/ana.py vb.) henuz okunur, yalniz yoksayilanlar
    # okunmaz.
    assert "ana.py" in open_calls


def test_ignored_env_content_never_read_and_secret_not_in_inventory(
    sample_project: Path,
) -> None:
    # `.env` yoksayildigi icin icerigi okunmaz, secret degeri envantere ya da
    # rapora hic yazilmaz; yalnizca yol/tur/buyukluk metadata olarak kalinir.
    (sample_project / ".env").write_text("DB_PASSWORD=ZorluEnvSecret456\n", encoding="utf-8")

    report = discover(sample_project)
    entry = next(item for item in report.inventory if item.relative_path == ".env")
    assert entry.kind is FileKind.FILE
    assert entry.size_bytes > 0
    assert entry.is_text is None  # icerik okunmadi
    assert entry.skip_reason is SkipReason.IGNORED

    rendered = repr(report.as_dict())
    assert "ZorluEnvSecret456" not in rendered
    assert not any("ZorluEnvSecret456" in repr(item.as_dict()) for item in report.inventory)
    # icerik okunmadigi icin secret taramasi tetiklenmez, finding yok.
    assert report.secrets == ()


def test_inventory_records_binary_and_generated_metadata(sample_project: Path) -> None:
    (sample_project / "veri.bin").write_bytes(b"\x00\x01\x02\x03binary-icerik")
    (sample_project / "gpu-ui").mkdir()
    (sample_project / "gpu-ui" / "tsconfig.tsbuildinfo").write_text(
        '{"program":{"fileNames":[]}}', encoding="utf-8"
    )

    report = discover(sample_project)
    by_path = {item.relative_path: item for item in report.inventory}

    # Ikili dosya envanterde metadata olarak gorunur (elastigi disinda kaliyor).
    assert "veri.bin" in by_path
    assert by_path["veri.bin"].kind is FileKind.FILE
    assert by_path["veri.bin"].size_bytes == len(b"\x00\x01\x02\x03binary-icerik")

    # Uretilmis .tsbuildinfo envanterde metadata olarak gorunur.
    assert "gpu-ui/tsconfig.tsbuildinfo" in by_path
    assert by_path["gpu-ui/tsconfig.tsbuildinfo"].kind is FileKind.FILE
    assert by_path["gpu-ui/tsconfig.tsbuildinfo"].skip_reason is SkipReason.IGNORED

    # Eligibility: .bin indeks dosyalari icinde, .tsbuildinfo disinda.
    indexed = {item.relative_path for item in report.files}
    assert "veri.bin" in indexed
    assert "gpu-ui/tsconfig.tsbuildinfo" not in indexed


def test_inventory_traversed_directories_are_recorded(sample_project: Path) -> None:
    report = discover(sample_project)
    dirs = {
        item.relative_path
        for item in report.inventory
        if item.kind is FileKind.DIRECTORY and item.skip_reason is None
    }
    # Izlenen normal dizinler envanterde metadata olarak gorunur.
    assert "src" in dirs
    assert "tests" in dirs
    # node_modules izlenmedigi icin "normal dizin" olarak tasinmaz.
    assert "node_modules" not in dirs


def test_file_count_limit_keeps_complete_metadata_inventory(
    sample_project: Path,
) -> None:
    report = discover(sample_project, policy=DiscoveryPolicy(max_total_files=1))

    # Icerik eligibility siniri metadata taramasini eksik birakmaz.
    assert report.truncated
    assert report.file_count == 1
    assert report.inventory_status is InventoryStatus.COMPLETE
    assert report.inventory_stop_reason is None
    assert report.inventory_checkpoint == ()
    # Kesilen dosyalar envanterde LIMIT_REACHED metadata olarak gorunur.
    limited = [
        item
        for item in report.inventory
        if item.kind is FileKind.FILE and item.skip_reason is SkipReason.LIMIT_REACHED
    ]
    assert limited


def test_byte_limit_keeps_complete_metadata_inventory(sample_project: Path) -> None:
    report = discover(sample_project, policy=DiscoveryPolicy(max_total_bytes=10))
    assert report.truncated
    assert report.inventory_status is InventoryStatus.COMPLETE
    assert report.inventory_stop_reason is None
    assert report.inventory_checkpoint == ()


def test_exact_inventory_cap_is_complete(tmp_path: Path) -> None:
    root = tmp_path / "tek"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    report = discover(root, policy=DiscoveryPolicy(max_inventory_entries=1))
    assert report.inventory_count == 1
    assert report.inventory_status is InventoryStatus.COMPLETE
    assert report.inventory_checkpoint == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"max_file_bytes": 0}, id="file-bytes-zero"),
        pytest.param({"max_file_bytes": -1}, id="file-bytes-negative"),
        pytest.param({"max_total_files": 0}, id="total-files-zero"),
        pytest.param({"max_total_files": -5}, id="total-files-negative"),
        pytest.param({"max_total_bytes": 0}, id="total-bytes-zero"),
        pytest.param({"max_total_bytes": -1}, id="total-bytes-negative"),
        pytest.param({"max_inventory_entries": 0}, id="inventory-zero"),
        pytest.param({"max_inventory_entries": -1}, id="inventory-negative"),
        pytest.param({"max_dir_entries": 0}, id="dir-entries-zero"),
        pytest.param({"max_dir_entries": -1}, id="dir-entries-negative"),
        pytest.param({"max_depth": -1}, id="depth-negative"),
        pytest.param({"follow_symlinks": True}, id="follow-symlinks-true"),
    ],
)
def test_invalid_discovery_limit_is_rejected(kwargs: dict[str, object]) -> None:
    # Sinirlar fail-closed dogrulanir: icerik/envanter sinirleri sifir veya
    # negatif ise, max_depth negatif ise, follow_symlinks True ise red.
    with pytest.raises(ValueError):
        DiscoveryPolicy(**kwargs)  # type: ignore[arg-type]


def test_zero_depth_is_valid() -> None:
    # max_depth=0 gecerlidir (yalniz kok seviyesi taranir); red edilmemeli.
    DiscoveryPolicy(max_depth=0)


def test_depth_zero_is_valid_and_stops_at_root(tmp_path: Path) -> None:
    # max_depth=0: derinlik kok 0'dir; her cocuk derinlik 1'dir ve 1 > 0
    # oldugu icin hicbir cocuk eligibility'ye giremez. Kok seviyedeki dosya bile
    # derinlik sinirina takilir ama metadata envanterinde gorunur.
    root = tmp_path / "kok"
    root.mkdir()
    (root / "kok.txt").write_text("k", encoding="utf-8")
    sub = root / "alt"
    sub.mkdir()
    (sub / "derin.txt").write_text("d", encoding="utf-8")

    report = discover(root, policy=DiscoveryPolicy(max_depth=0))

    # Hicbir dosya eligibility'ye giremedi (tumu derinlik sinirinda).
    assert report.files == ()
    # Kok seviyedeki dosya envanterde metadata olarak LIMIT_REACHED gorunur.
    kok = next(item for item in report.inventory if item.relative_path == "kok.txt")
    assert kok.kind is FileKind.FILE
    assert kok.skip_reason is SkipReason.LIMIT_REACHED
    # Alt dizine inilmedi: icindeki dosya envanterde gorunmez.
    assert "derin.txt" not in {item.relative_path for item in report.inventory}
    # Derinlik sinirina dokundugundan envanter partial (sahte complete yok).
    assert report.inventory_status is InventoryStatus.PARTIAL
    assert report.inventory_stop_reason is StopReason.DEPTH_LIMIT


def test_file_growth_after_stat_is_not_indexed(tmp_path: Path) -> None:
    root = tmp_path / "degisen"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("a", encoding="utf-8")
    original_open = Path.open
    changed = False

    def grow_before_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal changed
        if path == target and not changed:
            changed = True
            with original_open(target, "ab") as writer:
                writer.write(b"b")
        return original_open(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "open", grow_before_open)
        report = discover(root)

    assert report.files == ()
    assert report.skipped_by(SkipReason.UNREADABLE)
    assert report.inventory_status is InventoryStatus.PARTIAL
    assert report.inventory_stop_reason is StopReason.UNREADABLE_FILE
    assert report.inventory_checkpoint == ("a.txt",)


def test_depth_limit_yields_partial_inventory(sample_project: Path) -> None:
    # max_depth=1: derinlik kok'e gore yol bilesen sayisidir (kok 0,
    # README.md 1, src/ana.py 2). Ikinci seviye girdiler (src/ana.py,
    # tests/test_ana.py) envanterde metadata olarak gorunur ama icerikleri
    # okunmaz; birinci seviye dizinler (src, tests) normal izlenir.
    report = discover(sample_project, policy=DiscoveryPolicy(max_depth=1))
    assert report.inventory_status is InventoryStatus.PARTIAL
    assert report.inventory_stop_reason is StopReason.DEPTH_LIMIT

    # Derin dosya envanterde LIMIT_REACHED metadata olarak gorunur.
    deep = next(item for item in report.inventory if item.relative_path == "src/ana.py")
    assert deep.kind is FileKind.FILE
    assert deep.skip_reason is SkipReason.LIMIT_REACHED
    # Derin dosya eligibility'ye girmis degil.
    assert "src/ana.py" not in {item.relative_path for item in report.files}
    # Birinci seviye dizin (src) normal olarak izlenmistir.
    src_dir = next(item for item in report.inventory if item.relative_path == "src")
    assert src_dir.kind is FileKind.DIRECTORY
    assert src_dir.skip_reason is None
    # Ikinci seviye dosya da derinlik sinirindan disari.
    test_file = next(item for item in report.inventory if item.relative_path == "tests/test_ana.py")
    assert test_file.skip_reason is SkipReason.LIMIT_REACHED
    assert "tests/test_ana.py" not in {item.relative_path for item in report.files}
    # Kok dosyalar (README.md) henuz eligible.
    assert "README.md" in {item.relative_path for item in report.files}


def test_unreadable_directory_is_partial_not_complete(sample_project: Path) -> None:
    # Bir dizini okunamaz yap: okunamayan dizin envanterde UNREADABLE olarak
    # gorunur, envanter partial olur, sahte complete yok.
    with pytest.MonkeyPatch.context() as mp:
        original_iterdir = Path.iterdir

        def _raise_if_kilitli(self: Path) -> object:
            if self == sample_project / "tests":
                raise PermissionError(13, "Erisim reddedildi", str(self))
            return original_iterdir(self)

        mp.setattr(Path, "iterdir", _raise_if_kilitli)
        report = discover(sample_project)

    assert report.inventory_status is InventoryStatus.PARTIAL
    assert report.inventory_stop_reason is StopReason.UNREADABLE_DIRECTORY
    # Devam checkpoint'i okunamayan dizini gorur.
    assert "tests" in report.inventory_checkpoint
    # Okunamayan dizin envanterde UNREADABLE metadata olarak tasinir.
    assert any(
        item.relative_path == "tests"
        and item.kind is FileKind.DIRECTORY
        and item.skip_reason is SkipReason.UNREADABLE
        for item in report.inventory
    )
    # Okunamayan dizinin icindeki dosya envanterde gorunmez.
    assert "tests/test_ana.py" not in {item.relative_path for item in report.inventory}


def test_global_entry_cap_enforced_during_traversal(sample_project: Path) -> None:
    # Global girdi siniri tarama SIRASINDA (sonradan slice ile degil) zorlanir.
    # Kanit: tam bir tarama root + src + tests + node_modules + node_modules/paket
    # = 10 girdi sayar; sinirli tarama ise sinire ulasilir KESILIR ve sayilan
    # girdi sayisi 10'dan az kalir (tam tarama degil, erken kesilme).
    cap = 3
    yielded = 0

    original_iterdir = Path.iterdir

    def _counting_iterdir(self: Path) -> Iterator[Path]:
        nonlocal yielded
        for item in original_iterdir(self):
            yielded += 1
            yield item

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "iterdir", _counting_iterdir)
        report = discover(sample_project, policy=DiscoveryPolicy(max_inventory_entries=cap))

    # Envanter, sinira tam ulasiyor (sinir asimi tarama icinde kesildi).
    assert len(report.inventory) == cap
    assert report.inventory_status is InventoryStatus.PARTIAL
    assert report.inventory_stop_reason is StopReason.INVENTORY_CAP
    # Devam checkpoint'i, kesilme noktasini gorur (bos degil).
    assert report.inventory_checkpoint
    # KANIT: sayilan girdi, tam tarama (10) degil; tarama erken kesildi.
    # Yalnizca root dizini listelendi (6 girdi), alt dizinler islenmedi.
    assert yielded < 10
    assert yielded >= 6  # kok okunur, alt agacin tamami gezilmez


def test_directory_entry_cap_enforced_during_traversal(tmp_path: Path) -> None:
    # Tek bir dizinden islenen girdi siniri, dev dizinler bellege yuklenmeden
    # tarama SIRASINDA kesilir; envanter partial + devam checkpoint'i verir.
    root = tmp_path / "buyuk"
    root.mkdir()
    big = root / "calisma"
    big.mkdir()
    total = 1000
    for index in range(total):
        (big / f"d{index:04d}.txt").write_text("x", encoding="utf-8")

    yielded = 0
    original_iterdir = Path.iterdir

    def _counting_iterdir(self: Path) -> Iterator[Path]:
        nonlocal yielded
        for item in original_iterdir(self):
            yielded += 1
            yield item

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "iterdir", _counting_iterdir)
        report = discover(root, policy=DiscoveryPolicy(max_dir_entries=50))

    # Dizin sinirini asitigi icin tarama partial kesilir (sahte COMPLETE yok).
    assert report.inventory_status is InventoryStatus.PARTIAL
    assert report.inventory_stop_reason is StopReason.DIR_ENTRY_CAP
    # Devam checkpoint'i buyuk dizini gorur.
    assert "calisma" in report.inventory_checkpoint
    # KANIT: buyuk dizinin tumu (1000) bellege yuklenmedi; yalnizca sinirli
    # (50 kabul + 1 siniri asan) girdi sayildi, tarama orada kesildi (tam
    # tarama + slice degil). root (1) + buyuk dizinden 51 = 52.
    assert yielded < total
    assert yielded == 52  # root (1) + sinirli buyuk dizin (50 kabul + 1 asan)
    # Envanter, sinirli dizinden gelen girdilerle sinirli kalir.
    assert report.inventory_count < total
    # Dizin, sinirla okunamadigi icin envanterde LIMIT_REACHED metadata olarak.
    assert any(
        item.relative_path == "calisma"
        and item.kind is FileKind.DIRECTORY
        and item.skip_reason is SkipReason.LIMIT_REACHED
        for item in report.inventory
    )


def test_inventory_is_bounded_and_sorted(sample_project: Path) -> None:
    report = discover(sample_project)
    paths = [item.relative_path for item in report.inventory]
    # Envanter kararli (yol) sirasindadir.
    assert paths == sorted(paths)
    # Envanter girdisi salt metadata tasiyor: icerik/digest/secret yok.
    for item in report.inventory:
        assert isinstance(item, InventoryEntry)
        assert item.relative_path
        assert item.kind in FileKind
        assert item.size_bytes >= 0
    # as_dict() envanter ozetini icerir ama tum girdi icerigini sermez.
    rendered = report.as_dict()
    assert rendered["inventory"]["status"] == "complete"
    assert rendered["inventory"]["entry_count"] == len(paths)
    assert rendered["inventory"]["entries_by_kind"][FileKind.FILE.value] > 0
    assert rendered["inventory"]["entries_by_kind"][FileKind.DIRECTORY.value] > 0


def test_inventory_streaming_digest_matches_whole_read(sample_project: Path) -> None:
    # Streaming digest, dosyanin tamaminin okunmasiyla ayni sonuca ulasmali
    # (buyuk dosya bellekte toplanmali bile tutarli olmalidir).
    body = os.urandom(100) + b"A" * (BINARY_PROBE_BYTES + 2048)
    (sample_project / "buyuk.txt").write_bytes(body)
    report = discover(sample_project)
    found = next(item for item in report.files if item.relative_path == "buyuk.txt")
    expected = DIGEST_PREFIX + hashlib.sha256(body).hexdigest()
    assert found.content_digest == expected


def test_inventory_bytes_sum_matches(sample_project: Path) -> None:
    report = discover(sample_project)
    file_bytes = sum(item.size_bytes for item in report.inventory if item.kind is FileKind.FILE)
    assert report.inventory_bytes == file_bytes


def test_complete_inventory_reports_no_checkpoint(sample_project: Path) -> None:
    report = discover(sample_project)
    assert report.inventory_status is InventoryStatus.COMPLETE
    assert report.inventory_stop_reason is None
    assert report.inventory_checkpoint == ()


# --- Symlink / junction negatifleri (WP-04.02) ---------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Junction testi yalniz Windows'ta calisir")
def test_junction_is_not_followed(sample_project: Path, tmp_path: Path) -> None:
    # Windows'ta `pathlib.is_symlink()` junction icin False dondurur; kesif
    # reparse point'i tespit edip izlememeli, hedefe cogulmamali.
    outside = tmp_path / "disarida"
    outside.mkdir()
    (outside / "sizinti.txt").write_text("gizli-icerik", encoding="utf-8")
    link = sample_project / "baglanti"
    if not _make_junction(link, outside):
        pytest.skip("Junction olusturmak icin yetki yok (mklink /J)")
    try:
        report = discover(sample_project)
    finally:
        import shutil as _shutil

        try:
            _shutil.rmtree(link)
        except OSError:
            link.unlink()

    # Junction envanterde SYMLINK metadata olarak gorunur.
    entry = next(item for item in report.inventory if item.relative_path == "baglanti")
    assert entry.kind is FileKind.SYMLINK
    assert entry.skip_reason is SkipReason.SYMLINK
    assert "baglanti" in {item.relative_path for item in report.skipped_by(SkipReason.SYMLINK)}
    # Hedef icerigi cigerinme: envantere ya da eligibility'ye karismamali.
    assert not any("sizinti" in item.relative_path for item in report.inventory)
    assert not any("sizinti" in item.relative_path for item in report.files)
    # Hedef dizin (disarida) envanterde gorunmemeli (root disinda).
    assert not any(item.relative_path == "baglanti/disarida" for item in report.inventory)


@pytest.mark.skipif(os.name == "nt", reason="Windows'ta dosya symlink olusturmak yonetici ister")
def test_symlink_loop_is_not_followed(sample_project: Path) -> None:
    # Kendine/ust dizine cakan symlink dongu/escape olusturmasin; fail-closed.
    loop = sample_project / "dongu"
    loop.symlink_to(sample_project, target_is_directory=True)
    report = discover(sample_project)
    assert "dongu" in {item.relative_path for item in report.skipped_by(SkipReason.SYMLINK)}
    # Dongu cozulmemis: dongu altinda sonsuz tekrar yok.
    assert not any(item.relative_path.startswith("dongu/") for item in report.inventory)
