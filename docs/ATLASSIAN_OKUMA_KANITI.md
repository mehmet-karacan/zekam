# Yerlesik Atlassian Salt Okuma Kaniti

WP-06.04 kapsaminda yerlesik stdio MCP okuyucusu tek sayfalik, sinirli GET
istekleri yapar. Bu dilim connector kodudur; canli Jira/Confluence cagrisi,
credential kurulumu, knowledge importu veya Work kapanisi yapilmadi.

## Sayfalama ve provenance

`confluence_search` mevcut CQL sorgusuna `start` (0..1000000) ve `limit`
(1..50) ekler. `_zekam.pagination` donen adet, next_start ve gorunur sayfa
zincirinin sonuna ulasilip ulasilmadigini bildirir. Server'dan gelen next URL
takip edilmez: same-origin, exact endpoint ve monoton offset dogrulanir;
istemci ayni CQL ile verilen start degerini tekrar yollar. Bos sayfada next,
ayni/geri offset, boyut drift'i ve yinelenen content kimligi reddedilir.
Her cagri bagimsiz HTTP gozlemidir; degisen sorgu sonucunun snapshot'i veya
tum kullanicilarin gorebildigi icerik toplami oldugu iddia edilmez.

Her content sonucu site digest'i, content ID ve exact version numarasindan
source_ref, canonical REST locator ve response digest'i tasir. Version eksikse
veya `confluence_get_page` istenenden farkli content ID dondururse basarili
provenance uretilmez. Locator current endpoint'tir; exact revision source_ref
ve version alanindadir. `permission_evidence=authenticated-response-only`
yalniz o istegin gorunurlugunu belirtir; ACL snapshot'i, grant veya proje
kapsam yetkisi degildir. `grants_authority` false kalir.

Jira aramasi da acik `start_at` alir. Bu dilim Jira attachment indirme veya
issue/proje knowledge publication receipt'i uretmez; bu baglantilar aciktir.

## Transport sinirlari

Yaniti en fazla 2 MiB+1 bayt okuyup asimi reddeder. HTTPS base URL credential,
query, fragment ve traversal kabul etmez. Redirect same-origin olmak
zorundadir. 401/403/404/429 dahil HTTP kodu sanitize hata olarak korunur;
response body, credential ve upstream hata metni hata mesajina alinmaz.
Otomatik retry veya permission fallback yoktur. Mevcut GET timeout'u 45 sn'dir.
Provider'in izin verdigi kaynak verisi MCP yanitinda kalir; bu yanit kalici
proje knowledge veya publication kaniti olarak otomatik kaydedilmez.

