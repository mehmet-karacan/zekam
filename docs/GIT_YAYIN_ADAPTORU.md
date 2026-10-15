# Typed Git yayin adaptoru

## Kapsam

`application.git_publication.GitPublicationService` exact Work/Run/source binding,
depo kimligi, remote kimligi, tam branch ref ve tam commit OID icin plan uretir.
Planin subject digest'i test ve bagimsiz verification kanitina baglanir. Plan
yetki degildir; tam plan/effect/realm/actor/Work/resource eslesmesi olan, guncel
ve tek kullanimli `domain.security.Authorization` kaydi trusted ledger'dan okunur.
Test ve verifier boolean CLI bayraklari bu serviste kanit yerine gecmez.

`PublicationLedger` mevcut canonical authority/evidence backend'ine ait typed
get/consume/evidence arayuzudur. Model ciktisi veya Markdown'dan bu backend'i
ureten yol yoktur. Kanit kaydinin kendi digest'i, subject'i ve gercek execution
kimligi dogrulanir; verification execution ve executor kimligi test/yayin
executor'undan ayri olmalidir. Ayri bir baslik altinda ayni executor onay sayilmaz.

## Yurutme

1. Exact tamamlanmis CAS replay varsa mevcut canonical job receipt/checkpoint/
   artifact zinciri okunur; yeni yetki tuketilmez veya remote tekrar cagrilmaz.
2. Ledger yetkisi ve kanitlar yeni job acilmadan once dogrulanir.
3. Mevcut canonical job runtime `git.push` icin lease ve effect claim alir;
   generic worker bu reserved islemi destekliyormus gibi yurutmez.
4. Source/remote/head yeniden okunur ve immutable source snapshot revision'i
   dogrulanir. Yetki ve kanitlar claim sonrasinda yeniden okunur; one-shot yetki
   tuketildikten sonra transport baslar. Ag boyunca DB write lock tutulmaz.
5. Exact remote ref readback'i expected OID ile eslesirse CAS result, canonical
   checkpoint ve terminal receipt zinciri yazilir. Timeout, cancellation veya
   readback uyusmazligi `unknown`/recovery sinirinda kalir; sessiz retry yoktur.

## Git transport

`infrastructure.git.publication.BoundGitPushTransport` gercek kaynak kokunde,
shell kullanmadan bounded process tree ile calisir. Temiz kaynak/HEAD ve exact
root kimligi aranir; tek push URL okunur. Caller'in `GIT_*` override'lari tasinmaz;
hook ve ext protocol kapatilir. Force/mirror/tag/submodule yayini yoktur; tek
OID:refs/heads/... refspec ve remote ref readback'i vardir. Ham URL, stdout,
credential veya source path canonical sonuca yazilmaz.

URL rewrite, coklu push URL veya URL'de credential/query/fragment destegi bu typed
transportta yoktur; acik hata verir. Host-owned credential helper/SSH agent ve
native Git/OS konfigurasyonu host guven sinirinda kalir. Bu sinir keyfi native
shell, credential helper veya configuration race icin OS izolasyonu saglandigi
anlamina gelmez. Dolayli wrapper yayini engellenmis sayilmaz.

## Acik baglanti ve kabul

Bu dilim typed service/gercek Git transport/canonical job baglantisidir.
Production canonical authorization/evidence ledger adapter'inin CLI composition'a
baglanmasi ve native host transport enforcement kabulu halen aciktir. Mevcut
`git push-check` advisory domain kapisidir; bu typed servisin yerine kullanilmaz.
Kaynakta baska yerde `evaluate_push` sonucunun true olmasi yayin yetkisi degildir.

SQLite/CAS claim/receipt/replay, scope/revocation, bagimsiz verifier, unknown
recovery ve gercek yerel bare-Git transport regresyon kodu hazirlandi. Kullanici
talimatiyla HICBIR test calistirilmadi; tum gelistirmeler sonrasi final toplu
asama beklenir. Kullanici DB migration'i, global ayar, canli provider veya bu
adaptorden gercek remote yayini yapilmadi. WP-02/Work kapanisi uretilmedi.
