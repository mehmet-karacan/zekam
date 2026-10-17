# Kanonik kaynak mimarisi export'u - WP-12.04/05

Bu kaynak dilimi `application/architecture_export.py` ve
`scripts/export_source_architecture.py` ile tek bir kaynak katalog girdisinden
yerel Markdown, HTML, Mermaid ve ZIP uretir. Miro/GitBook icin remote import
veya yayin yapmayan yerel planlar da ayni katalogdan uretilir. Operational
authority veya kabul raporu degildir. Testler CALISTIRILMADI.

## Giris ve kaynak bagi

- Gercek checkout kokunde Git'in cached/tracked envanteri kullanilir. Untracked
  kullanici dosyalari, config/secret dosyalari ve legacy-preserved agaci okunmaz.
- AKTIF_GOREV.md exact bytes/digest + mevcut ActiveTaskContract ile okunur.
  REQ/CAP/WP ve D karar kapilari bu dosyanin guncel bolumunden gelir. Ek A'daki
  tarihsel talep defteri tekrar authority veya ikinci CAP matrisi yapilmaz.
- Python source/test, public schemas, mimari/ADR belgeleri ve pyproject icin
  relative source path, exact byte digest, boyut ve stable source ID tutulur.
  Source/secret content, kullanici yolu ve transcript export edilmez.
- CLI/API decorator, literal SQLite CREATE TABLE ve test fonksiyon adlari AST ile
  okunur. Source modulu import edilmez; test, migration, uygulama veya provider
  calistirilmaz. Literal declaration, runtime table/command varligi kaniti degildir.
- Pyproject'in exact wheel exclusion kayitlari archive-only olarak ayrilir.
  Diger source dosyalari wheel'de dogrulanmis veya hazir sayilmaz.
- Working-tree byte snapshot basis aciktir; HEAD tek basina dirty kaynaklari
  tanimlamaz. Ikinci readback envanter/content/HEAD degisimini reddeder. Bu,
  dosya sistemi transaction'i veya operational consistent backup iddiasi degildir.

## Yerel cikti

`python scripts/export_source_architecture.py --output <repo-disinda-yeni-mutlak-klasor>`

Yalniz yeni ve repo disindaki hedef kabul edilir; mevcut hedefe overwrite yapmaz.
Repo icinde gecici/generate rapor birakmaz. Dosyalar:

| Cikti | Kapsam |
|---|---|
| catalog.json | Tek canonical task/declaration/source-hash katalog girdisi |
| architecture.md | Ayni katalogdan REQ/CAP/WP/karar ve source envanteri |
| architecture.html | Escaped, script ve remote resource icermeyen yerel belge |
| architecture.mmd | Canonical WP -> CAP iliski diyagram kaynagi |
| traceability.json | Explicit task scope -> WP -> test source bagi ve eksikler |
| miro-plan.json | Yerel Mermaid inceleme/import plani; remote cagrisi yok |
| gitbook-plan.json | Yerel Markdown/traceability inceleme/yayin plani; remote cagrisi yok |
| manifest.json | Catalog digest ve diger dosyalarin exact byte hash'leri |
| architecture.zip | Sabit tarih/sira/izinlerle ayni sekiz dosyanin yerel paketi |

HTML/Mermaid render motoru veya yeni arac kurulumu gerekmez. Mermaid kaynak
olarak teslim edilir. GitBook/Miro login, yayin veya uzak API cagrisi yapilmaz;
bundle yerel export girdisidir, provider'in kabul ettigi import formati iddiasi yoktur.

## Dosyadan yeniden okuma

`python scripts/export_source_architecture.py --verify <mevcut-repo-disi-mutlak-klasor>`

Bu salt okunur yol guncel tracked kaynak katalogunu yeniden kurar ve beklenen
dokuz dosyanin exact bytes'ini bounded olarak diskten okur. Paket manifest'i path,
dosya listesi veya allocation yetkisi vermez. Fazla/eksik dosya, dizin, link/reparse,
source/HEAD drift veya degisen herhangi bir belge basari sayilmaz. Degistirilen
dosya icin manifest hash'ini tekrar yazmak paketi gecerli yapmaz. ZIP unzip
edilmez; ayni canonical girdiden uretilen exact archive bytes'i ile karsilastirilir.

