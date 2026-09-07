# Otonom Evolution Karar ve Kanit Kaydi

Bu belge `ZEKAM-AUTONOMOUS-EVOLUTION-001` icin kalici, sanitize edilmis muhendislik
karar kaydidir. Work state veya approval authority'si degildir.

## OE-00 baseline

- Kaynak baseline: `main@b59221a0891dc94d3702d042132254066dc089ed`.
- Yeni authority digest: `sha256:4788116aa01885aa8b884579f4f8ee1ce87b76ee54f55c8fe884aec7e33580b2`.
- Onceki authority exact arsivi: `docs/archive/tasks/`.
- Baslangic paket dogrulamasi: passed; secret ve Git security bulgusu yok.
- Operational envanter: acik Work Item, calisan lease ve recovery case yok.
- Baslangicta iki OpenCode lifecycle teslimati spool'da terminal ACK bekliyordu. Bounded
  drain iki teslimati da ACK ile uzlastirdi; kalan kuyruk sifir ve receipt digest'i
  `sha256:24002679c0b0707e8a5314cbd0d525e4494eab297fc840c502e90d01c68a9177` oldu.
- Scope transition mevcut local runtime uzerinden claim-before-effect ve terminal receipt
  ile kaydedildi; status okumasi journal ve receipt'i birlikte dogruluyor.
- Provider cagrisi ve OS servis kurulumu yapilmadi.

## Reuse haritasi

| Hedef | Yeniden kullanilacak mevcut parca | Sinir |
|---|---|---|
| Kalici queue/claim/receipt | `SQLiteLocalRuntimeStore`, `LocalRuntimeService` | Legacy PostgreSQL worker composition kullanilmaz. |
| Tipli dispatch | `LocalOutboxDispatcher` ve mevcut reserved-operation kurallari | Serbest shell payload kabul edilmez. |
| Capture/continuity | Mevcut OpenCode spool, lifecycle bridge ve continuity paketleri | Idle/Stop session sonu sayilmaz. |
| Learning/skill | `SQLiteLocalLearning` candidate/evaluation/review/usage/outcome zinciri | Dosya olusmasi gercek kullanim sayilmaz. |
| Improvement | `SQLiteLocalImprovement` class, experiment, activation ve rollback ledger'i | Ledger varligi gercek rollout kaniti degildir. |
| Analytics/status | `LocalCoreServices` ve mevcut scheduler/doctor yuzeyleri | Tek bilesenin ready olmasi urunu ready yapmaz. |

## Uygulama kararlari

1. Ikinci scheduler, ikinci approval sistemi veya yeni veri motoru kurulmayacak.
2. Standing grant exact operation, resource, device/realm, task/policy digest, sure ve
   butceye baglanacak; `allow-all` biciminde olmayacak.
3. Cross-store effect en az-bir-kez teslim ve idempotent receipt/outbox ile uzlastirilacak;
   tek transaction iddiasi uretilmeyecek.
4. OS scheduler ve canli provider etkileri once read-only plan, sonra ayri exact yetki ve
   readback ister.
5. macOS bu Windows cihazinda gecmis sayilmayacak.

## Baseline sirasinda bulunan runtime hatasi

Ilk transition journal tick'i, journal executor'ina ait olmayan bekleyen
`oracle.metadata.read` isini claim etti. Executor operation kontrolunu dosya etkisinden once
reddettigi icin dis etki olusmadigi kod ve receipt zinciriyle dogrulandi; vaka `failed`
resolution ile kapatildi. Kok neden, `LocalRuntimeService` cagrisinin store'daki mevcut
`supported_operations` filtresini kullanmamasiydi.

Journal worker artik yalniz `local.append-journal/v1` allowlist'ini claim eder. Yabanci is
ready kalir ve recovery olusturmaz; regresyon testi bu ayrimi dogrular. Bu, OE-03 typed
dispatch icin genisletilecek temel guvenlik kuralidir.

## Sonraki exact uygulama sirasi

OE-01'de once provider-free domain sozlesmeleri ve negatif testler uygulanir: protected
resources, standing grant, child authorization ve transactional budget reservation.
Gercek OS kaydi veya uzak model cagrisi bu paketin parcasi degildir.

## OE-01 yetki ve butce siniri

- Mevcut `ImprovementChangeClass` persistence modulunden ortak domain modulune tasindi;
  eski import yuzeyi korunarak ikinci bir risk sinifi olusturulmadi.
- Standing grant; owner/actor, device/realm/project, logical source, exact typed
  operation+handler version, task/policy/verifier/validator/source/protected-manifest
  digestleri, model/provider, local/remote execution boundary, data/network scope,
  sure/review ve tum butce boyutlarina baglandi. Wildcard ve `allow-all` yoktur.
