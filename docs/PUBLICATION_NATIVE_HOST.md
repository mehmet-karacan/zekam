# Canonical admission'dan native yayin bileşimi

`application.git_publication_host` gercek tamamlanmis evidence admission receipt'ini
okur; tam plan, project registry ve private source rootunu canonical kayitlardan
cozer. `build_admitted_publication` aktif trust/config ledger'i ve exact Git
transportu mevcut `GitPublicationService` ile birlestirir. Key provisioning,
model calistirma veya kullanici global ayari bu factory'nin etkisi degildir.

`publication apply-admission` dosyadan plan kopyalamak yerine actual admission
referansini kullanir. Preview salt okunurdur; approval kimligi gerektirmez.
`--uygula` exact `plan_digest` ve var olan trusted approval kimligi ister.
Admission, preview veya model ciktisi approval uretmez; mevcut authorization
scope/realm/actor/Work/evidence kontrolleri ve tekil consumption korunur.

Native service'in evidence validator'u her yeni push icin admission/producer/
review/ledger baglarini ve native current test source/ham raporlari yeniden okur.
Validation canonical dispatch oncesi, claim altinda, authorization consumption
sonrasi push oncesi ve push sonrasi terminal receipt oncesinde calisir. Ignored
test resource veya rapor drifti Git'in temiz HEAD gozleminden saklanamaz.
Effect basladiktan sonraki hata recovery gerektirir; push olmus olabilir ve
ikinci push sessizce baslatilmaz. Completed historical replay guncel Git/test
olcumu veya tekrar transport cagrisi yapmaz; mevcut canonical replay sonucu okunur.

`evidence-bundle --uygula` ve `apply-admission --uygula` explicit LOCAL_EFFECT
CLI admission yuzeyleridir. Preview mutating sayilmaz. Exemption yalniz eski
hydration prerequisite deadlock'unu onler; backend/Work/claim/approval yetkisi
vermez. Generic external trusted evidence icin mevcut typed publication yolu
korunur; native test-only bu ek current source validator'unu kullanir.

## Acik kabul kapilari

Bu composition source yolu, native shell'in OS credential veya signer key
erisiminin engellendigini kanitlamaz. Ayni kullaniciya sinirsiz shell ve key/remote
credential erisimi verilmisken bir Python factory tam transport isolation degildir.
Private signer provisioning/confinement, host principal/credential siniri ve
desteklenen native istemci/cihaz kabulunun gercek kanitlari halen gereklidir.
Mevcut kullanici home'una bu gelistirmede kurulum veya ayar uygulanmadi.

Regresyon kaynaklari actual canonical runtime ve source validator fixture'lariyla
hazirlandi. Before/after consumption, post-push drift, replay ve CLI preview/missing
approval senaryolari kapsanir. Testler CALISTIRILMADI; tum gelistirmeler sonrasi
final test asamasinda calistirilacaktir. Work/WP/release kapanisi uretilmedi.
