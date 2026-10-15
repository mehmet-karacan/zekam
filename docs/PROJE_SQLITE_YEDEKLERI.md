# Proje SQLite Snapshot Yedekleri

WP-11.03 local bundle v2, core SQLite store'larina ek olarak su Zekam-owned
konumlari online SQLite backup API'siyle self-contained snapshot olarak alir:

- `projeler/<proje>/runtime/provider-ledger.sqlite3`
- `knowledge-index/vector/opencode-bge-m3/<proje>/knowledge.sqlite3`
- `knowledge-index/vector/opencode-bge-m3/<proje>/vector-cache.sqlite3`
- `knowledge-index/graph/<proje>/code-graph.sqlite3`

Bu dosyalar dosya kopyasi yoluna girmez. WAL'deki committed veri SQLite backup
API'siyle gorulur; hedef DELETE journal modunda kapanir. Kaynak sidecar dosyalari
bundle'a kopyalanmaz. Snapshot integrity ve foreign-key kontrolunden gecer;
knowledge index'in vec tablolarinda mevcut trusted sqlite-vec loader kullanilir.
Yeni extension kurulmaz. CAS nesnesi veya arbitrary attachment, SQLite header'i
tasisin tasimasin, bu konumlar disinda snapshot'a donusturulmez; exact bytes korunur.

Proje dosyalari v2 manifest'te `kind=sqlite` tasir. Verify/restore ayni exact path
sozlesmesiyle dogrular. Restore hedefi yeni alan olmak zorundadir; manifest,
dosya sayimi/hash/mode ve core readiness kontrolleri korunur. Windows backslash
traversal, drive, NUL ve alternate stream `:` yollarina izin verilmez.

Yeni bundle schema `zekam-local-backup-bundle/v2`'dir. V1 manifestler mevcut
file-kind sozlesmesiyle okunur ve restore edilir. Receipt'te bundle_schema ve
project_sqlite_snapshot_contract ayrilir: v1 sonucu v2 online snapshot kaniti
sayilmaz. V1'de daha once eksik alinmis WAL bilgisi sonradan uydurulamaz.

Opsiyonel public trust dosyasi `config/git-publication-trust.json` exact byte
ve mode ile dahil edilir; operational config'teki trust pin korunur. Private
signer key veya credential dizini backup kapsamına eklenmedi. Dosyayi tasimak
grant, producer enrollment veya publication izni uretmez.

Bu akis her DB icin tutarli snapshot verir. Birden fazla store arasinda ayni
anlik transaction veya canli dis provider uzlastirmasi kaniti degildir; bu
kabul kapilari ayridir. Source census drift kontrolleri devam eder.

Regresyon kodu `tests/e2e/test_cli_backup.py` icinde acik WAL, committed satir,
verify/restore, opaque CAS bytes, trust dosyasi ve v1 ayrimi icin hazirlandi.
Testler CALISTIRILMADI; final toplu asamayi bekler. Bu gelistirmede kullanici
home'u, gercek DB veya backup hedefi acilmadi; gercek restore kabul kaniti yoktur.