- `HUMAN_APPROVAL_REQUIRED` ve `PROHIBITED_AUTONOMOUS` parent grant'e giremez.
  `REVIEW_REQUIRED` child ise bagimsiz reviewer receipt'i olmadan turetilemez.
- Kaynak kodu yazma genel evolution grant'inden dislandi. Yalniz ayri
  `code-maintenance` grant'i, exact `source-file:` kaynaklari ve
  `maintenance.reconcile/v1` ile tanimlanabilir. Authorization, scheduler, receipt,
  rollback executor, secret/security/retention ve evaluator holdout yazilamaz.
- Child yetki mevcut `domain.security.Authorization` tipinde, exact plan/effect
  digest'ine bagli ve tek kullanimliktir. Parent grant dogrudan effect yetkisi degildir.
- Approval/review/current-runtime attestation/terminal readback icin append-only operational
  receipt tablolari ve bunlari exact binding, zaman ve bagimsiz actor kosullariyla okuyan
  `SQLiteEvolutionApprovalAuthority` eklendi. Unit fake'i production authority degildir;
  SQLite verifier entegrasyon testi ayrica calisir.
- Operational SQLite icin V5 migration ledger ve immutable schema fingerprint'ine
  kaydedilen DDL; append-only grant revision,
  revocation, child reservation, immediate pre-effect claim ve terminal receipt
  zincirini tanimlar. Grant/current revision/revoke/drift, kalan butce ve concurrency
  `BEGIN IMMEDIATE` icinde kontrol edilerek child ayni transaction'da rezerve edilir.
  Unknown veya receipt'siz calisma concurrency hakkini serbest birakmaz; idempotent
  replay ikinci butce dusumu yapmaz.
- V5 schema fresh integration fixture'i gercek SQLite uzerinde dogrulandi; hic grant
  olusmadigi ayrica okundu. DDL bu adimda mevcut cihaz operational veritabanina
  uygulanmadi ve canli grant kaydedilmedi. V3→V4→V5 icin ayni external admission,
  spool fence, non-overwrite backup, original-row parity ve postcommit verification
  zincirini kullanan `migrate_v3_to_v5` yolu eklendi. Windows-native public API;
  private ACL/reparse kontrolleri, `msvcrt` nonblocking migration lock, admission
  drain/assert/release, verified V3 backup, pinned source kimligi ve postcommit V5
  readback ile gecici guvenli home uzerinde uctan uca gecti. Database, backup ve lock
  hedefleri resolved exact trusted-home containment'ina baglandi; scoped parentlerin
  private ACL/reparse/file-ID snapshot'i lock, writer ve postcommit sinirlarinda yeniden
  dogrulandi. Kapsam disi hedef, guvensiz parent, lock contention, non-empty spool,
  source replacement, precommit rollback ve postcommit recovery negatif yollari gercek
  public API testleriyle kapsandi. POSIX descriptor-anchored yol degistirilmedi.
  Cihazdaki uygulama yine exact plan/onay olmadan calistirilmaz.
  `evolve status` bunu
  `standing-grant-operational-migration-not-admitted` olarak acikca gosterir; gorev
  dosyasi yetki yerine gecmez.
- Hedef OE-01 testleri Windows'ta gecti. Tum unit koleksiyonundaki macOS-only
  `pwd/fcntl` import hatalari bu Windows paketinin sonucu degildir ve basari diye
  gizlenmedi.

## OE-01 bagimsiz dogrulama

Bagimsiz subagent once Windows public migration hedeflerinin trusted-home disinda
kalabildigini P0, negatif Windows kabul kapsamlarini da P1 olarak buldu. Hedefler exact
trusted-home containment ve scoped parent ACL/reparse/file-ID snapshot'lariyla baglandi;
negatif kabul matrisi eklendi. Yeniden incelemede authority, 54 hedef test, Ruff ve strict
mypy gecti; kalan OE-01 P0/P1 veya blocker raporlanmadi. Bu sonuc canli grant, V5 migration
veya OS servis kurulumu yetkisi vermez.

## OE-02 capture baseline ve Codex contract drift'i

- Mevcut lifecycle spool; content-free observation, per-session hash chain, duplicate
  delivery idempotency, bounded pending cursor ve terminal continuity receipt sinirlarini
  zaten sagliyor. Yeni `EvolutionCaptureEnvelope` bunlara paralel kimlik uretmez;
  `LifecycleBridgePlan`, exact client contract ve canonical project/work/run/event
  kimliklerini alan esleme gorunumu olarak kullanir. Prompt/response/transcript tasimaz.
- Onayli kaynak replay karari exact source ref/digest ve son teslim edilmis sequence'e
  baglandi; tek tur 256 olayla sinirli. Kaynak yoksa, yetkili degilse veya pencere
  kaybolduysa `capture-gap` uretilir ve `inferred_content=false` kalir.
