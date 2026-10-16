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

### Native CLI ve public trust

`git publication` alt yuzeyi native ajanin mevcut kimlik/kayitlardan cozecegi
girdilerle kullanilir. Kullaniciya UUID, digest, JSON veya komut hazirlama isi
verilmez. `plan <proje> <run> --executor-id ...` iki evidence henuz yoksa yalniz
subject/binding/target dondurur; sahte kanit veya yayin plani uretmez. Gercek
evidence digest'leriyle tekrar planlandiginda full immutable plan dondurulur.

- `import-evidence`: trusted producer'in imzali, gercek completed job/CAS
  kaynakli kanit envelope'unu kabul eder. `--uygula` yoksa imza dogrulandi demez.
- `import-approval`: ayri approval producer'in exact plan icin imzali one-shot
  Authorization envelope'unu kabul eder; CLI signer veya onay uretmez.
- `apply`: full plan dosyasi, exact plan digest, authorization ve `--uygula`
  ile mevcut canonical servis/transportu kullanir. Config/source drifti yeni
  yayini reddeder; tamamlanmis exact replay yeni trust/remote cagrisi yapmaz.
- `status`: imzasi dogrulanan canonical yetki durumunu salt okunur verir.
- `revoke`: imzali iptal envelope'unu kabul eder. Exact tekrar yeni kayit
  uretmez; ayni kimlikte farkli icerik immutable replay drift'tir.

Public producer registry `ZEKAM_HOME/config/git-publication-trust.json` dosyasidir.
Loader bu dosyayi olusturmaz veya sahiplenmez; private regular file ve linksiz
parent zinciri ister. Schema `zekam-git-publication-trust/v1`, pozitif revision,
canonical realm UUID ve 3..64 producer kaydi vardir. Her producer yalniz id,
role (`approval`, `test`, `verification`) ve 32-byte Ed25519 public key'in canonical
base64 degerini tasir. ID/public key tekrar edemez; her rol bulunmalidir. Private
key, signer veya ek freeform alan kabul edilmez.

Registry'nin logical digest'i aktif operational config revision'indaki
`git_publication.trust_digest` ile exact eslesmelidir; config'in kendi sanitized
payload digest'i de dogrulanir. Caller'in secebildigi bir trust-file veya public
key CLI secenegi yoktur. Gercek key provisioning, config aktivasyonu ve native
producer enrollment kendi yetki/kayit akisindan gelir; bu gelistirme kullanici
home'una trust/anahtar yazmadi. Mevcut canonical job runtime'in exact
`state/operational.db` yolu disindaki bir konfigde sessizce baska DB'ye
gecilmez; acik hata verilir.

16 Ekim 2026 typed config bagi: kalici config.yaml'in optional
`git_publication` bolumu yalniz exact `trust_digest` alanini kabul eder. Pin,
canonical sha256 metin olmalidir; null/bool/extra field ve root dotted-key
alias'i reddedilir. Settings.sanitized() configured pin'i korur; normal config
revision aktivasyonu bu degeri kaybetmez. Session override, nested veya dotted
key ile kalici trust/CLI integration tercihini degistiremez. Yapilandirilmamis
durumda yeni config alani, registry veya signer olusmaz.

Publication CLI hem loaded Settings pin'ini hem aktif config'e bagli registry'yi
ister; aralarindaki drift reddedilir. Bu bag kendi basina key provisioning,
producer enrollment, config aktivasyonu veya yayin yetkisi degildir. Gercek
Settings -> sanitized active SQLite config -> registry loader/CLI composition
ve malformed/session/drift regresyon kaynaklari hazir; CALISTIRILMADI.

## Ilk public registry enrollment

Signed public registry rotasyonu icin ayri canonical producer/CLI eklendi;
ayrinti `docs/PUBLIC_REGISTRY_ROTASYONU.md`. Onceki receipt head'i, eski approval
signature ve yeni admitted pin baglanir; key sahipligi/native kabul aciktir.

`git publication trust-plan <registry-file> --project <kayitli-proje>` incelenmis
public producer metadata'sini mevcut kalici Settings ve aktif canonical config
pin'iyle eslestirir. Registry semasi loader ile ortaktir; strict bounded JSON,
duplicate/nonfinite reddi, distinct ID/public key ve uc rol zorunludur. Bu yol
secret/private key uretmez veya okumaz. Pin'in config'te bulunmasi tek basina
config revision aktivasyonu, Work veya yayin izni sayilmaz; bunlar mevcut
otoritelerden gelir.

