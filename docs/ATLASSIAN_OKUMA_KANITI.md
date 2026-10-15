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