- Windows OE-02 baseline'inde kurulu Codex `0.153.1`, onceki current contract `0.150.1`
  bulundu. Onceki JSON kaniti degistirilmedi; yeni binary SHA-256 ve ayri
  `codex-0.153.1.json` kaniti eklendi. Resmi OpenAI hook belgesi SessionStart,
  PreCompact, PostCompact, Stop ve SessionEnd olaylarini; exact trust-review davranisini;
  `commandWindows` alanini ve SessionEnd icin mevcut `other` matcher degerini dogruluyor:
  <https://developers.openai.com/codex/hooks> (erisim 2026-09-06).
- Gercek `codex-cli 0.153.1` binary'si gecici Codex/Zekam home ve loopback Responses
  provider ile calistirildi. Bes lifecycle olayi spool'a dustu; prompt, cevap, workspace
  path ve transcript path kalici eventlerde bulunmadi. Bu test loopback/proxy gozlemidir;
  kernel seviyesinde egress-deny kaniti degildir.
- Gercek OpenCode `1.18.29` Windows runtime'i (native SHA-256
  `88d2fa691b2d9e32fde6d1039382a850ddf96fe49cd41683c6375fe1dc8ec2a5`) gecici
  XDG/user home, provider metadata cache ve yerel loopback server ile calistirildi.
  Managed plugin gercek `session.created` ve `session.deleted` olaylarini provider
  cagrisi olmadan content-free Zekam argv'lerine donusturdu; session basligi tasinmadi.
- Claude Code bu Windows cihazinda kurulu olmadigi icin native smoke `unverified` kalir;
  mevcut parser/contract unit testi native kurulum kaniti sayilmaz.
- Ilk bagimsiz OE-02 incelemesi capture projection'inin production composition'a
  baglanmadigini ve replay kararinin caller boolean'larina guvendigini P0 buldu. Duzeltmede
  caller authority bayraklari kaldirildi: bounded replay dogrudan immutable
  `ClientLifecycleSpool` session tail'inden geriye en fazla 256 kayit okur; tum session
  veya spool agacini taramaz. Kayip pencere karari ayni spool icinde
  content-free, digest-bound ve idempotent `capture-gap` olarak saklanir.
- Production Codex drain artik PostgreSQL terminal receipt ve ayri readback esitliginden
  sonra `EvolutionCaptureEnvelope` uretir. Device kimligi spool'un kalici instance
  receipt'inden; project/work/run/event, source ve evidence baglari canonical plan ile
  terminal receipt'ten gelir. Caller `device_id`, `parent_run_ref`, `evidence_refs`,
  source authorization veya source digest veremez. Capture receipt ayni immutable spool
  ACK v2 belgesine gomulur; duplicate replay ikinci capture uretmez. Path/assignment
  bicimli ref'ler persistence sinirinda reddedilir.
- Evolution capture hedef testleri duplicate, pre-compaction runtime binding ve DB commit
  sonrasinda capture/ACK oncesi ayri Windows child process'in `os._exit` ile sert
  kapanisini, SQLite terminal commit readback'ini ve restart replay'ini kapsar. Gercek Codex ve OpenCode
  native smoke yeniden gecti. Codex PreCompact/PostCompact'in CLI testine manuel
  enjeksiyonu native compaction kaniti olarak sunulmaz; bu kisim parser/spool parity
  kanitidir.
- Bagimsiz OE-02 final reverify 94 testi gecti; Ruff ve strict mypy temiz kaldi,
  P0/P1 bulunmadi. Ortam/kurulum kaynakli 39 skip, Claude `not-installed` ve native Codex
  compaction `unverified` durumlari PASS'e cevrilmedi.

## OE-00 bagimsiz dogrulama

Bagimsiz subagent incelemesi authority/archive/projection ve terminal transition zincirini
dogruladi. Ilk incelemede plan/status read-only composition'i ile eksik cryptographic
receipt binding'ini blokor olarak buldu. Duzeltme sonrasinda missing-home sifir-yazim testi,
18 bagimsiz hedef test ve 19 alanli transition drift matrisi gecti. Sonuc mevcut local
runtime journal'ina `evolution-verification:OE-00:4788116a` idempotency anahtariyla ve
terminal receipt ile baglandi. Bu kayit standing grant veya sonraki paketler icin yetki
vermez.

## OE-03 typed tick ve Windows supervisor plani

- `LocalEffectDispatcher`, operation adini exact allowlist'ten handler'a baglar. Production
  `local-runtime` composition'i journal ve `maintenance.reconcile/v1` icin ayni dispatcher'i
  CLI ve OS tick yolunda kullanir; payload serbest shell komutu tasiyamaz.
