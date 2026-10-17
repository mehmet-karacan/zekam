# Yerel scheduler, worker ve raporlar

Windows yerel çekirdeği bir scheduler daemon'u kurmaz. Kalıcı iş, lease, outbox ve
recovery durumu `state/operational.db` içindedir. Dış zamanlayıcı yalnız aşağıdaki
bounded run-once komutlarını çağırabilir; bunlar PostgreSQL, Docker veya provider
bağlantısı kullanmaz.

```bash
zekam worker status --home <home>
zekam worker run-once --home <home>             # salt-okunur plan
zekam worker run-once --uygula --home <home>    # en fazla bir claim/effect
zekam worker reconcile --home <home>            # salt-okunur recovery plani
zekam worker reconcile --uygula --home <home>

zekam scheduler status --home <home>
zekam scheduler reconcile --home <home>
zekam scheduler reconcile --uygula --home <home>
zekam scheduler rebuild --home <home>            # salt-okunur plan
zekam scheduler rebuild --uygula --home <home>   # immutable segmentlerden rebuild
zekam scheduler report --home <home>             # CURRENT projection reconcile/read
```

`worker run-once` claim-before-effect ve terminal receipt sözleşmesini mevcut
yerel runtime servisi üzerinden korur. `reconcile` yalnız orphan/expired lease ve
belirsiz outbox durumlarını mevcut recovery kurallarıyla uzlaştırır. Analytics
`rebuild`, immutable raw segmentlerden yeni DuckDB generation, kaynak manifesti,
iki rapor ve rebuild receipt üretip `CURRENT` işaretçisini atomik yayımlar.
`report` bu zinciri tekrar doğrular; eksik, bozuk veya stale bir parçayı rapor diye
sunmaz. Bu projeksiyonların hiçbiri yürütme yetkisi vermez.

## Evolution kullanim olcumunun kapsami (16 Ekim 2026, test bekliyor)

`evolve report` icindeki `actual_usage` yalniz kayitli child terminal tuketimini
toplar (`scope=recorded-child-terminals`); acik reservation veya halen calisan
isin final maliyeti oldugunu iddia etmez. Unknown effect sonucu ayri sayilir;
bagimsiz terminal readback'te kayitli tuketim varsa bu sayilar korunur.
Store/schema yoksa, okuma hataliysa veya 10.000 terminal siniri asilirsa
`available=false`, olcumler `null` olur. Bilinen bos tablo sifir tuketimdir.
Model bucket sayi/toplami terminal sayi/toplamiyla ayni snapshot'ta eslesmelidir.
Budget usage da ayni availability ve scope'u tasir; rapor grant vermez.

Evaluation before/after ornegi en fazla son 100 kayittir. Evaluation body strict
JSON ve digest ile okunur; eksik/yanlis tipte/negatif olcum sifira cevrilmez.
`evaluations_coverage` gercek toplam kayit sayisi, dahil edilen kayitlar, bilinmeyen
olcumler ve ornek toplamini ayri gosterir. Ornek tum retained ledger'i kapsamiyorsa
ledger geneli `evaluations` toplamlari `null` kalir. Gecerli ornekte tek bir metrik
eksikse yalniz o metrigin kesin toplami bilinmez. Gecerli bos evaluation tablosunda
gercek toplam sifirdir; store/schema yoklugu bos tablo yerine gecmez.

Regresyon kaynaklari `tests/unit/test_evolution_usage.py` icinde hazirlandi,
kullanici karariyla CALISTIRILMADI. Native scheduler ve genel WP kabulu aciktir.

## Salt okunur composition (16 Ekim 2026, test bekliyor)

Doctor/cognitive kontrolleri, local-core status, scheduler status/report/rebuild
hazirligi, evolution control plan'i ve backup kaynak okumasinda servis composition
`read_only=True` kullanir. Operational runtime mevcut schema/config ister;
eksik config'i constructor eklemez. Mevcut private skill receipt key varsa
create=False ile okunur, yoksa yenisi olusturulmaz veya rotate edilmez. Signed
backup mevcut key binding zorunlulugunu korur; eksik key yerine yeni rastgele
materyal gecmez. Local runtime status/recovery de mevcut runtime'i acar.

Paused/disabled tick worker/effect/signing-key hazirligindan once doner. Bu flag
yalniz composition kurulumundaki otomatik yazmayi kaldirir; sonraki yetkili
effect metotlari icin claim/admission yerine gecmez. Explicit init/prepare ve
yetkili worker'in kurulum yolu ayridir. Core schema sagligi skill key veya full
capability kabulunun kaniti degildir.

Unit ve CLI regresyon kaynaklari `test_local_core_read_only.py` ve
`test_cli_read_only_setup.py` icinde hazirlandi; CALISTIRILMADI.
