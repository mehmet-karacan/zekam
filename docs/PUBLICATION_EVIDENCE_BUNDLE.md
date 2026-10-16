# Test ve verifier kanit ciftinin canonical kabul yolu

`publication evidence-bundle` exact publication subject ve gercek test/verifier
producer job'larindan tam evidence-bound plan uretir. Export JSON authority sayilmaz:
actual completed producer receipt, native test ve review source okuyuculari yeniden
calisir. Model, signer veya Git/network cagrisi yapilmaz; preview disk yazmaz.

Iki producer ayni publication binding/realm/subject'e baglanmali; test producer'in
source job/receipt/Run kimligi verifier review'un gercek native test kaynagiyla
eslesmelidir. Farkli test olcumlerini ayni commit adiyla karistirmak reddedilir.
Verifier execution testten, verifier executor publisher ve test executor'dan ayridir.

`--uygula` canonical `publication.evidence.bind` claim ve exclusive project/global
trust lease altinda guncel kaynak/ham rapor/registry readback'ini yapar. Sonra iki
signed evidence kaydi tek kisa SQLite transaction'inda kabul edilir. Her kaydin
gercek canonical source project/Work/config/snapshot ve receipt/CAS'i denetlenir.
Ikinci kayit basarisizsa ilk insert de rollback olur. Mevcut exact kayitlar immutable
replay olarak korunur; baska source/signature ile adoption yoktur.

Post-effect source/trust drifti completed admission receipt'ini engeller. DB'de
kayit cifti kalmis olabilir; unknown effect sessiz yeniden yurutulmez ve recovery
gerekir. Completed canonical replay kaynak/Git/model/imza veya yeni insert yapmaz;
actual producer/ledger/CAS ve aktif trust baglarini yeniden okur.

`publication evidence-admission` actual completed claim/checkpoint/CAS/receipt ve
iki admitted ledger kaydini tekrar dogrular. Donen tam plan ayri trusted approval
icin girdidir. Preview veya admission push onayi/Work kapanisi uretmez. Tamamlanmis
rapor tarihsel source proof'tur; guncel disk dogrulamasi yaptigi iddia edilmez.

Bu dilim private key olusturmaz/okumaz ve native shell credential isolation'i
kurmaz. Host signer provisioning/confinement ve native transport enforcement'in
gercek cihaz kabul kapilari aciktir. Real SQLite/CAS transaction ve canonical
receipt regresyon kaynaklari hazir; upstream producer okuyuculari fixture'dir.
Testler kullanici talimatiyla CALISTIRILMADI; tum gelistirmeler sonrasi finaldedir.