- `local-runtime tick`, UTC bes dakikalik pencereyi SQLite `local_scheduler_slot` ile
  tekillestirir; claim-before-effect, fixed-target maintenance journal'i, terminal evidence
  receipt'i ve bounded outbox drain uygular. Ayni penceredeki ikinci tick yeni job veya
  ikinci journal kaydi uretmez. Testte OS tick -> job -> gercek file effect -> terminal DB
  readback zinciri gecti; provider/network cagrisi yoktur.
- Windows Task Scheduler adapter'i `\Zekam\AutonomousEvolution` exact adini, kurulu
  `zekam.exe` SHA-256'sini, config digest'ini, `InteractiveToken`/`LeastPrivilege`, bes
  dakika, `StartWhenAvailable=true` ve `IgnoreNew` ayarlarini planlar. Bu logon tipi
  kullanici logoff sonrasi calisma vaat etmez. Install/uninstall exact plan digest ister,
  yalniz bu task adina dokunur ve terminal OS readback yapar.
- Bu cihazdaki salt-okunur plan digest'i
  `sha256:a791507bbde9433597dbccac4f7944ea834d2748493ba69e5a5de32a68a754ce`;
  Task Scheduler readback `absent` dondu. Plan `apply=false` ve
  `authorization_required=true`; gercek OS kaydi veya canli home tick'i uygulanmadi.
- Ilk bagimsiz inceleme; ayni adli drifted task'in overwrite/delete edilmesini, XML
  readback'in yalniz secili alanlara bakmasini, create-sonrasi query hatasinda rollback
  atlanmasini, tick'in kendi slot job'i yerine eski backlog'u claim edebilmesini ve
  handler'in schedule digest'ini yeniden hesaplamamasini P0/P1 olarak buldu. Duzeltmede
  install/uninstall yalniz exact matching task'a izin verir; exported XML tum agac,
  attribute, metin, cardinality ve child sirasi ile exact karsilastirilir; ek action,
  trigger, attribute veya element drift'tir. Create sonrasi her readback hata yolu exact
  delete ve ardindan `absent` readback ister; bu kanit yoksa `recovery-required` kalir.
  Tick exact kendi job kimligini claim eder ve handler UTC bes-dakika schedule govdesinin
  digest'ini bagimsiz yeniden hesaplar.
- OE-03 final bagimsiz reverify 82 testi gecti; 2 POSIX-only SIGKILL testi Windows'ta
  atlandi, Ruff ve strict mypy temiz kaldi, P0/P1 bulunmadi. Gercek Task Scheduler
  install/import-export normalizasyonu uygulanmadigi icin native install kabul durumu
  `unverified` kalir; salt-okunur `absent` status bunu PASS'e cevirmez.

## OE-04 learning/daylog ve trusted skill usage (kapandi)

- `SQLiteLocalLearning.daily_snapshot`, bir UTC gunundeki learning kayitlarini icerik
  tasimadan canonical record ref/digest ve sayaclarla derler. `LearningDailyEffectExecutor`
  schedule govdesini yeniden hesaplar; provider/model/network kullanmadan immutable
  generated Markdown daylog uretir. Ayni snapshot ikinci note uretmez; yeni snapshot
  content-addressed revision ve `supersedes` iliskisi olusturur.
- Generated predecessor kullanici tarafindan degistirilmisse eski byte'lar silinmez veya
  overwrite edilmez; yeni note ayri yazilir ve `conflicts-with` iliskisi olusur. Iki active
  conflict varken ayni snapshot mutation yapmadan conflict doner; yeni snapshot ucuncu
  active revision olusturmadan fail-closed olur.
- Skill usage ve outcome ayri kayitlardir. Yalniz exact aktif manifest profiline uyan
  `skill.execute.local-journal/v1` production handler'i bounded, secret-taramali kaydi
  fiziksel journal'a yazar. Usage kapisi public-deterministic receipt'i tek basina yeterli
  saymaz ve exact kaydi descriptor ile yeniden okur. Outcome ise
  ayri `skill.verify.local-journal/v1` isiyle journal'i yeniden acip exact kaydi okuyan
  deterministic receipt ile birlikte ayri append-only verification-audit kaydini da
  fiziksel olarak dogrulamadan yazilamaz. Generic handler'in exact public digest taklidi
  usage veya success uretemez: trusted handler private `SkillRuntimeSigner`, learning
  store ise seal uretmeyen `SkillRuntimeVerifier` capability'si alir. Yerel key private
  ACL/mode, bounded descriptor read ve create/reopen/path identity bagi ile korunur;
  restart sonrasi ayni issuer bagi devam eder. Eksik/tahrif edilmis journal yalniz
  `verified-failure` uretir; serbest verifier etiketi kabul edilmez. Skill input secret
  taramasi durable queue insertion'dan once calisir.
