# Obsidian Projection

Obsidian vault, PostgreSQL kanonik kayitlarinin deterministik ve authority-free
gorunumudur. Kullanici vault dosyasini degistirse bile degisiklik Zekam'a geri
alinmaz.

## Profiller ve fiziksel ayrim

- `private-local`: yalniz local store'da `public` ve `internal` typed kayitlari
  degerlendirir. Secret/PII/raw/diagnostic ve privacy scanner finding'leri her zaman
  elenir. Bu profil public projection uygunlugu veya Git export yetkisi vermez.
- `public-safe`: yalniz `public` classification kabul eder. Ayni fail-closed privacy
  ve link kontrollerini uygular.

Her project UUID ve profil ayri generation ve ayri `CURRENT.json` pointer'i
kullanir. Bir proje veya profil digerinin generation'ina gecemez. `project_id`;
projection digest, manifest, projection receipt ve apply-plan resource'una exact
olarak baglidir. Bos snapshot'lar dahil ayni realm/profile altindaki iki proje ayni
generation kimligini veya CURRENT pointer'ini paylasamaz.

## Deterministik uretim

Generator work item, decision, memory/relation, skill, failure ve compiler candidate
gorunumlerini tek repeatable-read/read-only PostgreSQL snapshot'indan alir. Kayitlari
canonical identity ile siralar; profile uygun olmayanlari digest+reason olarak
manifest exclusion listesine yazar. Daylog ve index dosyalari bu filtered setten
deterministik turetilir.

Legacy `memory.record` tablosunda explicit classification olmadigi icin raw `content`
Markdown'a alinmaz; not yalniz lifecycle metadata ve kanonik digest tasir. Explicit
classification'a sahip compiler candidate kayitlari kendi classification'i ile
filtrelenir. Bu karar yeni migration gerektirmeden PII/raw sizintisini fail-closed
engeller.
Legacy failure gorunumunde de `occurrence_key` ve `run_ref` yerine yalniz category
ve evidence digest kullanilir.

WikiLink hedefleri yalniz generator'un ayni manifestte olusturdugu portable relative
note path'leridir. Absolute path, `..`, backslash, drive prefix, path collision,
broken link, secret pattern, e-posta, connection string veya raw-content marker
generation'i fail-closed durdurur.

```text
ZEKAM_HOME/global/bellek/obsidian/
  <realm>/<project-uuid>/<profile>/
    generations/<projection-digest>/
      00_HOME/...
      01_ACTIVE/...
      02_DECISIONS/...
      03_KNOWLEDGE/...
      04_SKILLS/...
      05_FAILURES/...
      06_DAYLOGS/...
      07_RELATIONS/...
      90_ARCHIVE/...
      _META/manifest.json
      _META/projection-receipt.json
    CURRENT.json
```

## Immutable publish ve CURRENT

Stage yeni ve benzersiz bir temporary directory'de `xb` ile yazilir. Her dosya,
manifest ve projection receipt digest'i yeniden dogrulanir. Generation dizini
`projection_digest` ile immutable publish edilir. Son adimda ayni profile root'unda
temporary pointer `os.replace` semantigiyle atomik olarak `CURRENT.json` olur.
Tamamlanmamis generation mevcut CURRENT'i degistiremez.

`CURRENT.json` v2 exact realm, `project_id`, profile ve sanitize store identity digest'i
tasir. Verifier requested realm/project/profile ile pointer, manifest, receipt ve live
yeniden uretilmis projection/manifest/receipt digest zincirini birlikte karsilastirir;
symlink/reparse point, unmanifested veya eksik file, content digest, coordinated
manifest/receipt forge ve stale live digest farklarini reddeder. Stage publish oncesi
ve generation destination'a tasindiktan sonra yeniden dogrulanir; CURRENT ancak bu iki
exact kontrol sonrasinda degisir.
`obsidian-status` read-only'dir ve missing/stale/cross-project CURRENT durumunda
basarili sonuc uretmez.

## CLI authority siniri

- `zekam memory obsidian-plan`: snapshot'i okur ve authority-free exact plan verir.
- `zekam memory obsidian-apply`: live snapshot'tan plani yeniden kurar;
  `--plan-digest`, `--uygula` ve exact one-shot file-write authorization olmadan
  publish etmez. Plan ayrica sanitize store identity digest'ine baglidir; ayni plan
  baska bir project UUID'ye veya ZEKAM_HOME'a yonlendirilemez.
- `zekam memory obsidian-status`: CURRENT'i live source/policy digest'i ile dogrular.

Publish sonucu fiziksel store identity digest'ini ve bu digest + realm + project +
profile + generation ile bagli `current_ref` degerini tasir. Ayni logical projection'in
iki farkli fiziksel store'daki pointer'lari ayni ref sayilmaz.

Plan, projection receipt veya Markdown dosyasi kendi basina authority degildir.
Runtime orchestration ayrica project authorization, claim-before-effect, checkpoint
ve terminal receipt kapilarini korur.