API semantigi icin resmi kaynak: [Confluence Data Center REST API](https://developer.atlassian.com/server/confluence/rest/latest/)
(start/limit pagination ve version expand). Canli server surumu ve permission
paritesi final kabul asamasinda dogrulanmalidir.

Regresyon kodu `tests/unit/test_atlassian_read_evidence.py` icinde hazirlandi.
Kullanici karariyla CALISTIRILMADI; final toplu test asamasini bekler.

## Jira issue ve attachment metadata bagi

Jira search yaniti artik startAt, maxResults, total ve returned row sayisini
dogrular; duplicate issue ID/key ve ilerlemeyen bos sayfa reddedilir. Cikti kendi
site/JQL digest'ini, next_start ve has_more bilgisini tasir. Cursor limiti
asilinca has_more saklanir, continuation_blocked gorunur. Degisebilen offset
search bir transaction snapshot'i gibi sunulmaz: consistent_snapshot=false.

Issue read, istenen exact key ile donen key'i eslestirir. Site, numeric issue ID,
timezone-aware updated, authenticated response digest ve locator provenance'a
baglanir. Updated bir monoton Jira revision sayaci olarak sunulmaz.

Attachment alani explicit istenir; eksik alan bos liste varsayilmaz. En fazla
200 unique numeric ID icin filename, size ve mime metadata'si dogrulanir. Yeni
projection author, thumbnail ve server-provided content URL alanlarini tasimaz.
Locator configured base + API attachment ID'den kurulur. Metadata degisimi kendi
digest'ini degistirir; bu content digest DEGILDIR. content_digest=null,
content_read=false ve attachment_content_imported=false kalir. Projection digest
ile upstream response digest ayri alanlardir. Comments complete iddiasi yoktur.

HTTP JSON duplicate-key/nonfinite/strict UTF-8 kontrolunden gecer; hata cause'u
upstream ham response/credential'i disari tasimaz. Otomatik retry, attachment
download, knowledge publication veya Work receipt uretilmez. Gercek yetkili
download -> content hash -> parser/secret -> scoped canonical knowledge publication
baglantisi ve canli ACL kabulu halen aciktir.

Regresyon kaynaklari mevcut test_atlassian_read_evidence.py dosyasina eklendi,
CALISTIRILMADI. Canli Jira/Confluence cagrisi yapilmadi.

Birincil Jira Server/Data Center kaynaklari:

- https://developer.atlassian.com/server/jira/platform/rest/v11002/api-group-search
- https://developer.atlassian.com/server/jira/platform/rest/v10000/api-group-issue/

## Yetkili ek okuma ve parser bagi

`jira_read_attachment` issue key, attachment ID ve onceki `jira_get_issue`
ciktisindaki expected_metadata_digest ile calisir. Native assistant bu degerleri
arac ciktisindan cozer; kullanicidan JSON/digest yazmasi beklenmez. Bu salt-okunur
tool knowledge/import veya execution yetkisi vermez.

Ek ayni issue'da indirme oncesi ve sonrasi aranir; metadata digest, issue numeric
ID veya updated drift'i reddedilir. Server-provided content URL yalniz configured
HTTPS origin ve exact attachment path'indeyse kullanilir. Redirect diger origin,
ayni origin'deki baska API, traversal veya query/fragment hedefine gidemez.
HTTP status kodu korunur; credential/URL/body hata ciktisina eklenmez. Her HTTP
istegi 45 saniye socket timeout tasir; bu tum parser islemine wall-clock deadline
kaniti degildir.

Ek boyutu 8 MiB, normalize cikti 256 unit ve 384 KiB ile sinirlidir. Metadata ve
HTTP byte/content-length/content-type/encoding bagi kontrol edilir. TXT/Markdown,
DOCX ve dijital PDF mevcut parser'larla locator'li ContentUnit'e doner. PdfParser
bu yolda allow_ocr=false kullanir; taranmis PDF'de render/OCR sureci baslatmaz.
Desteklenmeyen MIME/format TXT varsayilmaz. OCR default davranisi diger mevcut
yetkili ingestion yollarinda korunur; no-OCR profili ayri profile digest tasir.

Normalize metin ve tum sonuc secret/PII kontrolunden gecmeden tool'a donmez. Ham
binary, upstream author/download URL ve ham transcript artifact'i saklanmaz.
Gercek downloaded byte hash'i content_digest'e; metadata/projection/normalized
hash'ler kendi alanlarina baglanir. Kaynak untrusted-reference kalir,
knowledge_imported=false ve grants_authority=false tasir. Ekin icerigindeki
talimatlar host/Work yetkisi olamaz.

Bu kaynak gelistirmesinde canli download yapilmadi. Transport/metadata race,
cross-origin/path, secret/PII, MIME/size ve PDF no-OCR regresyon kodlari eklendi;
CALISTIRILMADI. Canonical project knowledge publication, gercek endpoint/client
akisi ve ACL kabulu halen aciktir.

## Canonical proje knowledge publication

`knowledge jira-attachment-plan --project ... --issue-key ... --attachment-id ...
--metadata-digest ...` yetkili read yolunu kullanip salt-okunur bir mutation plani
uretir. Issue -> proje mapping'i canonical registered project ID ile eslesmeden
remote read baslamaz. Mapping, metadata, byte/normalized/parser ve tum read result
digest'leri plana baglanir. Generation timestamp planda sabittir; apply yeni bir
timestamp uydurup payload'i degistirmez.

Native assistant plan JSON'unu kaynak repo disinda tutar. Kullaniciya teknik
digest/JSON yazdirmaz. `knowledge jira-attachment-apply <plan-file>` yalniz preview
yapar; `--uygula --plan-digest <exact>` mevcut canonical knowledge.create local
job/lease/effect claim/terminal receipt yoluna gider. Bu control-plane CLI mevcut
scope/claim kapisini kullanir; Markdown veya tool output authority uretmez.

Apply oncesi ek tekrar okunur; proje mapping, source ve plan drift'i effect
claim'den once reddedilir. Cikti proje kapsaminda generated/reference Markdown'dir;
user-authored belge gibi kaydedilmez. Kaynak metni quote bloklarinda, locator ve
provenance ile untrusted reference olarak tasinir. Secret/PII kapisi tekrar uygulanir.
Canonical note kaydi ve dosya materialization ayni mevcut claim/receipt mekanizmasina
baglidir. Knowledge search dosyayi digest readback ile gorebilir; vector/embedding
cagrisi veya index aktivasyonu bu receipt'e eklenmez.

Ayni exact completed plan replay'i network'e gitmeden receipt/resolution ve note
readback yapar. Bozuk/kayip dosya basari gibi sunulmaz. Enqueue sonrasi ilk lease
alinmadan kesilen ready/attempt=0 knowledge job'i ayni key ile devam edebilir;
running veya belirsiz effect sessizce yeniden dispatch edilmez. Unknown receipt
recovery'de orijinal receipt degistirilmez; canonical completed resolution ayri
readback alanlarinda gorunur.

Yerel SQLite/file/receipt/search/replay, source drift, cross-project mapping ve
enqueue/unknown-receipt recovery regresyon kaynaklari eklendi, CALISTIRILMADI.
Canli kurumsal read/import yapilmadi. Yeni plan farkli bir generated reference
revision'i uretebilir; mevcut remote-source head/supersession ve incremental
index baginin tamamlanmasi ile gercek client/ACL kabul kaniti halen aciktir.

## Kaynak basi, tekrar ve supersession

Yeni import origin kimligi site + numeric issue ID + attachment ID'den turetilir.
Operational note ref prefix'i bu kimlige baglidir. Head, Markdown'dan degil scope'u
tam eslesen active/generated/reference canonical note satirindan cozulur; iki head
varsa reconciliation gerekir. Query scope ve ref araligi LIMIT'ten once uygulanir.

Ayni observed source revision yeni generation timestamp uretmez: mevcut manifested
notun timestamp'i digest readback ile tekrar kullanilir. Ayni plan/payload/idempotency
key'e donulur. Degisen source digest yeni immutable ref'e gider; exact onceki note
ID/content/state plan'a pin edilir. Lease/effect claim sonrasi head CAS tekrar
dogrulanir; stale plan yeni head'i ezemez.

Generated reference update mevcut atomik relation/archive DB yolunu kullanir.
Eski dosya archive'a alinir ve verified supersedes relation'i eklenir. Normal
knowledge search yalniz active kaydi getirir. Onceki planin completed replay'i
archive dosyasini readback yapabilir; is_current_reference=false tasir. Historical
receipt yeni source versiyonunu aktif yapmaz.

Yeni bir revision'in predecessor'i yalniz materialized note varligi ile onaylanmaz.
Canonical completed producer job, exact claim effect ve completed receipt veya
completed recovery resolution dogrulanir. Unknown/unresolved producer head'i
once recovery ister. Producer lookup tek scope/ref query'sidir; buyuk local job
gecmisinde JSON predicate performansi final kabulde olculecektir.

Bu gelistirmede schema veya kullanici DB migration'i yapilmadi. Eski import
planlari/source origin bagi olmayan legacy notlar otomatik authority/head'e
yukseltilmez; canonical mutation status tarihsel kaniti okumaya devam eder.
Legacy source adoption/reconciliation ayri exact inceleme gerektirir. Yeni
head/dedup/update/stale/unknown-producer regresyon kaynaklari hazir,
CALISTIRILMADI. Incremental vector/index publication ve canli kabul aciktir.