- Supervisor UTC gece yarisi ile acik gunu erken kapatmaz. Europe/Istanbul 21:00 kapisinda
  yalniz tamamen bitmis yerel gun due olur. Son receipt-backed daily watermark ile yeni
  due gun arasindaki gunler tek `start_day..day` snapshot ve tek job olarak birlestirilir;
  her kacirilan gun icin ayri is/cagri firtinasi olusmaz. UTC sorgu sinirlari yerel gun
  baslangic/bitisinden turetilir. Watermark yalniz slot adindan alinmaz: canonical daily
  payload/schedule, effect claim, completed receipt ve fiziksel generated daylog watermark
  birlikte eslesmek zorundadir.
- Daily materialization artik file-create/DB-confirm ve archive/DB-commit arasindaki iki
  hard-kill penceresinde exact dosya digest readback'i ile replay edilir. Eksik veya drift
  gosteren byte'lar otomatik basari ya da silent delete uretmez. Kabul testleri bu iki cut
  point'i Windows spawned child process icinde `os._exit` ile keser ve yeni process/store
  handle'lariyla recovery readback yapar.
- OE-04 final bagimsiz reverify PASS verdi; kalan P0/P1/P2 yok. Parent hedefli kapisi
  166 PASS, 2 Windows'ta yetkisiz POSIX symlink senaryosu skip; Ruff, strict mypy,
  authority/projection, package ve secret taramasi temizdir. Bu sonuc native Task Scheduler
  kurulumu veya uzak model/provider cagrisi yapildigi anlamina gelmez.

## OE-05 baseline/holdout ve source refresh (kapandi)

- Maintenance, knowledge ve skill icin exact typed metric profilleri tanimlandi. Frozen
  dataset; calibration/holdout ayrimini, yirmi case'i ve her case icin provenance receipt'ini
  tasir. Baseline ile candidate ayni case, condition, fixture, harness ve environment bagi
  altinda paired olarak olculur; correctness/citation guardrail'i bozulurken maliyet kazanci
  `improved` uretemez, yetersiz kanit otomatik PASS olmaz.
- Provenance, builder, evaluator ve verifier farkli gercek spawned process kimlikleridir.
  Ed25519 private key yalniz child process icinde kalir; parent generic payload imzalatamaz.
  Evaluator parent tarafindan verilmis outcome veya metric kabul etmez, verifier raporu ve
  execution receipt zincirini yeniden hesaplar. Her worker icin signed terminal receipt
  evaluation ledger'ina baglanir; yalniz farkli etiket kullanmak bagimsizlik sayilmaz.
- Holdout provenance worker'i beklenen sonucu sabit golden artifact, fixture, harness ve
  case input'tan canonical olarak turetir. Dataset contract'i adaydan once SQLite'a yazilir;
  `propose` ayni `BEGIN IMMEDIATE` icinde bu satiri yeniden okur ve contract digest'ini
  candidate'a immutable FK olarak baglar. Caller `registered_at` degeri yalniz audit
  metadata'sidir ve ordering/policy karari vermez. Persisted public key, terminal receipt ve
  tum case receipt'leri yeni store instance'inda yeniden kurulup dogrulandigi icin restart
  trust zincirini kaybetmez.
- HTTPS source refresh exact host/path allowlist, public DNS/IP pinning, peer-IP readback,
  hostname TLS verification, redirect/auth/query/fragment/private-target reddi, bounded
  metin ve secret taramasi uygular. Ayni icerik model cagrisi veya candidate uretmez; degisen
  dis metin yalniz executable-olmayan review candidate olabilir. Paket/script calistirma veya
  otomatik dependency upgrade yolu yoktur.
- OE-05 final bagimsiz verifier sonucu PASS ve kalan P0/P1 yoktur. Focused kabul 23/23 PASS;
  Ruff ve strict mypy PASS; provider/model/network cagrisi sifirdir. Genis Windows regresyon
  seciminde 245 PASS ve 38 ortam-bagimli SKIP vardir. Gercek yerel improvement ledger'i
  owner/SYSTEM/Administrators ACL'iyle readback edildikten sonra kayipsiz v3->v4 migration
  ve FK/integrity audit'i PASS'tir. App-managed state/model/benchmark/analytics koklerindeki
  eski inherited ACL'ler ayni policy ile normalize edilmis ve native `local-core status`
  `all_ready=true` readback vermistir. Tum-suite collection ayrica macOS-only `pwd` ve
  `fcntl` importlari nedeniyle Windows'ta baslamadi; bunlar OE-05 basarisi gibi gosterilmez.

## OE-06 shadow/canary/activation/rollback (kapandi)

- Rollout planlari standing grant, exact child authorization, protected local resource
  manifesti, candidate/evaluation/fixture/harness ve before/LKG digest'lerine baglidir.
