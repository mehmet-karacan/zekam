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