`git publication trust-enroll <plan-file>` preview'dur. `--uygula`, exact
`--plan-digest` ve `--run-id` ile mevcut canonical Work/Run ve admitted config'e
bagli `publication.trust.enroll` claim/checkpoint/receipt yolu calisir. Gerekirse
historical binding icin `--work-revision` explicit secilebilir. Apply generic
worker'a verilemez. Public registry yalniz ilk revision ve sabit
`config/git-publication-trust.json` hedefindedir; existing/private olmayan/link
hedef benimsenmez veya uzerine yazilmaz. Yeni config dizini gerekirse claim
sonrasi private kurulur; mevcut dizinin ACL'i sessizce degistirilmez.

Exclusive private file creation, flush/fsync ve exact disk readback sonrasi
canonical terminal receipt uretilebilir. Completed replay kendi receipt/CAS ve
exact registry dosyasini okur; eksik/drift dosyayi yeniden yaratmaz, current
config'i degistirmez veya yeni signer/yayin authority'si uretmez. Baslamis fakat
receipt'siz is tekrar yazilmaz; explicit recovery gerekir. Ilk enrollment
rotasyon, signer provisioning veya producer'in gercek execution/model
bagimsizligi kaniti degildir. Signing key ownership/rotation ve gercek test/
verifier producer kabulleri aciktir.

SQLite/CAS live claim-oncesi yazma reddi, completed replay/missing file,
foreign Work/config drift, existing target, strict schema ve rotation reddi
regresyon kaynaklari hazirlandi. Kullanici talimatiyla CALISTIRILMADI. Bu kaynak
gelistirmesi kullanici home/config/anahtarini degistirmedi; canli enrollment,
provider veya yayin cagrisi yapilmadi.

Canonical runtime admission callback'i effect oncesinde ve sonrasinda cagirir.
Enrollment bunu process-local own_write_completed durumuyla ayirir: once
target absent, kendi fsync/readback'inden sonra exact target content zorunludur.
Bu durum disaridan plan/CLI ile secilemez; baslamis baska is veya existing dosya
adoption izni vermez. Post-effect config, pin, plan veya file drift terminal
basariyi engeller. Bu callback regression kodu hazir, CALISTIRILMADI.

Operational v9 `SQLiteGitPublicationLedger` gercek mevcut DB'de imzali kanit,
approval, consumption ve revocation tablolarini append-only tutar. Guvenilen
producer verifier capability'leri host composition tarafindan verilir; store
signer key almaz veya kendi approval'unu uretmez. Test ve verification producer
kimlik/capability'leri ayri olmalidir. Evidence ancak gercek completed canonical
job/Run/receipt/artifact bagiyla ve CAS byte/digest/size readback'iyle kaydedilir;
her okumada ayni zincir tekrar dogrulanir. Tuketim canli claim ve lease, guncel
Work binding, yeni imza/kanit/CAS kontrolu ve tekil insert'i ayni kisa transaction'da
yapar. Ag veya provider cagrisi bu transaction'a girmez. Replay eski tamamlanmis
canonical kaniti okur; tuketilmis yetki yeni effect uretmez.

V8->v9 additive migration mevcut digest-bound dry-run/apply, pause/quiescence,
fencing/spool, online backup ve row parity yoluna baglidir. V1-v8 pinleri ve
varsayilan v3 bootstrap korunur. Migration mevcut/aktif approval, signer veya
trust kaydi olusturmaz; kullanici DB'sine uygulanmadi. V9 pinleri yalniz mevcut
in-memory schema metadata generator'uyla gelistirme sirasinda uretildi.

Bu dilim typed service/gercek Git transport/canonical job baglantisidir.
Concrete canonical authorization/evidence ledger ve CLI composition eklendi;
gercek trusted producer provisioning/enrollment, test/verifier receipt producer
baglantisi ve native host transport enforcement kabulu halen aciktir. Mevcut
`git push-check` advisory domain kapisidir; bu typed servisin yerine kullanilmaz.
Kaynakta baska yerde `evaluate_push` sonucunun true olmasi yayin yetkisi degildir.

SQLite/CAS claim/receipt/replay, scope/revocation, bagimsiz verifier, unknown
recovery ve gercek yerel bare-Git transport regresyon kodu hazirlandi. Kullanici
talimatiyla HICBIR test calistirilmadi; tum gelistirmeler sonrasi final toplu
asama beklenir. Kullanici DB migration'i, global ayar, canli provider veya bu
adaptorden gercek remote yayini yapilmadi. WP-02/Work kapanisi uretilmedi.