- Executor ve verifier farkli Windows spawned process'leridir; Ed25519 process-boundary ve
  terminal receipt'leri caller tarafindan uydurulamaz. Verifier artifact uygulamasini ve
  readback'i bagimsiz yeniden hesaplar.
- Shadow ve canary metadata simulasyonu degildir: private registry'deki iki gercek bounded
  fixture implementation'i inputlari calistirir. Sonuclar ve canary invocation kayitlari
  transaction icinde saklanir. Bu kabul `local-deterministic-fixture` kapsamindadir;
  `production_traffic=false` ve production canary basarisi iddia edilmez.
- Activation ve rollback selector degisikligi, evidence ve transition tek SQLite
  transaction'inda CAS ile yapilir. Kullanici selector'u degistirdiyse recovery-required
  olur ve yeni deger korunur.
- Effect sonrasi kesinti kor calistirma uretmez. Append-only recovery transition exact onceki
  digest'e bounded CAS uygular; evolution child terminali ve improvement recovery receipt'i
  yazilarak concurrency/butce serbest birakilir. Shadow/canary/rollback no-effect veya reverse
  recovery ayni terminal yola sahiptir.
- Eski metadata-only `record_rollout`, `activate_auto` ve `rollback` girisleri fail-closed'dur;
  eski tablolar yalniz tarihsel audit uyumlulugu icin korunur. Typed settlement ancak exact
  durable evolution terminali ile finalize edilir.
- Improvement schema additive v8'e tasindi. Bu Windows cihazinda migration sonrasi native
  `local-core status` `all_ready=true` dondu. Hedef OE-06/OE-05 regresyon secimi 72 PASS;
  Ruff ve strict hedefli mypy PASS, provider/network cagrisi sifirdir. Bagimsiz final
  verifier, completed terminal ile settlement arasindaki kill penceresinde pointer'in geri
  alinabildigi P0'i buldu. Recovery artik terminali once okur: completed terminal yalniz
  prepared settlement'i finalize eder; unknown adjudication ister; failed/absent terminal
  bounded recovery izler. Yeni connection ve yeni worker'larla restart testi candidate
  selector'un korundugunu kanitladi; final reverify P0/P1/P2 olmadan PASS verdi.

## OE-07 durum yuzeyleri ve runbook (kapandi)

- `evolve candidates` ve `evolve report`, canonical improvement ledger'ini bounded ve
  salt-okunur okur; prose/Markdown'dan work state uretmez. Legacy aktivasyonu yetkili veya
  fixture canary'yi production olarak raporlamaz.
- `capabilities` envanterinde autonomous evolution ayri `partial` yetkinliktir. Exact Windows
  supervisor plani ve standing grant kurulmadan canli `ready` yazilmaz.
- `evolve pause/resume/disable/run-once/enable` yuzeyleri eklendi. Append-only v8 control
  ledger'i yeni tick admission'ini gercekten keser; disabled durum normal resume ile acilmaz.
  `run-once` debug bypass degil ayni supervisor tick handler'idir. `enable`, exact Windows
  plan digest'i olmadan mutation yapmaz ve standing grant urettigini iddia etmez.
- Doctor `runtime.autonomous-evolution` kontrolu, observatory
  `/api/observatory/evolution` endpoint'i ve CLI status ayni kanonik ledger durumunu okur.
  Eski Markdown/rapor work authority sayilmaz.
- Windows testlerinde skill runtime anahtarinin binary bayrak olmadan okunmasi halinde rastgele
  `0x1A` baytinda kesilebildigi bulundu. Key create/reopen artik `O_BINARY`, acik handle ve ACL
  readback'i kullanir; replacement negatif testi korunur.
- Ilk final verifier, `resume` yolunun grant/recovery/scheduler drift'ini yeniden kontrol
  etmedigini, status/report/doctor/observatory effective state'lerinin ayrisabildigini ve
  plan/rapor sozlesmesinin handler/model/butce/heartbeat/last-effect/usage/metric/capture-gap
  alanlarini eksik gosterdigini buldu. Resume artik ancak current standing grant, sifir recovery,
  matching supervisor ve guncel task/config/implementation digestlerinden turetilen append-only
  admission evidence ile acilir. Tum yuzeyler tek fail-closed state reducer'ini kullanir; plan ve
  rapor eksik alanlari kanonik ledgerlardan, veri yoksa acik sifir/unavailable olarak uretir.