Writer da her dosya yazildiktan sonra ayni captured girdiye karsi disk readback'i
yapar; readback basarisizsa basari raporu donmez. Readback sonrasi degisen kaynak
veya commit icin eski paket guncel kabul edilmez; yeni hedefte yeniden export
gerekir. `file_readback_verified` yalniz bu dosya kapsamidir; runtime_verified,
acceptance_verified, published ve grants_authority false kalir. Bu kontrol mevcut
paketleri onarmaz, yeni model cagrisi/test/migration veya uzak yayin yapmaz.

Readback kodu ve tampered manifest/ZIP/belge, eksik/fazla entry, stale source ve
writer readback failure regresyon kaynaklari eklendi. Kullanici talimatiyla test
ve gercek generator/verify kosusu CALISTIRILMADI; final asamada yapilacak.

## Kabul siniri ve sonraki kapilar

### Kaynak izlenebilirligi

`architecture_traceability.py` guncel WP bolumlerindeki explicit Girdi/kapsam
REQ/CAP kimliklerini ve test path referanslarini ayri kaynak grafina baglar.
REQ-001/004 ve CAP-01/21 gibi slash kisaltmalari acilir; serbest metinden,
benzer dosya adindan veya eski passed iddiasindan semantik esleme yapilmaz.
Genel "tum gereksinimler" ifadesi bir tekil REQ eslemesi olarak tahmin edilmez.

Her edge exact task satirini; test link'i declared path/satirini ve tracked
kaynak varsa source ID/hash + AST test ad/satirlarini tasir. Dosya eksikse
missing kalir; untracked dosya veya baska test ile doldurulmaz. Explicit scope'u
olmayan REQ, test referansi olmayan WP ve eksik test dosyalari ayri listelenir.
WP-parent source bagi, her alt gorevin kabul testini tek basina kanitlamaz.

Bu grafik canonical input, ayri digest-bound traceability.json, manifest ve ZIP
icindedir. Disk readback bu yeni dosyayi da mevcut kaynakla karsilastirir.
Run/evidence/execution_commit null; execution_evidence_attached,
release_chain_complete ve acceptance_verified false kalir. Gercek final test
sonuclari ve canonical receipt/independent review baglantisi sonraki kabul
kapisidir; bu source declaration producer'i bunlari uydurmaz.

Markdown checkbox'i `document_checked`, kaynak declaration olarak kaydedilir;
Work state veya acceptance'a donusmez. Tum acceptance_verified/grants_authority
alanlari false; test run/evidence alanlari null'dir. Eski declared ready/ADR
metni guncel kabul sayilmaz. Bu paket source inventory kapsamindadir; tum migration
guncel runtime kurulumunu, native host/model/OS matrisini, latency/quality olcumunu
ve REQ->CAP->WP->test->run->evidence->commit kapanisini tamamlamaz.

Final test asamasinda deterministic bytes/ZIP/hash, declaration identity, escaped
HTML, duplicate/historical task, unknown CAP/WP ve concurrent source drift
regresyonlari calistirilacak. Gercek paket/source export ve bundle link/hash kabul
kontrolu final kapida yapilacak. Bu asamada generator veya test kosusu yapilmadi.

## 17 Ekim 2026 kaynak readback kaydi

Generator yeni repository-disi bir hedefte calistirildi ve ayni hedefe hemen
`--verify` readback uygulandi. Iki cikti da `catalog_digest` olarak
`sha256:a65d909e2e3054c67595b5425f74e5ba40e937d1dda609a7a702b9a641b33a05` ve
`file_readback_verified=true` verdi. Dokuz manifest dosyasinin byte digest'i
eslesti; `published`, `acceptance_verified` ve `grants_authority` false kaldi.
Bu kayit generator/readback calismasinin gercek kanitidir; final test, native kabul,
bagimsiz verifier veya uzak yayin kaniti degildir.
