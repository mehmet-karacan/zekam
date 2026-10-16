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

Yeni capture, operational DB'yi once ve diger SQLite store'larini sabit sirada
`BEGIN IMMEDIATE` ile fence'ler. Busy writer'da bekleyip is sahibini dusurmez;
fail-closed durur. Operational lease, aktif run/job, unresolved effect/delivery,
recovery ve pending continuity close mevcut quiescence kuraliyla denetlenir.
Provider ledger'inda receipt'siz veya unknown effect varsa capture yapilmaz.
Fence icinde kaynak transaction degistirilmez; connection kapanisinda rollback
olur. Writer fence penceresi 30 saniyeyle sinirlidir; backup page progress ve
hash donguleri deadline'i denetler. Canli provider/Maven isi bu pencereye kabul
edilmez, aktif sahip expiring/recovery yazimiyla degistirilmez.

Tum DB snapshot'lari bu ortak fence penceresinde alinir. Regular dosya
digest'leri ayni sinirda kaydedilir; DB kilitleri birakildiktan sonra dosya
kopyasi bu digest'le eslesmek zorundadir. Buyuk regular dosya kopyalari sirasinda
DB kilidi tutulmaz. Manifest'in opsiyonel versioned `capture_contract` alani
exact database_paths ve regular_file_digests setlerini tasir; verify bunlari
entries ile eslestirir. Restore receipt'i `quiescent_cohort_contract` bilgisini
ayri verir. Eski v1/v2 dosyalarda bu alan yoksa cohort kaniti varsayilmaz.

Bu, Zekam-owned store ve claimed writer siniridir; harici uygulama dosyasi,
canli provider sonucu veya cihazlar arasi ortak transaction kaniti degildir.
Source census drift kontrolleri devam eder. Gercek restore/continuity kabul
ve performans kaniti final asamada halen gereklidir.

Regresyon kodu `tests/e2e/test_cli_backup.py` icinde acik WAL, committed satir,
verify/restore, opaque CAS bytes, trust dosyasi ve v1 ayrimi icin hazirlandi.
Testler CALISTIRILMADI; final toplu asamayi bekler. Bu gelistirmede kullanici
home'u, gercek DB veya backup hedefi acilmadi; gercek restore kabul kaniti yoktur.
## Receipt dogrulama anahtarinin devamligi

Backup signed skill execute/verify claim'i veya usage kaydi gorurse,
`capture_contract.verification_key_bindings.skill-runtime` icinde yalniz
domain-separated public binding digest'i saklar. Private key bytes, HMAC seal
ve credential bundle'a veya manifest'e yazilmaz. Skill execution/verification
icin kullanilan `runtime/local-effects` journal agaci data kapsamindadir.

Bu bag mevcutsa `backup restore ... --skill-key-file <private-key-file>` gerekir.
Anahtar eski home'un `state/skill-receipt.key` dosyasindan veya ayni private
malzemenin ayri guvenli saklama yerinden acikca verilir. Restore bounded,
private/link-free kaynagi salt okunur okur; kaynak yoksa anahtar olusturmaz.
Binding eslesmezse yeni hedef yayinlanmaz. Eslesen malzeme yalniz yeni hedef
home'un private state credential dosyasina exclusive yazilir ve tekrar okunur.
Mevcut hedef key overwrite edilmez; raw key metadata/receipt'e cikmaz.

Restored composition bu anahtari kullanir; signed kayitlar varken yeni rastgele
key ile sessiz devam etmez. Receipt `verification_keys_restored` alaninda yalniz
mantiksal anahtar turunu raporlar. Signed kaydi olan eski bundle binding metadata
tasimiyorsa ayri dogrulanmis recovery gerekir; legacy byte hash'i key kaniti
sayilmaz. Key-dependent kaydi olmayan home eski yeni-key davranisini korur.

Key transfer ve restore sonrasi HMAC readback fixture'lari eklenmistir;
gercek kullanici receipt kabulunun yerine gecmez. Testler CALISTIRILMADI.

Ek regresyon kaynaklari; butun DB writer fence'lerinin birlikte tutulmasi ve
sonra birakilmasi, aktif sahibin degismemesi, sinir sonrasi regular file drift
ve snapshot deadline sonrasi partial hedefin kalmamasi senaryolarini kapsar.