- Ikinci reverify self-consistent fakat sahte resume evidence'inin persistence katmaninca
  kabul edilebildigini ve evaluation ledger'indaki gercek metrik/usage degerlerinin raporda
  yalniz digest/sayac olarak kaldigini buldu. Production resume artik current authority'yi
  mutation sinirinda yeniden okuyan verifier ister: latest grant'in exact approval receipt'i,
  revocation/expiry/review zamani, yedi task/policy/verifier/validator/source/protected/dependency
  binding'i, matching supervisor ve recovery state'i yeniden dogrulanir. Sahte verifier'siz
  evidence negatif testte reddedilir. Rapor bounded evaluation pair'larinda baseline/current
  degerlerini, progress sonucunu, gercek provider/token/cost kullanimini; adaylarda acik risk ve
  evaluation/review/prohibited blocker'larini gosterir.
- Ucuncu bagimsiz final reverify 91 hedef testi, Ruff, strict hedefli mypy, package manifest
  check ve paket dogrulamayi gecirdi; OE-07 icin P0/P1/P2 kalmadi. Bu kabul Task Scheduler
  native kurulumu, iki gercek otomatik cycle veya standing grant aktivasyonu yerine gecmez.

## OE-08 native kabul ve devir (kapandi)

- Baseline'daki son dort commitin author/committer kimligi, kullanicinin exact talebiyle
  `mehmet-karacan <karacan.mehmet@hotmail.com>` olarak history rewrite edildi. Eski
  `8761790...` baseline'i ayni agac ve patch'i tasiyan `b59221a...` commitine donustu.
  Immutable scope-transition receipt'i silinmedi veya yeniden yazilmadi; runtime yalniz exact
  eski/yeni authority ve baseline digest ciftini receipt-bound author-rewrite koprusu olarak
  kabul eder. Baska authority veya source degisikligi bu istisnadan yararlanamaz.

- Kod, unit/integration, spawned-process chaos/recovery ve provider-free iki-tick davranisi
  calisti. Gercek Task Scheduler kaydi bu cihazda halen `absent`; exact plan CLI tarafindan
  launcher, package implementation manifesti ve config digest'ine bagli uretilir. AGENTS.md
  geregi guncel digest gosterildikten sonra ayri acik onay olmadan kurulmaz.
- Operational authority DB halen v3'tur. V3->v5 migration; evolution control'u duraklatan,
  expired runtime/outbox recovery calistiran ve terminal olmayan claim/lease/recovery varken
  fail-closed duran production admission adapter'ina baglandi. Combined bootstrap exact plani
  etkiden once owner-private immutable intent'e ve append-only control ledger'a exact claim yazar;
  migration sonrasi ayni digest ile backup, schema, grant, realm ve task readback'inden idempotent
  devam eder ve yalniz tum zincir dogrulaninca exact terminal event uretir. Precommit kesintisinde
  kalmis backup ancak ACL, v3 schema, logical ve original-row digest eslesirse reuse edilir. Intent
  dosyasi no-follow descriptor identity ve bounded read/write primitive'lerini kullanir. Canli DB ayri exact onay
  olmadan migrate edilmedi ve standing grant sayisi sifirdir.
- Canli inventory'de GPU ile SKY backend ayni realm'e bagli, SKY UI binding'i eksiktir. Combined
  plan yalniz bu eksik project-realm satirini ayni realm'e ekler ve grant oncesi tum uc project
  scope'unu exact geri okur; yeni realm tahmin etmez.
- A03/A04/A06-A49'un provider-free kod/test kapsami vardir; A05 ve A50'nin gercek OS
  tetikleyicili kaniti Task Scheduler kurulumu olmadan `blocked-on-native-authorization`,
  production model/traffic ve cok cihaz kabulü ise `unverified` kalir. Bu ayrim Global DoD
  sayilarini degistirmez.
- OE-08 bootstrap final bagimsiz verifier; exact claim digest'in migration admission, realm,
  grant, scheduler ve terminal sinirlarinda yeniden dogrulandigini, araya giren owner
  pause/disable olayinin asilamadigini ve terminal CAS'in yalniz ozgun claim'i kabul ettigini
  dogruladi. Son verifier kosusunda 57 hedef test, Ruff ve release manifest check PASS; ana
  hedef regresyon kosusunda 163 test PASS oldu. Bu kod/verifier kabulu, A05/A50 native iki
  otomatik cycle kaniti yerine gecmez.

### Windows native readback recovery karari

- Ilk onayli bootstrap girisimi trusted home ACL kontrolunde etkiden once durdu. Yalniz
  `.zekam` kokundeki inherited olmayan `CodexSandboxUsers` read ACE'si kaldirildi; state ve
  database ACL'leri zaten private idi. Owner/SYSTEM/Administrators readback'i sonrasinda ayni
  immutable intent ile devam edildi.
