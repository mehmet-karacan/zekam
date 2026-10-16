# Kanonik kaynak mimarisi export'u - WP-12.04/05

Bu kaynak dilimi `application/architecture_export.py` ve
`scripts/export_source_architecture.py` ile tek bir kaynak katalog girdisinden
yerel Markdown, HTML, Mermaid ve ZIP uretir. Operational authority veya kabul
raporu degildir. Testler CALISTIRILMADI.

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
| manifest.json | Catalog digest ve diger dosyalarin exact byte hash'leri |
| architecture.zip | Sabit tarih/sira/izinlerle ayni bes dosyanin yerel paketi |

HTML/Mermaid render motoru veya yeni arac kurulumu gerekmez. Mermaid kaynak
olarak teslim edilir. GitBook/Miro login, yayin veya uzak API cagrisi yapilmaz;
bundle yerel export girdisidir, provider'in kabul ettigi import formati iddiasi yoktur.

## Kabul siniri ve sonraki kapilar

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
