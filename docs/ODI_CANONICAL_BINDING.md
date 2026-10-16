# ODI export binding canonical Work/Run bagi

`project odi-preflight` ve `odi-bind` preview salt okunur plan v2 uretir.
`source_snapshot_contract` export manifest/tree ve exact physical root digest'ini
snapshot revision/tree/content alanlarina baglar. Native ajan mevcut proje/Work
kimligini kendisi cozer; snapshot ve Run'i mevcut yetkili canonical araclarla
bu contract/plan digest'ine baglar. Kullaniciya UUID veya digest yazma isi yuklenmez.
Preview Work, grant, schema migration veya source snapshot olusturmaz.

`odi-bind --uygula` artik exact typed `LocalJobBinding`, admitted Run plan digest'i,
Work revision, project ve source snapshot ister. CLI `--run-id`/`--work-revision`
alanlarini actual operational store'dan cozer; kaynak koduna gecilen binding
salt Markdown/JSON beyanindan uretilmez. Eski unbound job'lar yeni Work'e adopt
edilmez. Operational v7+ kurulumu kendi mevcut migration kapisindan gecmelidir.

Yazma ortak `project.odi-bind` canonical runtime'indadir: exclusive project lease,
claim-before-effect, kisa DB transaction'lari, source/target once-sonra readback,
CAS result, checkpoint ve terminal receipt kullanir. Raw XML ve absolute source
path CAS sonucuna konmaz; sadece private binding belgesinde kalir. Yeni metadata
2 MiB private file limitini asarsa partial binding yazilmaz.

Mevcut private binding degistirilecekse exact eski bytes ayni private baglantilar
alaninda content-addressed `odi11g-history-<digest>.json` dosyasinda korunur.
Legacy mapping korunmasi ona canonical execution authority kazandirmaz.
Process-local own-write readback ile destekli canonical writer'lar serialize olur;
arbitrary OS writer isolation iddiasi yoktur.

`project odi-binding-source` actual completed Work/Run/binding, claim/checkpoint/
CAS/receipt ve private dosya hash'ini yeniden okur. Guncel mapping ile korunmus
historical mapping ayri alanlarda raporlanir. Historical rapor export XML'ini
yeniden parse etmez ve guncel export freshness/embedding readiness iddia etmez.
Eksik/degismis private dosya veya history kaniti kabul edilmez.

Receipt yazimindan once kesilen is explicit recovery gerektirir; ayni komutu
tekrarlamak yeni dosya yazimi veya recovery-completed durumu uretmez. Completed
replay de source parsing veya binding yazimi yapmaz. Kaynakta eski silent recovery
yolu kaldirildi; kullanici verisi ya da DB migration'i bu gelistirmede calistirilmadi.

Smart Export import/sanitized index canonical kaynak bagi ayri dilimde eklendi;
ayrinti `ODI_SMART_CANONICAL_IMPORT.md`. Directory binding metadata yolunun tek
basina ODI indeksini veya WP'yi tamamladigi soylenmez.
SQLite/CAS/XML/private file ve CLI regresyon kaynaklari hazir, CALISTIRILMADI.
Tum testler tum gelistirmeler sonrasindaki final asamadadir.