- Ayni girisim operational v3->v5 backup/migration, uc project-realm binding'i ve provider-free
  standing grant'i terminal readback ile uyguladi. Task Scheduler ise olusturulan XML'i native
  exportta siraladi, principal adini SID'e cevirdi, UTC boundary'yi esdeger `+03:00` instant
  olarak yazdi, default alanlari atip pil/idle/unified defaultlarini ekledi. Eski ordered byte-tree
  matcher bu semantik olarak esdeger exportu drift saydi; installer exact task'i geri alip
  `absent` readback'i kanitladi. Bu nedenle bootstrap terminal olmadi ve control bilincli olarak
  `paused/bootstrap-claim` kaldi.
- Scheduler plan schema `v2` oldu. Principal SID artik plan digest'ine bagli; boundary ayni UTC
  instant olarak karsilastirilir. Native pil/idle/unified davranislari plan ve uretilen XML'de
  acik alanlardir. Yalniz kanitlanan default omission'lari kabul edilir. Bilinmeyen/yinelenen
  action, trigger, principal, element veya attribute; farkli capability ve hassas leaf whitespace
  drift olarak reddedilir. Readback XML'i 1 MiB ile sinirlidir.
- Unit/regresyon kaniti native receipt degildir. Kaynak ve implementation digest'i degistigi icin
  eski bootstrap digest'i tekrar kullanilamaz; yeni exact recovery plan ayri kullanici onayi ve
  gercek `Export-ScheduledTask` terminal readback'i ister.
- V2 matcher ile ilk gercek zaman tetigi `09:55:01+03:00` aninda native olarak basladi; ancak
  action exit `70` verdi ve heartbeat olusmadi. Hata Task Scheduler degil,
  `SQLiteLocalRuntimeStore` constructor'inin admitted operational v5 dosyasinda eski default v3
  `bootstrap()` yolunu cagirip `downgrade forbidden` uretmesiydi. Constructor artik yeni dosyayi
  v3 olustururken mevcut desteklenen v3/v5'i mutation olmadan yeniden acar; eski v1/v2
  `migration-required` hata sozlesmesi korunur. V5 reopen ve eski-schema negatif regresyonlari
  eklendi.
- Kaynak driftinden sonra control owner-pause ile yeni effect admission'ina kapatildi. Eski task
  `10:00:01+03:00` tetiginde `not-admitted` yoluyla exit `0` verdi. Ardindan task marker, tek action,
  executable, arguments ve working directory exact readback ile dogrulanip yalniz bu stale Zekam
  task'i silindi; terminal readback `absent` oldu. Bu iki tetik A05/A50 basarisi sayilmaz.

### Final Windows native kabul

- Kullanici `sha256:ec39a6608193a173080f6bee37346ecbd8d34772ba12c5a0b681ea9b48985035`
  bootstrap planinin ayni bounded kapsamda uygulanmasini yetkilendirdi. V5 readback `already-v5`,
  standing grant aktif, supervisor exact planla `matching` ve provider/network kullanimi sifir
  olarak geri okundu.
- Windows PowerShell 5.1 `Get-WinEvent` autoload'u PowerShell 7 modul yoluyla karisabildigi icin
  olay okuyucu Windows'un kendi `Microsoft.PowerShell.Diagnostics.psd1` modulunu exact sistem
  yolundan yukler. Sorgu yalniz task EventID 201 kayitlarini ve en fazla 16 sonucu okur.
- `11:00:01+03:00` ve `11:05:01+03:00` native tetikleri Task Scheduler sonucu `0` ile bitti.
  Kanonik kabul, bunlari `08:00:00Z` ve `08:05:00Z` slotlarindaki exact schedule digest,
  idempotency key, job, tek claim, tek completed receipt ve terminal evidence zinciriyle esledi.
  Kabul evidence digest'i
  `sha256:75b428f206f36c9ebe83687ee716c000d8b6e4e3d034ecb651d39a4d86bc2aa7` oldu.
- Final `evolve status` durumu `observing`, `setup_gaps=[]`, `blockers=[]`, supervisor `matching`,
  aktif grant `1`, drifted grant `0` ve OE-08 `independently-verified` olarak geri okundu.
- Eski doctor persistence kontrolunun yalniz v3 bekleyip admitted v5'i yanlis bloke etmesi
  giderildi; desteklenen runtime semalari v3 ve v5 exact digest'leriyle kabul edilir, v4 ve
  bilinmeyen semalar fail-closed kalir. Final SQLite doctor kategorisi `healthy` dondu.
- Author-only history rewrite sonrasinda onceki task-scope grant'leri current admission icin
  yetki vermedigi halde drift sayiliyordu; current-scope filtresi ve revoked-grant ayrimi
  eklendi. Ayni current scope icindeki binding sapmalari fail-closed drift olarak kalir.
- PowerShell process baslatma veya timeout arizasi native kabul okuyucusunu exception ile
  dusurmez; bounded okuyucu bu durumda authority vermeyen fail-closed `missing` sonucu uretir.
