# Kalan Gelistirmeler Uygulama Plani

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. User instruction overrides intermediate test execution: run tests only in the final phase after all development.

**Goal:** AKTIF_GOREV.md WP-02..12 kapsamindaki kalan gelistirmeleri sirayla tamamlamak.

**Architecture:** Mevcut native CLI, canonical SQLite Work/lease/receipt ve CAS yollari genisletilir. Belge ve projection operational authority uretmez.

**Tech Stack:** Python, SQLite, CAS, native CLI adapterleri.

**Spec:** AKTIF_GOREV.md (tek kapsam authority'si).

## Global Constraints

- Gercek kaynak kokunde calis; worktree veya kopya olusturma.
- Kullanici karari: tum gelistirmeler bitmeden test calistirma; son asamada tek toplu test kosusu.
- Her asama Turkce ASCII commit ve origin/main push; test bekleme durumunu kaydet.
- Yeni canli provider/model cagrisi, kurulum ve global ayar kendi exact iznini gerektirir.
- Korunan untracked natural_request_rag dosyalari ve Java target dizinine dokunma.
- Work kapanisi ve release kabulu icin gereken bagimsiz kaniti uydurma.

## Review Focus

- Lease startedAt process incarnation degildir; canli spool sahibi korunur.
- Ayni revision/tree farkli kaynak kokunu esdeger yapmaz.
- Native checkpoint Work veya grant yetkisi tasimaz.
- Tekrar edilen delivery effect'i cogaltmaz; eksik receipt recovery gerektirir.
- Test ertelenmesi gelistirme veya kabul basarisi olarak raporlanmaz.

## Sirali gorevler

- [ ] 1. WP-07.05: spool candidate processStartedAt ayrimi ve native source root drift reddi. Dosyalar: opencode_spool.py, local_continuity_native.py; regresyon kodu ilgili mevcut test dosyalarinda hazirlanir.
- [ ] 2. WP-07.01..04: canonical Work dogal dil ve resume/lease/grant akislarindaki kalan uygulama bosluklarini kaynak incelemesiyle belirle ve tamamla.
- [ ] 3. WP-02/03: native mutation admission, istemci ve shell/publication transport baglantilarini tamamla.
- [ ] 4. WP-04/05/06: discovery/parser/secret ve incremental publication/reconciliation; issue/history/provenance baglantilarinin kalan uygulamasini tamamla.
- [ ] 5. WP-08/09/10: host capability, model/agent envelope, test muhendisligi ve memory/skill lifecycle baglantilarini tamamla.
- [ ] 6. WP-11/12: scheduler/backup/restore/report ve canonical mimari/release ciktilarini tamamla.
- [ ] 7. Butun gelistirme maddelerinin kaynak kapsamini yeniden denetle; gerekli gercek ortam/erisim eksiklerini exact kapilara bagla.
- [ ] 8. Yalniz tum gelistirmelerden sonra tek toplu test asamasi, gereken bagimsiz review ve kabul kanitlari. Hata varsa duzelt ve yalniz basarisiz kapsami yeniden dogrula.

## Yurutme kaydi

- 17 Ekim 2026 CLI yuzeyi: `registered_commands` recursive Typer grup taramasina
  alindi; `git publication` alt komutlari canonical surface denetiminde artik
  gorunur. Kaynak manifesti yenilendi; AST/diff kontrolleri yapildi, pytest
  calistirilmadi. Commit: `c7872e3`.

- 17 Ekim 2026 aktif gorev projection: `AKTIF_GOREV.md` degisikligi sonrasi
  eski digest tasiyan generated YAML projection canonical contract ureticisiyle
  yenilendi. Commit: `28056f7`.

- 17 Ekim 2026 research/analytics sinirlari: research plan RAG source identity
  alanlarini raw `KeyError` yerine bounded policy gate ile dogruluyor; bos,
  yeni analytics source census'i `analytics-events-missing` olarak raporluyor.
  AST/diff kontrolleri yapildi, pytest calistirilmadi. Commitler: `419dd03`,
  `40d4029`.

- 17 Ekim 2026 canonical fixture hizalamasi: research CLI fixture'i zorunlu
  `tree_digest` source identity alanini tasiyor; operasyonel SQLite schema
  status fixture'i v9 head ve desteklenen head listesiyle eslendi. Kaynak
  degisikligi yapilmadi, AST/diff kontrolu yapilacak, pytest final kapisina
  kadar calistirilmayacak.

- 17 Ekim 2026 research kimlik siniri: RAG planina giren source revision, tree
  ve generation alanlari artik yalniz canonical SHA256 digest olarak kabul
  ediliyor; bos veya bozuk degerler raw hata yerine bounded PolicyViolation
  uretiyor. AST/diff ve manifest kontrolden gecirilecek, pytest final kapisina
  kadar calistirilmayacak.

- 17 Ekim 2026 RAG route kapisi: dis CLI route okuyucusu da canonical local
  multilingual-e5-small profilini dogrulamadan remote veya legacy route
  dondurmuyor. Bozuk ayar provider cagrisi veya indeks yolu baslamadan bounded
  configuration hatasiyla kesiliyor. AST/diff ve manifest kontrolu yapildi;
  pytest final kapisina kadar calistirilmayacak.

- 17 Ekim 2026 yerel E5 runtime guvenligi: kurulum manifesti artik sabitlenen
  yedi dosyanin tamamini source revision, boyut, SHA256 ve upstream SHA bagiyla
  yeniden okur; model agirligi beklenen SHA256 ile tekrar eslesir. Model yolu
  boyunca junction/symlink/reparse parent kabul edilmez. Proje yerel ince giris
  marker'i `.ai` ve `PROJE_MANIFESTI.yaml` icin ayni regular/reparse kontrolunu
  uygular. Yeni regresyon kaynaklari eklendi, AST/diff kontrolleri yapildi;
  pytest CALISTIRILMADI. Commitler: `6cbd2d2`, `af55982`, `44c0afc`,
  `5b9ccd8`, `7b739bf`, `8c84bad`.

- 17 Ekim 2026 kaynak denetimi: onceki "compose_agent_gateway generic
  digest-flag" acigi artik gecerli degil. `unit_test_runtime.py` gercek
  `opencode_batch_adapter` V2 run/standalone/JSON/CAS akisina bagli; bunu
  `52518cb` ve sonraki native batch commitleriyle dogruladim. Kalan kapilar
  kaynak degil, kurulu OpenCode/native host, verifier bagimsizligi ve final
  kabul kanitidir; testler CALISTIRILMADI.

- 17 Ekim 2026 tek final test kapisi: `python -m pytest -q` exit code `1`,
  `7831 passed, 1409 skipped, 149 failed, 3 warnings`, sure `1:11:38`.
  Kirmizi kapsamlar eski BGE/remote sozlesmeleri, schema/projection driftleri
  ve makineye bagli SQLite/skill/unit-test loop fixture'larinda toplandi.
  Yeni pytest cagrisi yapilmayacak; E5 model smoke/kurulum kaniti bu genel suite
  sonucundan ayri tutuluyor.

- 16 Ekim 2026 embedding erteleme karari: Mevcut embedding provider/modeli
  kullanilamaz durumda oldugu icin WP-05.03, WP-05.04 ve WP-05.06'nin provider
  cagri, qualification, gercek vector publication ve performans adimlari
  kullanici yeni yerel modeli hazir oldugunu bildirene kadar calistirilmayacak.
  Bu alanlar gelistirme veya kabul basarisi sayilmayacak; embedding disi kaynak
  ve control katmanlari bagimsiz ilerleyebilir. Testler final toplu asamada.

- 16 Ekim 2026 WP-08.01 child-dispatch capability kapisi: Canonical child
  assignment dispatch'i adapter'in explicit `child-dispatch` capability
  beyanini effect oncesinde zorunlu kilar. Structured-result veya OpenCode
  named-agent benzeri baska bir capability tek basina child runtime kaniti
  sayilmaz; eksik beyan assignment/invocation persistence'tan once reddedilir.
  Regression kaynagi guncellendi, testler CALISTIRILMADI; gercek native child
  host capability ve kabul kaniti acik.

- 16 Ekim 2026 WP-08.04 provider kimligi kapisi: Model gateway invoke, manifest
  provider_ref degerini gercek ProviderCall provider_ref ile effect ve attempt
  kaydindan once eslestirir. Prefix drift ledger veya provider effect'e
  ilerleyemez. Regression kaynagi guncellendi, testler CALISTIRILMADI; catalog
  revision/model config ve gercek provider kabul kaniti acik.

- 16 Ekim 2026 WP-11.03 backup manifest kapisi: ArtifactEntry digest ve boyut
  alanlari kurulusta dogrulanir; BackupManifest schema, timezone, configuration
  digesti ve tekil artifact kimliklerini fail-closed kontrol eder. Bozuk
  manifest restore/verify akisina gecemez. Regression kaynagi guncellendi,
  testler CALISTIRILMADI; online snapshot, ownership ve gercek restore kabul
  matrisi acik.

- 16 Ekim 2026 WP-07.01..04 lease readback: Canonical bound-job ozetleri owner,
  token ve fencing degerlerini disari cikarmadan lease varlik durumunu tasiyor.
  Running job'da lease yoklugu ve terminal/recovery job'da lease kalintisi resume
  execution guard tarafinda recovery-required olarak durduruluyor; eski adapter'larin
  additive alani vermedigi durumda mevcut bounded guard korunuyor. Testler
  CALISTIRILMADI; grant yeniden edinimi ve gercek race/crash kabulu final kapida.

- Gorev 4 WP-04.01/06.03: Code graph build plani bounded metadata envanterini
  (yol, tur, boyut ve eligibility durumu) kaynak dosya manifestinden ayri
  tasir. Denied/generated/linkli/okunamayan girdiler content okumadan planda
  gorunur; metadata digest'i plan/source manifestine baglanir ve apply yeniden
  okuyup drift varsa fail-closed durur. CLI plan/build ciktisi envanter digest
  ve sayisini tasir. Testler CALISTIRILMADI; graph metadata, parser/resolver ve
  native kabul matrisi acik.

- Gorev 4 WP-04.01/05: `DiscoveryReport` bounded metadata envanteri icin
  canonical `inventory_digest` uretir; project knowledge index plani ve JSON
  cikti kimligine bu digest baglanir. Icerik tree digest'i ayni kalsa da
  ignored/generated/binary/eligibility metadata drift'i stale plan olarak
  gorunur. Testler CALISTIRILMADI; parser/secret, incremental publication ve
  native kabul matrisi acik.

- Gorev 4 WP-05.03: Eski `apply_project_index` yolu da count/token/payload
  sinirli deterministik embedding batch planina alindi. Provider cagrisi oncesi
  cancel/deadline kontrolu ve cagridan sonra tekrar kontrol eklendi; limit veya
  kismi sonuc CAS/veritabani yayinindan once durur. Provider profile kimligi ve
  receipt dogrulamasi korunur. Testler CALISTIRILMADI; quota/backoff ve canli
  provider kabul matrisi acik.

- Gorev 4 WP-05.03: Legacy project embedding apply provider cagrilarinda
  yalniz transient-unavailable durumlar icin 4 denemeli bounded backoff,
  cancellation/deadline kesmesi ve toplam provider-call kotasi eklendi.
  Outcome-unknown/deadline/kalici HTTP hatasi tekrar edilmiyor; receipt/profile
  dogrulamasi ve CAS oncesi atomiclik korunuyor. Testler CALISTIRILMADI;
  canli provider ve performans kabul matrisi acik.

- Gorev 4 WP-05.03: Legacy embedding apply quota asimini mevcut typed
  `ProviderCallQuotaExceeded` sinifina bagladi; generic policy mesaji yerine
  canonical quota sinyali korunuyor. Testler CALISTIRILMADI.

- Gorev 4 WP-05.04: Yeniden gruplanan embedding girdileri icin input-level
  claim/receipt eslesmesi append-only `provider_document_input_reconciliation`
  ledger tablosuna baglandi. Requested input sirasi, parent effect, slot,
  manifest, claim, receipt ve vector evidence digest'i her recovery'de yeniden
  okunuyor; duplicate veya drift fail-closed kaliyor. Testler CALISTIRILMADI;
  provider quota, legacy adoption ve gercek performance kabul kapilari acik.

- Gorev 4 WP-05.04: Project source identity artik bounded discovery
  `inventory_digest` alanini da tasiyor. Manifest ve normalized document
  parser profile ayni metadata kimligiyle baglandi; ignored/generated veya
  eligibility metadata degisimi yalniz content tree ayni kalsa bile stale plan
  olarak gorunur. Testler CALISTIRILMADI; incremental publication kabul kapisi
  acik.

- Gorev 4 WP-05.04: Birlesik Project RAG revision/tree ve combined manifest
  repository `inventory_digest` alanini de tasir. Ignored/generated/eligibility
  metadata degisimi repository content tree ayni kalsa bile generation scope ve
  canonical manifest digestini degistirir; eski generation current kalmaz.
  Testler CALISTIRILMADI.

- Gorev 4 WP-05.04: Project RAG plan ve index sonucu readback ciktilari
  repository `inventory_digest` alanini acikca tasiyor. Generation scope ve
  manifest baginin raporlanan metadata kimligiyle denetlenebilirligi saglandi.
  Testler CALISTIRILMADI.

- Gorev 4 WP-06.02: Oracle metadata indeksinde `EXECUTE IMMEDIATE` ve `DBMS_SQL.PARSE`
  dinamik SQL hedefleri `unresolved_relations` olarak guvenli nesne kimligi ve belirsizlik
  turu ile kaydedildi; ham SQL, bind degeri ve tahmini lineage kenari uretilmedi. Kaynak
  degisikligi tamamlandi, testler CALISTIRILMADI; Oracle/native kabul kapisi acik.

- Gorev 2 WP-07.01..04: `work resume` canonical SQLite Work eventlerini, son Run ozetlerini ve bounded bound job referanslarini acik Work penceresine baglar. Recent activity zaman sirali ve bounded; payload/owner token/grant disarida kalir. CLI regresyon kaynagi hazir, CALISTIRILMADI. Lease/grant yeniden edinimi ve WP kabul kapilari acik.

- Gorev 2 WP-07.01..04: Operational checkpoint payload dogrulamasi canonical JSON boyutunu 16 KiB, derinligi ve alan sayisini sinirlar; secret/credential/token/prompt/response/transcript/raw-content/private-key alan veya degerlerini ve `grants_authority=true` durumunu reddeder. Unit regresyon kaynagi hazir, CALISTIRILMADI. Lease/grant yeniden edinimi ve WP kabul kapilari acik.

- Gorev 2 WP-07.02/04: Bound job resume readback'i source snapshot ID varken revision/tree/content/config digestlerini canonical SQLite join ile tasir; eksik snapshot readback'i fail-closed reddeder. Unit regresyon kaynagi hazir, CALISTIRILMADI. Lease/grant yeniden edinimi ve WP kabul kapilari acik.

- Gorev 4 WP-04/WP-06.01: ODI preflight policy ile reddedilen topology/sensitive/secret dosyalarin raw byte hashini public plan files alanina tasimaz; content_digest null kalir. Kabul edilen dosya fingerprint ve rejected planin bind reddi korunur. CLI regresyon kaynagi hazir, CALISTIRILMADI.

- Gorev 4 WP-04/WP-06.01: ODI directory discovery limiti queue boyutu yerine toplam kesfedilen file/directory sayisina baglandi; islenmis bos dizinler artik butceden dusmez. Deep empty chain, mixed entry ve exact limit regresyon kaynaklari hazir, CALISTIRILMADI. Native/WP kabul acik.

- Gorev 4 WP-06.01: Smart XML tek-kayit indekslerinde ayni sinif/kimlige ait celisen allowlist kayitlari lineage oncesinde reddedilir; celisen tekrar Field de reddedilir. Ozdes kayit ve composite relation parent tekrarlarini engellemez. Chunk profile sanitizer v2 ile eslendi. Regresyon kaynaklari hazir, CALISTIRILMADI; native/WP kabulu acik.

- Gorev 4 WP-04/WP-06.01: Smart XML DTD/entity guard, XML veri cikarimi ve SHA256 ayni bounded descriptor akisina baglandi. 128 MiB byte, 1 milyon element, 256 derinlik ve 200 bin object siniri; biten node detach ve kaynak identity/size/mtime readback eklendi. Sanitizer v2/xml_profile plan ve manifestte; yalniz Field allowlist korunur, final kaynak hash tekrar okunur. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek ODI/native kabul ve WP kapanisi acik. Ayrinti docs/ODI_SMART_XML_AKISI.md.

- Gorev 2/4 WP-06.01/WP-07.02/04/05: Smart Export canonical import/index source bagi eklendi: plan v2 exact original source identity, source snapshot contract ve destination library ref; typed admitted Work/Run/plan olmadan copy yok. Reserved project.odi-smart-import common claim/project lease/CAS/checkpoint/receipt ve pre-post source/private binding readback kullanir. Private bounded copy/fsync/hash/exclusive hard-link existing farkli/public raw dosyayi overwrite/adopt etmez; own temp identity ile cleanup yapar. Private binding history korunur, raw XML/absolute path CAS sonucuna girmez. Index load_smart_binding actual completed receipt/current mapping/source hash/private ownership ister; legacy/unbound veya missing receipt reddedilir, sanitizer/disclosure kapilari korunur. Completed replay original source yokken yeniden copy/parse/write yapmaz; unknown explicit recovery ister. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek native ODI/Work ve WP kabul acik. Ayrinti docs/ODI_SMART_CANONICAL_IMPORT.md.

- Gorev 2/4 WP-06.01/WP-07.02/04/05: ODI directory binding canonical Work/Run kaynak bagi eklendi: preflight plan v2 source snapshot contract manifest/tree + physical root digestini baglar. Typed admitted Run/Work revision/project/plan/snapshot olmadan yazma yok. Ortak project.odi-bind claim/project lease/CAS/checkpoint/receipt, source/destination pre-post readback ve no-write completed replay kullanir. Mevcut private binding exact bytes content-addressed private history olarak korunur; legacy receipt adoption yok. Salt okunur odi-binding-source current vs historical mappingi ayirir, absolute root/raw XML CASa alinmaz. Silent recovery rewrite kaldirildi; unknown explicit recovery ister. SQLite/CAS/XML/private file/CLI regresyon kaynaklari guncellendi, CALISTIRILMADI. Smart Export import/sanitized index canonical bagi ve gercek kabul sonraki dilimdir. Ayrinti docs/ODI_CANONICAL_BINDING.md.

- Gorev 4 WP-04.03/05: Belge provider locator sozlesmesi sikilastirildi: OCR int/float/str ve structured text str donusumlerinin sahte citation/metin uretme yolu kaldirildi. Ortak Locator page/line/block/timestamp gercek int, bbox finite numeric ve ContentUnit text/order/confidence turleri dogrulanir. OCR threshold oncesi tam validation; structured exact DOCX/PDF enum, heading bounds ve PDF bbox korunumu eklendi. 16384 blok/16 MiB normalize cikti butcesi filtered bloklari da kapsar, cap+1 partial kabul edilmez. Generic parser surumu 2; valid serialized body bicimi degismedi. Regresyon kaynaklari hazir, CALISTIRILMADI. WP/native kaynak/secret/provenance kabul kapilari acik. Ayrinti docs/BELGE_PROVIDER_LOCATOR_SOZLESMESI.md.

- Gorev 3/5 WP-02.03/WP-08.03/WP-09.05: Native publication host composition eklendi: actual evidence admission receipt -> tam plan/project/private root -> aktif ledger + exact transport + existing GitPublicationService. apply-admission preview approval istemez/uretmez; apply exact plan digest ve mevcut trusted authorization ister. Current admission/producer/review/native test source/raw report validator dispatch/claim, consumption sonrasi ve push sonrasi terminal oncesinde tekrar okunur; ignored tree/report drift recovery ister. Completed replay yeni Git/test/transport cagrisi yapmaz. Yeni apply yuzeyleri explicit LOCAL_EFFECT, preview read-only registry yolundadir. Canonical runtime/source fixture regresyon kaynaklari hazir, CALISTIRILMADI. Host key provisioning/principal/credential isolation ve gercek native cihaz kabulu halen acik. Ayrinti docs/PUBLICATION_NATIVE_HOST.md.

- Gorev 3/5 WP-02.03/WP-08.03/WP-09.05: Publication test/verifier evidence bundle kaynak bagi eklendi: actual producer receipt -> ayni subject/binding/realm/native test job/receipt/Run -> tam evidence-bound plan. publication.evidence.bind claim ve project+global trust lease, current source/raw report/trust pre/post readback ile iki signed evidence kaydini tek SQLite transactioninda kabul eder; ikinci kayit hatasi ilk inserti rollback eder. Immutable canonical source scope/receipt/CAS, completed replay ve salt okunur evidence-admission reader baglandi. Gercek SQLite/CAS protokol regresyon kaynaklari hazir, CALISTIRILMADI; upstream producer fixture kabul sayilmaz. Approval/push, host signer confinement/provisioning ve native cihaz kapilari acik. Ayrinti docs/PUBLICATION_EVIDENCE_BUNDLE.md.

- Gorev 3/5 WP-02.03/WP-08.03/WP-09.02/05: Trusted verification evidence producer kaynak bagi eklendi: actual completed publication.review accepted native session/upstream/commit scope -> current native test source/ham rapor/subject -> ayri verification-role host signer. publication.verification.attest claim-before-sign, project+global trust lease, admitted key signature ve source/trust post-effect readback; unknown recovery ve no-sign completed replay baglandi. verification-evidence salt okunur export ve importta actual producer receipt exact readback eklendi. Real SQLite/CAS/Ed25519 protokol regresyon kaynaklari hazir, CALISTIRILMADI; upstream review/current source fixture kabul sayilmaz. Host signer confinement/provisioning, native composition ve gercek cihaz/quality kabulu halen acik. Ayrinti docs/PUBLICATION_VERIFIER_PRODUCER.md.

- Gorev 3/5 WP-02.03/WP-08.03/WP-09.03: Commit araligi unit-test scope bagi eklendi: historical source commit current HEAD atasi olmali; tum net Git farki yalniz accepted test A/M dosyalaridir. Baska dosya/silme/chmod/link/submodule/binary/invalid UTF-8 reddedilir. Bounded shell-free Git raw diff ve before/after blob OID/metni context ve plan digestine baglandi; immutable reader Git blob hashlerini tekrar hesaplar. Replace-object/inherited GIT override/external diff/textconv kullanilmaz; toplam Git okuma 30 sn, proof 150000 byte/512 dosya ve toplam context kapisi korunur. Yerel Git regresyon kaynaklari hazir, CALISTIRILMADI. Imzali verifier producer/native host confinement acik; WP kapanisi yok. Ayrinti docs/PUBLICATION_COMMIT_SONRASI_REVIEW.md.

- Gorev 3/5 WP-02.03/WP-08.03/WP-09.02/05: Commit-sonrasi publication.review kaynak bagi eklendi: historical builder full tree/production/build/plan ile current native test subject eslesir; explicit reviewed profile tum builder upstreamlerinden farkli reviewer ister. Secili production ve kabul edilen test icerigi bounded secret-safe context ile exact budget/plan digestine baglanir. Ayri admitted Run, claim-before-model, native session ayrimi ve post-effect current source readback canonical receipt yolundadir. Salt okunur review-plan/review-source CLI eklendi; model veya signer cagrisi yapilmadi. Regresyon kaynaklari hazir, CALISTIRILMADI. Bu secili unit-change review scope'udur; quality/signing/publication authority degildir. Trusted verifier producer ve host signer confinement/native composition acik. Ayrinti docs/PUBLICATION_COMMIT_SONRASI_REVIEW.md.

- Gorev 3/5 WP-02.03/WP-09.03/05: Olculen full test-tree source bagi eklendi: actual Maven reviewed reactor src/test before/after fingerprint ve stable/drifted/unavailable projection; ignored resources kapsanir. Accepted/no-tests olcumde drift stale sayilir; failed/timeout sinifi korunur. Loop current tree portu verifier oncesi/sonrasi ve success oncesi reread yapar; missing port/okuma/drift success'i engeller, external dosya silinmez. Native ve batch CAS/registry/receipt refs ile source/verifier okuyucularina baglandi; eski ref yoksa rapor digest'i degismez. 5000 dosya/64 MiB aggregate/4 MiB tek dosya ve 128 modul limitleri; link/junction ve enumeration error silent omit yerine ret. Regresyon kaynaklari hazir, CALISTIRILMADI. Atomic filesystem veya quality/publication authority iddiasi yok; current HEAD/subject, trusted verifier producer ve host confinement/native composition kapilari acik. Ayrinti docs/OLCULEN_TAM_TEST_AGACI.md.

- Gorev 3/5 WP-02.03/WP-08.03/WP-09.02/05: Canonical verifier-source okuyucusu eklendi: completed test.run input/binding/checkpoint/receipt -> gercek ledger final terminal/unique accepted attempt -> coverage re-evaluation -> actual CAS measurement/binding/source snapshot -> accepted builders ve final verifier context/model/profile/patch zinciri denetlenir. Loop measurement record/binding/report refs CAS'a, measurement/terminal/candidate/pre-postimage artifact registry'ye baglandi; existing input digest govdesi ayni algoritmayla outputta saklanir. Bounded no-scan reader legacy/missing/drift kaniti benimsemez; replay/model unknown bagimsiz kaliteye yukselmez. Regresyon kaynaklari hazir, CALISTIRILMADI. Tarihsel canonical anchor current HEAD/publication subject veya independent quality authority degildir; commit-sonrasi review, trusted verifier producer ve host signer/native composition kapilari acik. Ayrinti docs/CANONICAL_VERIFIER_KAYNAGI.md.

- Gorev 3/5 WP-02.03/WP-08.03/WP-09.02: Verifier execution provenance kaynak bagi eklendi: canonical gateway gercek assignment/invocation/dispatch/artifact/context/instruction ve host configured model/upstream/profile baglarini direct CAS execution kaydinda tutar. Dependency artifact registry composition ve accepted builder list/final verifier refs terminal state/evidence'a tasindi; legacy completeness false kalir. Bounded no-scan reader deterministic request identity/digest/schema/role/context kontratini; comparator exact candidate/measurement, tum builder scope, distinct native execution ve configured upstream ayrimini denetler. Replay/model alias farki native/bagimsiz kalite kaniti sayilmaz; canonical terminal/quality authority false. Regresyon kaynaklari hazir, CALISTIRILMADI. Siradaki katman completed test.run/terminal/attempt -> publication candidate/subject -> trusted verifier producer. Host signer confinement/native composition kabul kapilari acik. Ayrinti docs/TEST_AGENT_EXECUTION_KANITI.md.

- Gorev 3/5 WP-02.03/WP-09.05: Host-owned test evidence producer kaynak bagi eklendi: typed signer capability, actual canonical measurement/current publication source, active role/realm/config/pin ve publication.test.attest claim-before-sign; project+global trust lease, exact public-key signature, source proof/CAS/checkpoint/receipt ve claim time readback. Completed replay yeniden imzalamaz; drift/unknown effect recovery ister. Salt okunur test-evidence export ve importta actual producer receipt yeniden okuma baglandi; CLI private key veya generic sign-JSON sunmaz. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek host key confinement/provisioning/native worker composition ve independent verifier producer halen acik; yayin/quality authority veya WP kapanisi uretilmedi. Ayrinti docs/PUBLICATION_TEST_PRODUCER.md.

- Gorev 3/5 WP-02.03/WP-09.05: Publication test-source current kaynak okuyucusu: exact canonical native receipt/measurement ve yeni report-facts hedef modul sirasi; ayni project/Work/source snapshot/config; current Work/dependency readback ve private project root, temiz exact Git identity/HEAD/remote, Maven plan/config/launcher, production/test tree/class ve ham Surefire/JaCoCo/exec hash+parser readback baglandi. Pre/post reread partial basariyi reddeder; raw bytes saklanmaz. JVM reprobe/HTML/independent verifier/signing authority iddiasi yoktur. Pending subject preview typed plan/evidence_bound=false tasir; mevcut evidence/approval kapisi korunur. Regresyon kaynaklari hazir, CALISTIRILMADI. Ayrinti docs/PUBLICATION_TEST_KAYNAK_DOGRULAMASI.md. Siradaki signing producer ve gercek bagimsiz verifier execution baglari acik.

- Gorev 3/5 WP-02.03/WP-09.05: Native Maven rapor projection gelistirmesi: mevcut Surefire/JaCoCo parser sonuclari tekrar parse edilmeden sayisal ve secret/PII kontrollu report-facts CAS artifactine baglandi. Ham properties/testcase/output/session metni saklanmaz. Native attempt receipt projection digestini tasir; source-evidence module/toplam sayac, measurement/binding ve JaCoCo XML/exec bagini yeniden dogrular; legacy not-captured, red capture-rejected, missing captured CAS ise ret olur. Gercek SQLite/CAS ve parser fixture regresyon kaynaklari hazir, CALISTIRILMADI. Bu semantic projection ham rapor arsivi, bagimsiz quality veya publication authority degildir; exact current source/HEAD, trusted signing ve verifier producer baglari acik. Ayrinti docs/NATIVE_TEST_KANIT_KAYNAGI.md.

- Gorev 3/5 WP-02.03/WP-09.05: Native test source-evidence okuyucusu canonical job/checkpoint/CAS -> request/attempt/receipt -> measurement/binding/candidate tree baglarini salt okunur dogrular. Nondefault execution key, failed sonuc, bounded strict JSON ve kolon/body drift regresyon kaynaklari eklendi; CALISTIRILMADI. Tarihsel freshness guncel kaynak veya ham rapor/quality kaniti degildir; signing/publication authority false kalir. Ayrinti docs/NATIVE_TEST_KANIT_KAYNAGI.md. Immutable raw report, exact publication subject, independent verifier ve signer ownership baglari acik.

- Gorev 3 WP-02.03 public registry rotation: onceki full canonical receipt lineage/head, old approval signature/window ve yeni admitted config pin exact Work/claim'e baglandi. Global registry lease, bounded terminal census, no reset/fork/unknown adoption, private staging/fsync/atomic replace ve post-effect readback/complete replay eklendi. Strict public CLI JSON, trust-source/rotation-plan/rotate yuzeyleri ve gercek SQLite/CAS regresyon kaynaklari hazir, CALISTIRILMADI. Signing key provisioning/ownership, actual test/verifier producer ve native enforcement kabulleri acik; docs/PUBLIC_REGISTRY_ROTASYONU.md.

- Gorev 3 enrollment post-effect admission duzeltmesi: ortak canonical runtime callback'i yazma sonrasinda tekrar cagirdigi icin target-absent kontrolu kendi tamamlanan yazmasini reddediyordu. Process-local own-write phase ile once absent, sonra exact fsync/readback icerigi dogrulanir; current config/pin/callback ve foreign-existing reddi korunur. Pre/post ve post-effect drift regresyon kaynaklari hazir, CALISTIRILMADI.

- Gorev 3 WP-02.03 ilk public registry enrollment: ortak strict public schema, existing settings/active config pin, exact Work/Run/plan ve reserved publication.trust.enroll claim/checkpoint/receipt CLI'a baglandi. Exclusive private yeni dosya, fsync/readback, no-adopt/no-overwrite/no-silent-rotation ve completed replay missing/drift reddi eklendi. Gercek SQLite/CAS regresyon kaynaklari hazir, CALISTIRILMADI; kullanici home/anahtar degisikligi yok. Signing key ownership/rotation, actual test/verifier producer ve native enforcement kabulleri acik.

- Gorev 3 WP-02.03 publication config: optional strict public trust_digest normal Settings/sanitized active revision/provenance yoluna baglandi; CLI loaded pin/active registry eslesmesi ister. Empty/null/extra/dotted config ve session pin/CLI preference bypass reddi eklendi; unconfigured varsayilan degismez, key/registry uretilmez. Actual Settings->SQLite->trust/CLI regresyon kaynaklari hazir, CALISTIRILMADI. Producer enrollment/receipt ve native enforcement kabulleri acik.

- Gorev 4 WP-06.05 history scope: root commit read-only diff-tree --root ile actual additions'a baglandi; hardcoded empty-tree yok, SHA-256 kimlik destegi var. First-parent basis, subject truncation/match, binary unavailable ve omitted/redacted/truncated partial propagation eklendi. Offline gercek Git/regresyon kaynaklari hazir, CALISTIRILMADI; native/issue/full WP kabul acik. docs/ISSUE_GIT_GECMISI_KAPSAMI.md.

- Gorev 6 WP-12.06 source traceability: explicit current WP input scope REQ/CAP -> WP ve declared test path -> tracked hash/AST declaration bagi eklendi. Exact authority satiri, missing/unmapped kaynaklar ve null run/evidence/execution commit korunur; free prose/historical passed kabul olmaz. Canonical catalog + traceability.json + manifest/ZIP/readback ayni graph'i tasir. Regresyon kaynaklari hazir, test/generator/verify CALISTIRILMADI; gercek release chain/receipt/native/independent review kapilari acik.

- Gorev 6 WP-12.04 disk readback: export writer captured canonical bytes'e karsi persisted dosyalari yeniden okur; --verify guncel source catalog'dan exact dosya/hash/ZIP eslestirir. Imported manifest path yetkisi vermez; bounded missing/extra/link/tamper ve stale source reddi eklendi, unzip yoktur. Readback runtime/acceptance authority degildir. Regresyon kaynaklari hazir, test/generator/verify CALISTIRILMADI; genel WP kabul acik.

- Gorev 6 WP-12.04/05 kaynak mimarisi: bounded tracked source catalog, current REQ/CAP/WP/D kayitlari, AST interface/literal table/test declarations, wheel exclusion ve source hashes tek girdiye baglandi. Historical appendix authority olmaz; source/HEAD readback, no-import ve new external target siniri korunur. Yerel Markdown/escaped HTML/Mermaid/manifest/deterministic ZIP ureticisi ve regresyon kaynaklari eklendi. Test/generator CALISTIRILMADI; runtime/full/native/release kabul acik. docs/KANONIK_MIMARI_EXPORT.md.

- Gorev 6 WP-11.01 trial usage: optional salt okunur native-usage raporu exact source receipts/assignment/artifact chain ve local grade'den uretilir. Frozen aggregate/digest degismez; selected trial scope/model/baseline/candidate coverage, partial null totals ve separate estimated cost eklendi. Yeni call/yazma sifir; full account/billing kaniti degildir. Regresyon kaynaklari hazir, CALISTIRILMADI; docs/OPENCODE_NATIVE_USAGE.md.

- Gorev 5 WP-09/10 native usage: desteklenen exact OpenCode v2.0.29 schema kaynagindan step_finish token component ve host USD estimate optional CAS artifact'e baglandi. Legacy/partial null, native skill iki-step coverage ve total/identity readback eklendi; strict artifact consumers guncellendi. Tournament historical/new selector ayri kapsamda raporlar. Provider requests/billed cost bilinmez, hard-cap/gercek native kabul acik. Regresyon kaynaklari hazir, CALISTIRILMADI; docs/OPENCODE_NATIVE_USAGE.md.

- Gorev 2 WP-07.04 resume Run fingerprint readback: bounded Run ozetleri canonical source binding/revision/tree/content/config ve config revision digestlerini ayni SQLite joininden tasir. Eksik veya source-config uyumsuz bag `complete=false`, `consistent=false`, `revalidation_required=true` olarak fail-closed gorunur; yeni lease/grant/efekt uretilmez. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2 WP-07.04 ortak Run projection: `work resume`, `work show`, dogal dil Work status ve `workspace_resume` ayni bounded `run_document` yardimcisini kullanir; fingerprint alanlari yuzeyler arasinda kaybolmaz. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2 WP-07.04 natural exact show Run readback: Dogal dil Work `goster` yolu bounded son Run ve source/config fingerprint projection'ini exact-work scope'unda tasir; checkpoint yolu ayni Run baglarini korur. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 3 WP-02.01 inert provenance: Zekam disi proje-yerel ince giris plani `inert_reason=workspace-marker-missing-or-invalid` ile plan/effect/receipt readback'inde kapsam disi kalma nedenini tasir; dosya hedefi ve yazim yine bos kalir. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 4 WP-04.06 source identity: Proje indeks plan/manifest/parser profile `project_id + logical_source + source_revision + tree_digest` digestini ayri identity olarak tasir; content digest ile karistirilmaz ve plan digestine baglanir. Ayni icerigin farkli projelerde eslesmesi veya slug degisimi tek source kimligi sayilmaz. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 4 WP-04.06 source identity apply gate: Index apply, manifest v2 ve normalized parser profile kimligini planin project/revision/tree identity'siyle tekrar eslestirir; reassociation drift fail-closed kalir. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2/4 WP-07.02 source identity plan bagi: Canonical `project.index` Run plan ve RAG plan projection ayni `project_id + source_revision + tree_digest` source identity digestini tasir; Work/Run binding'de cross-project veya eski source reassociation gorunur kalir. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2/4 WP-07.02 research source identity plan bagi: Research run plan ve effect claim ayni `project_id + source_revision + tree_digest` source identity digestini tasir; execution oncesi canli RAG tree/source identity drift'i fail-closed reddedilir. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2/4 WP-07.02 research source readback: Research report/status ve read-only receipt yuzeyleri source revision/tree/source identity/generation bagini kaybetmeden legacy payloadlarda bounded uyumlulugu korur. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 4 WP-05.04 source identity recovery: RAG state/pending CAS publish ve crash recovery, generation source revision/tree ile source identity digestini birlikte dogrular; query/status readback identityyi yeniden uretir. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2/4 WP-07.02 research eski plan kapisi: Source identity alanlari olmayan legacy research planlari effect/claim baslamadan bounded policy hatasiyla reddedilir; KeyError veya sessiz fallback yoktur. Regresyon kaynagi hazir, CALISTIRILMADI.

- Gorev 4 WP-05.04 RAG state self-consistency: Mevcut ve pending recovery state'lerinde source binding alanlari varsa `project_id + source_revision + tree_digest` ile yeniden dogrulanir; legacy state alanlari yoksa bounded uyumluluk korunur. Publish/recovery state drift'i fail-closed kalir. Regresyon kaynaklari hazir, CALISTIRILMADI.

- Gorev 2 WP-07.05 native source root: Checkpoint/resume source readback'inde Git kokune giden symlink veya Windows reparse ancestor kabul edilmez; exact fiziksel root siniri `source-root-link-or-reparse` ile fail-closed kalir. Regresyon kaynaklari hazir, CALISTIRILMADI.

- Gorev 2/4 WP-07.02/04 resume RAG readback: `workspace_resume` RAG ozeti state'te source binding varsa revision/tree/source identity alanlarini yeniden dogrulayip ayni packet'e tasir; bozuk veya eksik yeni binding attention-required olur, eski alanlari olmayan state bounded uyumlulukla kalir. Bozuk digest de packet'i dusurmeden ayni attention sinifina indirilir. Regresyon kaynaklari hazir, CALISTIRILMADI.

- Gorev 4 WP-05 batch recovery ambiguity: Completed document input regrouping artik bounded input-level claim adaylarini toplar; ayni digest'in duplicate slotlarini parent kapasitesiyle eslestirir, birden fazla parent secenegini veya manifest/vector slot driftini fail-closed reddeder. Regresyon kaynaklari hazir, CALISTIRILMADI.

- Gorev 4 WP-05.04 RAG staged recovery: Disk state staged generation'i gosteriyorsa source revision/tree/source identity pending result ile exact eslesmelidir; self-consistent fakat baska generation state'i recovery'de kabul edilmez. Regresyon kaynaklari hazir, CALISTIRILMADI.

- Gorev 4 WP-05.04 RAG status/query readback: State source binding varsa canonical SQLite generation revision/tree/identity ile exact karsilastirilir; status ve query generation drift'inde fail-closed kalir. Legacy binding alani olmayan state bounded compatibility yolunda kalir. Regresyon kaynaklari hazir, CALISTIRILMADI.

- 16 Ekim 2026 kullanici yeniden devam izni verdi; 07:40 conversational duraklama kaldirildi. Resume read ZEKAM_HOME hazir degil hatasi verdi; kanonik Work/lease durumu uydurulmadi. Hedef araci paused gorunuyor ve mevcut API resume desteklemiyor; yeni hedef uretilmedi.

- Gorev 5 WP-10.05 canonical tournament: actual paired trial -> fresh local grade -> no-progress stop veya tek native verifier-role selector reserved Work/claim/checkpoint/CAS receipt akisina baglandi. Exact source/config/model/session, typed karar, completed replay ve historical report eklendi; caller scores kabul edilmez. Unmetered token/cost/provider-request boyutlari null ve explicit opt-in; promotion false. Unit regresyon kaynaklari hazir, CALISTIRILMADI. Gercek host/full quality ve quantitative metering kabul kapilari acik; docs/TOURNAMENT_CANONICAL_SELECTION.md.

- 16 Ekim 2026 07:40 checkpoint talimati: sekiz yeni kaynak dilimi origin/main'e push edildi, tamaminda testler CALISTIRILMADI. Devir notu docs/CHECKPOINT_2026-10-16_0740.md ve MODEL_DEVIR_METNI.md; mevcut hedef tamamlanmis sayilmaz. 07:40'ta conversational hedef duraklatilir; kullanici devam et diyene kadar yeni gelistirme baslatilmaz.

- Gorev 5/6 WP-10/11 radar connection omru: 37 repository transaction yolu ortak commit/rollback + finally close context'ine baglandi. SQLite context manager'in dosya handle'ini kapatmama davranisina/GC'ye guvenilmez; read-only ve writer basari/hata yollarinda handle birakilir. Retained gercek connection regresyon kaynaklari hazir, CALISTIRILMADI. Schema/migration degismedi; genel kabul acik.

- Gorev 5 WP-10.05 tournament contract: bounded integer budget/usage, finite nonbool score, exact gate/isolation/UUID ve plan digest kontrolleri eklendi. Future completed_at selection'a giremez; same declared model case/#variant bagimsizlik sayilmaz. Negatif regresyon kaynaklari hazir, CALISTIRILMADI. Bu typed contract'tir; actual dispatch/receipt/model bagimsizligi, fixture/baseline/no-progress runner ve WP kabulu acik; docs/RADAR_RUNBOOK.md.

- Gorev 6 WP-11 read-only composition: doctor/cognitive/local-core/scheduler/control-plan/backup source yollarinda key provisioning ve runtime config insert kaldirildi. Mevcut private key create=False ile okunur; missing key rotate edilmez. Paused/disabled tick worker hazirligindan once doner; explicit init/prepare/worker yolu korunur. Unit/CLI regresyon kaynaklari hazir, CALISTIRILMADI. Native/signed restore ve genel WP kabul kapilari acik; docs/SCHEDULER_VE_RAPORLAR.md.

- Gorev 5/6 WP-10.05 radar claim/replay: schema/campaign writer claim sonrasina tasindi; admission reddi store olusturmaz, claim sonrasi init kesintisi recovery-required kalir. Replay missing store'u yeniden kurmaz; exact project/stage/plan/tek claim/receipt/terminal ve campaign readback'i zorunlu. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek campaign/tournament ve genel WP kapilari acik; docs/RADAR_RUNBOOK.md.

- Gorev 5/6 WP-10.05 radar read siniri: status/report/candidates, analyse-plan ve scheduler read-only handler mevcut DB'yi mode=ro/query_only ile acar; constructor schema install/migration etkisi bu yollardan kaldirildi. Version/digest/path drift reddi ve missing store needs-discovery/unknown ayrimi eklendi. Yetkili run writer yolu korunur. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek campaign/tournament ve genel WP kabul kapilari acik; docs/RADAR_RUNBOOK.md.

- Gorev 6 WP-11 native scheduler kabul bagi: raw bootstrap lookup oncesi full control-chain dogrulamasi, exact settlement/admission/grant/supervisor plan/status eslemesi ve son head readback eklendi. Job/receipt/finish/OS zaman sirasi ve iki farkli event record ID zorunlu; accepted evidence exact bootstrap/head digest'lerini tasir. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek native Windows kabul veya WP kapanisi uretilmedi; docs/OTONOM_EVOLUTION_RUNBOOK.md.

- Gorev 6 WP-11 kullanim raporu: eksik operational/evaluation olcumleri sifir yerine unknown/null; recorded terminal scope ve budget availability eklendi. Bounded 10.000 terminal census/model totals ayni read snapshot'inda eslesir. Son 100 evaluation ornegi tum ledger geneli toplam diye sunulmaz; strict bounded body/digest, metrik tipleri ve coverage metadata baglandi. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek scheduler/kabul ve diger WP kapilari acik; docs/SCHEDULER_VE_RAPORLAR.md.

- Gorev 5/6 WP-10/11 evolution control: status ve append admission ortak bounded full-chain JSON/hash/column/ordinal/predecessor dogrulamasina baglandi. Resume/enable historical evidence butunlugu kontrol edilir; guncel trusted grant verifier korunur. Bozuk/oversize gecmis partial head uretmez; yeni append commit edilmez, runtime degraded/setup durumu running'e donusmez. Regresyon kaynaklari hazir, CALISTIRILMADI. Native kabul, usage olcum dogrulugu ve genel WP kapilari acik; docs/OTONOM_EVOLUTION_RUNBOOK.md.

- Gorev 5 WP-10.03 native outcome recording: actual verifier terminal report/receipt'i ayri reserved recording live claim/fence altinda immutable skill_outcome_v2 satirina baglandi. Legacy operation kapisi korunur; recording receipt verifier yerine gecmez. Unverified journal unique outcome satirini doldurmaz; exact replay/readback kayip row'u yeniden yaratmaz. Regresyon kaynaklari hazir, CALISTIRILMADI. Native kaynak zinciri eklendi; gercek host/full quality/preference/provenance ve genel WP kabul kapilari halen acik; docs/SKILL_NATIVE_OUTCOME_RECORDING.md.

- Gorev 5 WP-10.03 native outcome verifier: actual execution/invocation receipt'inden bounded task/oracle/artifact context'i ayri verifier role/Work job/claim/receipt ve native dispatch'e baglandi. Same model/variant/upstream alias ve reused session reddi, reviewed builder-config pin, typed karar ve source-regrading report eklendi. Configured distinct upstream + consistent local oracle/verdict yalniz tek artifact kapsaminda sonuc verir; unknown upstream/unavailable unverified, promotion false kalir. Regresyon kaynaklari hazir, CALISTIRILMADI. Verifier receipt -> skill_outcome_v2 recording ve gercek host/full quality kabulu acik; docs/SKILL_NATIVE_OUTCOME_VERIFIER.md.

- Gorev 5 WP-10.03 execution -> invocation ledger: actual skill.native.execute terminal receipt/artifact'i scoped plan ve ayri reserved recording live claim altinda ortak atomic invocation writer'a baglandi. Original execution Work/run korunur; baska Work/config/source reassociation ve duplicate native call reddedilir. Historical report source receipt/CAS/row zincirini tekrar okur; deprecation yeni ingestion'a yetki vermez. Regresyon kaynaklari hazir, CALISTIRILMADI. Actual farkli verifier artifact/receipt -> outcome producer ve gercek host/kalite kabulu halen acik; docs/SKILL_EXECUTION_INVOCATION.md.

- Gorev 5 WP-10.03 canonical native execute: aktif package/case/model/provider/runtime/source plan'i execute-native CLI ve reserved skill.native.execute Work job/claim/checkpoint/terminal receipt'ine baglandi. Actual native tool/final output artifact'i local oracle ile gradelenir; execution-report assignment/context/invocation/envelope/CAS zincirini tekrar okuyup yeniden gradeler. Varsayilan paired trial kontrolleri korunur. Regresyon kaynaklari hazir, CALISTIRILMADI. Execution receipt -> SkillInvocationV2 ve ayri native verifier -> outcome producer halen acik; docs/SKILL_CANONICAL_NATIVE_EXECUTION.md.

- Gorev 5 WP-10.03 gercek execution kaynagi: text-only batch'ten ayri explicit native skill profili ve iki-step skill tool/final JSON parser'i eklendi. Canonical paket private neutral runtime'da safe projection ile yuklenir; exact skill action/integration policy allow, diger tool deny korunur. Exact renderer/call/session/message/result bagi content-free CAS projection uretir; skill effect verified sayilmaz. Regresyon kaynaklari hazir, CALISTIRILMADI. Canonical single-case execution producer/CLI ve bagimsiz outcome verifier halen acik; docs/OPENCODE_NATIVE_SKILL_EXECUTION.md.

- Gorev 5 WP-10.03 verifier model on kosulu: gercek V2 tool callback'i model tasimadigi icin exact session.step.started assistant-message/agent/model bagi generated plugin v6'ya eklendi. Bounded content-free map, ambiguous/missing/farkli session reddi ve v2 load model hash'i invocation planina baglandi; v1 unbound metadata unavailable kalir. Regresyon kaynaklari hazir, CALISTIRILMADI. Bu model bagimsizligi veya verified outcome kaniti degildir; gercek artifact/task evidence ve ayri verifier receipt producer acik; docs/OPENCODE_NATIVE_STEP_MODEL.md.

- Gorev 5 WP-10.03 native invocation recording: retained completed skill-load event'i exact aktif managed source/export receipt ve explicit Work run bagiyla reserved canonical recording job'una baglandi. Native instruction-tool completion state zinciri atomic learning transaction'inda unverified olarak kaydedilir; ayri live claim/fence, duplicate call reddi, terminal CAS readback ve historical read eklendi. Bu host attestation veya verified skill effect degildir. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek host, outcome verifier ve passing kalite/provenance/independence kabul kapilari acik; docs/SKILL_NATIVE_INVOCATION_RECORDING.md.

- Gorev 5 WP-10.03 native skill load binding: V2 gercek skill SDK kaydi/renderer sonucu, native call/message hash'leri ve aktif managed package/revision exact terminal export receipt'ine salt-okunur baglandi. V5 plugin enabled workspace'te content-free label uretir; actual V2 event.id normalize edilerek cakisan resource gozlemi duzeltildi. Ownership/policy/root/body/artifact/active-pointer drift kapilari korunur. Regresyon kaynaklari hazir, CALISTIRILMADI. Bu load gozlemidir; canonical SkillInvocationV2 producer ve full verified outcome/host kabul kapilari halen acik; docs/OPENCODE_NATIVE_SKILL_BINDING.md.

- Gorev 5 WP-10.03 native invocation on kosulu: V2 tool.execute.after'in error dalini da tasidigi resmi host sozlesmesinden dogrulandi. Generated plugin v4 actual native status'u korur; missing/future status unknown, raw error/result aktarilmaz. Resume fail/unknown idle/compaction/deleted/checkpoint ile basariya donusmez. Exact v3 ownership digest'i korundu; Bun/native resolver regresyon kaynaklari genisletildi, CALISTIRILMADI. Per-call skill/revision/package producer ve host kabul kapilari acik; docs/OPENCODE_NATIVE_TOOL_OUTCOMES.md.

- Gorev 5 WP-10.03 native lifecycle projection: evaluator/verifier/reviewer canonical source job/receipt'leri exact revision/package/aggregate bagiyla existing SkillEvaluationV2 ve review ledger'ina baglandi. Own Work projection live claim/fence, scope/home/candidate kontrolu ve iki-row atomic learning transaction eklendi; legacy role-operation kontrolleri korunur. Improved bounded olcum tam kalite olmadan insufficient-evidence, review approved=false kalir. Regresyon kaynaklari hazir, CALISTIRILMADI. Tam kalite/provenance/independence, genel passing producer ve native invocation kabul kapilari halen acik; docs/SKILL_NATIVE_LIFECYCLE_PROJECTION.md.

- Gorev 5 WP-10.03 canonical evaluator: aggregate-record/report exact source receipt/case manifest planini reserved skill.evaluation.aggregate Work job/lease/claim/checkpoint/terminal CAS receipt'ine baglar. Bounded public-safe case/oracle snapshot sonucu korunur; report source native artifact zincirini tekrar gradeler ve stored sonucla exact eslesme ister. Failed/regressed/insufficient olcum completed evaluator diye kalite basarisina yukseltilmez. Regresyon kaynaklari hazir, CALISTIRILMADI. SkillEvaluationV2 projection, gercek kalite/provenance/independence ve invocation kabul kapilari acik; docs/SKILL_MEASURED_AGGREGATE.md.

- Gorev 5 WP-10.03 native audit: skill audit-run/report exact aggregate/case/output disclosure planini canonical Work skill.audit.verify/review claim/receipt ve gercek role-bound native dispatch'e baglar. Requested model/variant/upstream alias ve config drift reddi, prior verifier receipt/subject readback, reuse session reddi ve bounded typed decision eklendi. Reviewer prior ret sonucunu gecemez; unsupported preference integrity kabulunu engeller. Regresyon kaynaklari hazir, CALISTIRILMADI. Upstream independence/provenance/human preference gercek kaniti, SkillEvaluationV2 producer ve native invocation kabul kapilari acik; docs/SKILL_NATIVE_AUDIT.md.

- Gorev 5 WP-10.03 trial aggregate: v2 plan/result'a repetition/instruction hash/gercek dispatch envelope eklendi; her tekrar ayri claimed sample. Scoped terminal Work/checkpoint/receipt/CAS ve assignment/invocation/result zinciri yeniden okunup gercek prompt/native artifact local oracle ile tekrar gradelenir. Alias/sample/session dedup, mixed conditions reddi, ayri development/holdout ve equal-case descriptive metrics baglandi. En az bes holdout pair; process/protocol hata ve per-case regression korunur. Regresyon kaynaklari hazir, CALISTIRILMADI. Bagimsiz verifier/reviewer ve SkillEvaluationV2 producer halen acik.

- Gorev 5 WP-10.03 paired trial producer: skill trial-run exact candidate package/case/model/config/root planini canonical Work/Run skill.trial job'una ve gercek OpenCode batch assignment-first dispatch'ine baglar. Iki fresh session baseline/candidate, oracle builder'dan gizli local exact JSON grade ve gercek elapsed/native kimlik raporu eklendi. Native adapter cancel forwarding ve reserved-worker reddi eklendi. Regresyon kaynaklari hazir, CALISTIRILMADI. Aggregate 5 tekrar/holdout provenance, bagimsiz verifier/reviewer ve SkillEvaluationV2 producer baglantisi halen acik; docs/SKILL_PAIRED_TRIALS.md.

- Gorev 5 WP-10.03 receipt consumption: exact bounded evaluation/review input codec, scoped preview/apply ve ayri canonical recording claim/receipt CLI'a baglandi. Existing evaluator/verifier/reviewer terminal kaniti zorunlu; recording receipt execution yerine gecmez. Completed replay salt okunur hash/dokuman esitlemesi yapar, kayip ledger'i yeniden yazmaz. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek skill evaluation/reviewer producer, model bagimsizligi ve native invocation kabul kapilari acik.

- Gorev 4 WP-05/06 reference index: terminal scoped active note snapshot, bounded Markdown chunks ve batch producer proof mevcut project index cache/journal/combined generation yoluna baglandi. Canonical plan pin ve yayin oncesi drift; query/citation current note/file readback ve import source-kind/policy ayrimi eklendi. SQLite/vector/cache/supersession/pin/research negatif regresyon kaynaklari hazir, CALISTIRILMADI. Legacy adoption, gercek client/ACL/performans kabul kapilari acik; docs/CANONICAL_REFERENCE_INDEX.md.

- Gorev 4 WP-06.04 source head: site/issue/attachment origin prefix'i canonical active note query'sine, ayni version timestamp/plan dedup'a ve generated update/archive/supersedes akisina baglandi. Effect oncesi head CAS ve terminal producer/receipt/resolution kapisi eklendi. Historical archive replay current sayilmaz. Regresyon kodu hazir, CALISTIRILMADI. Legacy source adoption, incremental vector/index publication ve canli kabul halen acik.

- Gorev 4 WP-06.04 knowledge publication: Jira mapping -> canonical project scope, fresh source pins ve generated reference, mevcut knowledge.create claim/receipt akisina CLI plan/apply ile baglandi. Exact completed replay note/receipt/resolution readback yapar; ready/attempt=0 enqueue recovery duzeltildi. Yerel SQLite/file/search/replay/drift/recovery regresyon kaynaklari hazir, CALISTIRILMADI. Remote-source head/supersession/incremental index bagi ve gercek client/ACL kabul acik.

- Gorev 4 WP-06.04 ek read: yerlesik MCP'ye exact issue/attachment/metadata digest ile bounded same-origin/path download, once/sonra metadata kontrolu, gercek byte hash, parser locator ve secret/PII kapisi eklendi. TXT/Markdown/DOCX/dijital PDF desteklenir; no-OCR profili ayri, raw bytes saklanmaz. Regresyon kodu hazir, CALISTIRILMADI. Canonical project knowledge publication ve gercek client/ACL kabul acik.

- Gorev 4 WP-06.04 Jira read: exact issue key/site/updated/response provenance, bounded attachment metadata projection ve search offset/size/duplicate/next gate eklendi. Upstream/projection/metadata digest ayridir; attachment bytes veya comments complete iddiasi uretilmez. HTTP strict JSON ve raw cause suppression baglandi. Regresyon kodu hazir, CALISTIRILMADI. Yetkili attachment download -> parser/secret -> canonical project knowledge publication ve canli ACL kabul acik.

- Gorev 5 WP-09 provider profili: strict bounded secret-free endpoint/scope/model alias projection plan/run/job/native kimligine baglandi. Secilen provider/model disinda config aktarilmaz; yerlesik compatible paket ve deny izinleri korunur. Loopback literal IP + private data/state/cache, remote HTTPS + explicit onay/butce; ayni upstream alias reddi eklendi. Profil/parser/local adapter/plan drift regresyon kaynaklari hazir, CALISTIRILMADI. Gercek native/credential provisioning/tool-denial ve bagimsiz verifier kabul kaniti acik.

- Gorev 5 WP-09 modeller: iki explicit builder/verifier model secimi, uc verifying specialty icin ayri adapter, plan digest'inde ortak remote-call butcesi ve canonical job input'ta verifier bagi eklendi. Native model/profile binding context ve assignment/invocation kimliklerine de dahil. Ayni model/variant/provider etiketi ayrimi fail-closed; model planinda bagimsizlik kanitlanmis sayilmaz. CLI/model/specialty/digest/canonical kimlik regresyon kaynaklari hazir, CALISTIRILMADI. Local/custom provider profili ve gercek bagimsiz kabul acik.

- Gorev 5 WP-09 native batch: composition generic wrapper yerine gercek V2 run/standalone/JSON + CAS stdin adapter'ine baglandi. Exact version probe, isolated config/home, project disable, deny permissions/policies, observed session/message ve artifact-only sonuc eklendi. Parser/negative process/CAS/native measurement ayrimi regresyon kaynaklari hazir, CALISTIRILMADI. Local/custom provider profili, farkli verifier modeli ve gercek native tool-denial/end-to-end kabul acik; docs/OPENCODE_NATIVE_BATCH.md.

- Gorev 5 WP-09/08 stdin: optional digest-bound bounded input, concurrent raw-fd writer ve ortak timeout/cancel/tree cleanup eklendi; prompt argv/spec kaydina girmez. Windows argv/pipe siniri, simultaneous output, blocked-reader timeout/interrupt ve pre-start drift/size regresyon kodu hazir, CALISTIRILMADI. Native adapter bu tasimaya baglanacak; gercek kabul halen acik.

- Gorev 5 WP-09/08 executable: arastirma ve test CLI ortak shell-free npm resolver'a baglandi. Kurulu @opencode/cli ile eski opencode-ai ayrimi shim referansindan yapilir; missing/ambiguous shim fail-closed. Regresyon kaynaklari hazir, CALISTIRILMADI. Native run/event/CAS + V2 effect-oncesi tool-denial baglantisi acik; docs/OPENCODE_BINARY_COZUMLEME.md.

- Gorev 5 WP-09/08 process: stream-time output cap, timeout/cancel/Ctrl+C ve owned descendant cleanup; strict client JSON/UTF8 ve observed exit-status baglandi. Yerel child/forged-result regresyon kodu hazir, CALISTIRILMADI. Siradaki exact kaynak acigi: compose_agent_gateway explicit OpenCode batch'i generic digest-flag protokolunden gercek native run/event + CAS prompt adapter'ine baglamak; native Maven measurement bundan ayridir.

- Gorev 6 WP-11 receipt key: signed skill claim/usage bagina public key-binding digest, ayri private key dosyasindan exclusive restore ve journal tree backup eklendi. Key bundle'a konulmaz; eksik/yanlis key yeni rastgele authority ile maskelenmez. Key/HMAC readback ve backup/restore regresyon kodu hazir, CALISTIRILMADI. Legacy signed bundle recovery ve gercek kabul acik.

- Gorev 6 WP-11 cohort: existing operational quiescence, provider unresolved-effect reddi, bounded tum-DB writer fence ve regular file boundary digest baglandi. Manifest/restore versioned cohort ayrimi tasir; eski bundle kaniti yukseltilmez. Lock release/aktif sahip/drift/deadline regresyon kodu hazir, CALISTIRILMADI. Gercek restore/continuity/performance kabul kapilari acik.

- Gorev 6 WP-11.03: local bundle v2 exact owned proje SQLite dosyalarini online backup/DELETE/integrity/FK yoluna alir. V1 read/restore ayrimi, public trust dosyasi ve portable path siniri eklendi. WAL/restore/opaque CAS/v1 regresyon kodu hazir, CALISTIRILMADI. Cross-store snapshot ve gercek restore kabulu acik.

- Gorev 5 WP-10.03 lifecycle CLI: scoped current-head/review plan, strict plan apply, exact skill.activate-v2 claim/receipt, rollback/deprecation ve idempotent replay baglandi. Mevcut export projection kapisi korunur. Plan parity, foreign scope ve real local worker CLI replay regresyon kodu hazir, CALISTIRILMADI. Gercek evaluation/review producer ve native invocation kabulu acik.

- Gorev 5 WP-08 native research: typed host capability/call bridge, logical researcher/verifier role, subject/session/catalog/digest/cancel baglari ve ortak canonical research report/replay eklendi. Usage agent_calls sayar, olculmeyen provider_calls null. Offline Codex/Claude kind fixture ve runtime regresyon kodu hazir, CALISTIRILMADI. Gercek native tool bridge/enforcement ve client kabul kapilari acik.

- Gorev 5 WP-08 research lifecycle: programatik cancel, Ctrl+C/timeout/overflow child tree temizligi ve gec sonuc/materialization iptal kapisi eklendi. Prompt'un okunmayan stdin'e tekrar yazilip bloklanmasi kaldirildi. Yerel Python child regresyon kodu hazir, CALISTIRILMADI. Host-neutral/native child adapter ve gercek kabul halen acik.

- Gorev 4 WP-05 eski ledger: exact ordered inputs/profile ile completed claim/receipt/vector yeniden dogrulanip append-only slot manifest'ine idempotent baglanir. Runtime yalniz missing cache ve unbound completed kayit varsa bounded adaylari dener. Yanlis sira/scope/tamper ve claim/receipt degismezligi regresyon kodu hazir, CALISTIRILMADI. Kayip orijinal girdi veya unknown provider sonucu otomatik cozulmez.

- Gorev 4 WP-05: append-only girdi manifest/slot, atomik regrouped overlap claim kapisi ve completed parent vector/receipt'ten alt kume/sira recovery eklendi. Runtime yeni batch oncesi tekil cache miss recovery yapar; yeni+cokmus cache senaryosu regresyon kodu hazir, CALISTIRILMADI. Eski manifest'siz ledger toparlamasi, provider hesap kotasi ve gercek performance/kabul kaniti acik.

- Gorev 4 WP-06.04: yerlesik Atlassian MCP read transport bounded response/status, Confluence explicit offset/version/site-scoped provenance ve locator baglantisi eklendi. Canli cagri yapilmadi; regresyon kodu hazir, CALISTIRILMADI. Jira attachment/project knowledge ve canli permission kabulleri acik. WP-05 batch regrouping icin belirsiz cagrilarin input-level claim baginin tasarimi halen gerekli.

- Gorev 4 WP-04 kalici discovery: strict kok/frontier snapshot/digest JSON codec ve kayitli proje metadata-only inventory CLI eklendi. Cikti penceresi tarama partial durumundan ayrildi; persisted resume root/drift kontrolunden sonra fresh bounded traversal yapar. Regresyon kodu hazir, CALISTIRILMADI. Parser/secret ve incremental publication kabul baglantilarinin incelemesi devam eder.

- 16 Ekim 2026 RAG freshness envanter bagi: ignored/generated path/type/size drift'i Git status'un atlamamasi icin content-free `metadata_inventory_digest` eklendi. Plan/index/state readback bu digest'i tasiyor; query freshness bounded `read_content=false`, `scan_secrets=false` kesfiyle stale/unknown ayrimini yapiyor. Provider cagri/test CALISTIRILMADI; native kabul ve genel WP kapilari acik.

- 16 Ekim 2026 WP-04.03 polyglot structural parcasi: bounded declaration parser Kotlin/Groovy/Swift/Rust/PHP `class/interface/struct/trait/object/protocol/extension/actor/impl/module/namespace` ve `fun/fn/func/def` govdelerini sembol locator'ina bagliyor. Belirsiz/dengesiz kaynaklar mevcut bounded line fallback'ine donuyor; indexer profile v6'ya alindi. Test CALISTIRILMADI; AST/Tree-sitter kalite ve native kabul kapilari acik.

- 16 Ekim 2026 source binding metadata readback: `project source` planinda ayni bounded content-free `metadata_inventory_digest` baglandi. Binding path/revision kimligi degismeden ignored/generated metadata snapshot'i plan readback'inde gorunur; partial inventory fail-closed reddedilir. Test CALISTIRILMADI; native source kabul kapisi acik.

- 16 Ekim 2026 WP-07.01..04 execution guard: `work resume`, canonical Work show ve dogal Work readback artik bagli job lease/recovery/binding durumunu ortak salt-okunur projection ile raporlar. Receipt'siz, recovery-required, binding driftli veya eksik readback devam yonlendirmesi yerine recovery/revalidation ister; yeni lease/grant edinimi authority'siz acik bir sonraki adim olarak kalir. Test CALISTIRILMADI; gercek lease/grant yeniden edinimi ve WP kabul kapisi acik.

- 16 Ekim 2026 WP-02.03 publication native admission: `LOCAL_EFFECT` CLI kapisi artik parsed `apply` parametresini degerlendiriyor; publication trust enrollment/rotation, evidence bundle, direct apply ve native apply-admission yapraklari effect oncesi explicit admission cagiriyor. Preview yollarinda mutasyon admission'i uretilmiyor; bilinmeyen/uygunsuz command path fail-closed kaliyor. Transport ve canonical authorization kontrolleri ayni yerde korunuyor. Test CALISTIRILMADI; gerçek host-native shell enforcement ve WP kabul kapisi acik.

- 16 Ekim 2026 WP-04.03 XML parser dilimi: bounded stdlib Expat parser'i DTD/external entity ve yapisal limitleri reddeder; metin birimleri element yolu ve satir locator'i ile normalize edilir. XML uzanti/MIME ve Jira read-only attachment route'u parser router'a baglandi. Test CALISTIRILMADI; belge kalite ve native kabul kapilari acik.

- 16 Ekim 2026 WP-04.05 arsiv ozel dosya siniri: ZIP/TAR metadata'sinda FIFO,
  cihaz, socket ve tanimsiz Unix dosya turleri normal icerik sayilmaz; gelecekteki
  extract adimindan once fail-closed reddedilir. Symlink/hardlink, traversal ve
  mevcut boyut/ratio sinirlari korunur. Regresyon kaynaklari hazir,
  test CALISTIRILMADI.

- 16 Ekim 2026 WP-04.01 discovery ozel dosya metadata'si: FIFO, cihaz, socket ve
  diger normal olmayan kaynak girdileri `FileKind.OTHER` olarak envanterde
  korunur; `unsupported-type` eligibility karariyla icerik disinda kalir ve tek
  basina discovery'yi partial yapmaz. Regresyon kaynagi hazir,
  test CALISTIRILMADI.

- 16 Ekim 2026 WP-12.05 yerel hedef planlari: Kaynak mimari export'una ayni katalogdan Miro Mermaid ve GitBook Markdown/traceability yerel planlari eklendi. Catalog/source ve input digest'leri, tool usage, yapilmayan kontroller, drift metodu ve remote/yayin false kapilari korunur. Test CALISTIRILMADI; gercek import/yayin ve kabul aciktir.

- Gorev 3 publication CLI composition: config-pinned public trust loader, strict plan/evidence/approval codec, native CLI plan/import/status/revoke/apply ve mutation admission baglandi. Imzali import exact replay idempotent; tarihsel completed apply yeni remote/trust islemi yapmaz. Regresyon kodu hazir, CALISTIRILMADI. Gercek producer enrollment/receipt producer ve native host enforcement kabulleri acik.

- 16 Ekim 2026 yeni hedef active; onceki duraklatilmis hedef degistirilmeden kalan tum kapsam icin yeni sayac baslatildi. Gorev 3 signed SQLite publication ledger v9, gercek source receipt/CAS dogrulamasi ve atomic live-claim consumption eklendi. Migration/backup/CLI db dry-run ve runtime version destekleri baglandi; kullanici DB migration'i yapilmadi. Regresyon kodu hazir, CALISTIRILMADI. Trusted producer provisioning, gercek test/verifier receipt adapter ve publication CLI composition acik.

- 16 Ekim 2026 kullanici devam karari: 15 Ekim 23:39 checkpoint duraklamasi kaldirildi. Gorev 3 typed Git publication service/gercek bounded transport ve canonical git.push claim/receipt baglantisi eklendi. Regresyon kodu hazir, CALISTIRILMADI. Production canonical authorization/evidence ledger adapter ve CLI composition siradaki acik baglantidir; native shell enforcement kabulu acik.

- 15 Ekim 2026 23:39 kullanici checkpoint/duraklama: docs/CHECKPOINT_2026-10-15_233926.md. Son gelistirme cb2918f; publication adapter kaynak incelemesi yapildi, yeni kod yazilmadi. Testler CALISTIRILMADI. Kullanici devam etmeden gelistirmeyi baslatma.

- Baslangic: 578238ef2aea1af614a766039cca3787a2e1e6f0. Kullanici bu sirali gelistirme ve son test duzenini onayladi.
- Gorev 2 WP-07 status readback: dogal dil Work durum raporu, donen acik Work penceresine bounded bound job ve source snapshot digest ozetini baglar; scope/truncation canonical SQLite okuyucusundan gelir. E2E regresyon kaynagi hazir, CALISTIRILMADI. Lease/grant yeniden edinimi ve Work kabul matrisi acik.
- Gorev 2 WP-07 exact read: tekil dogal dil Work okumasina exact-work bound job/source snapshot readback eklendi; unit/E2E regresyon kaynaklari hazir, CALISTIRILMADI. Lease/grant yeniden edinimi ve Work kabul matrisi acik.
- Gorev 2 WP-07 direct show: `work show --json` exact-work bound job/source snapshot readback ve salt-okunur authority alanlarini tasir; E2E regresyon kaynagi hazir, CALISTIRILMADI. Work kabul matrisi acik.
- Gorev 4 WP-06.05 natural history: exact Jira anahtari ve degisiklik/gecmis sorusu `ask` icinde bounded local Git history/diff okuyucusuna baglandi; explicit/coklu proje scope kapisi korundu. E2E regresyon kaynagi hazir, CALISTIRILMADI. Native/live kabul acik.
- Gorev 4 WP-06.05 history provenance: ask history ciktisi route ve resolved scope digestlerini tasir; kapsam karari history evidence ile birlikte yeniden denetlenebilir. E2E regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2 WP-07 checkpoint source readback: CheckpointRecord ve CLI/dogal dil ozetleri canonical `source_snapshot_id` alanini korur; E2E/unit regresyon kaynaklari hazir, CALISTIRILMADI.
- Gorev 2 WP-07 run source/config readback: RunRecord ve resume/status/show ozetleri canonical `source_snapshot_id` ve `config_revision_id` alanlarini korur; SQLite readback ve unit regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2 WP-07 direct show Run readback: `work show --json` exact Work icin bounded Run ozetleri ve truncation/limit alanlarini canonical SQLite'dan verir; E2E regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2 WP-07 direct show dependency readback: `work show --json` exact Work scope'unda canonical dependency ozetini de verir; E2E regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2 WP-07 read lock: Salt-okunur Work/Run CLI ve dogal dil okumalari SQLite read-only transaction ile write lock almadan calisir; mutasyon transactionlari ayni kalir. Unit regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2 WP-07 resume read lock: `workspace_resume` ve bounded knowledge ref okumasi desteklenen operational store'da read-only UoW kullanir; test store fallback'i korunur. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2 WP-07 resume Run baglari: `workspace_resume` acik ve yeni tamamlanan Work kayitlarinda bounded canonical Run ozetlerini plan, source snapshot ve config revision baglariyla birlikte tasir; limit/truncation alanlari korunur. Regresyon kaynagi hazir, CALISTIRILMADI. Lease/grant yeniden edinimi ve Work kabul matrisi acik.
- Gorev 2 WP-07 resume Run batch: Son Run readback'i Work basina N+1 SQLite sorgusu yerine tek bounded partition sorgusuna alindi; ayni source/config/plan baglari ve truncation sozlesmesi korunur. Regresyon kaynagi hazir, CALISTIRILMADI.
- Gorev 2 WP-07 resume Run uyumluluk: Batch okuyucusu olmayan eski test/adaptor store'lari icin limitli tekil canonical fallback eklendi; gercek SQLite hizli batch yolunda kalir. Regresyon kaynagi hazir, CALISTIRILMADI.
- Eski test sonuclari yeni degisiklikler icin kanit sayilmaz. Hicbir WP bu planla kapatilmaz.
- Gorev 1 gelistirmesi 747c6ea: lease/process marker ayrimi ve native root-drift reddi. Regresyonlar hazir; CALISTIRILMADI.
- Gorev 3 native admission parcasi: start/checkpoint/close LOCAL_EFFECT ve work-item hint baglantisi eklendi. Regresyonlar hazir; CALISTIRILMADI. Shell/publication ve diger istemci baglari acik.
- Gorev 2 proje kapsamli work resume: exact project resolver ve SQL LIMIT oncesi scope filtresi CLI'a baglandi. Regresyon kodu hazir, CALISTIRILMADI. Diger Work/lease/grant akislarinin gelistirme denetimi suruyor.
- Gorev 2 cross-client resume: canonical binding project/Work eslestirmesi ve operational source snapshot yeniden dogrulamasi eklendi. Foreign scope, missing snapshot ve DB revision drift regresyonlari hazir, CALISTIRILMADI.
- Gorev 2 keyed Work create: proje-key kimligi mevcut Work PK/ilk revision'a baglandi; current-state replay ve input drift reddi eklendi. CLI --key/--json girisi ile concurrency/scope/revision regresyonlari hazir, CALISTIRILMADI.
- Gorev 2 harici is numarasi: bounded exact resolver show/history/transition/close yollarina baglandi; proje belirsizligi ve foreign scope fail-closed. Regresyon kodu hazir, CALISTIRILMADI.
- Gorev 2/6 Work raporu: exact SQL state sayimlari ve open/closed/nested event-run-step-checkpoint pencereleri eklendi; metin toplamlarini truncation etkilemez. Buyuyen gercek SQLite fixture regresyonu hazir, CALISTIRILMADI.
- Gorev 2 Work dependency: additive v8 canonical event/view/trigger, revision/savepoint, CLI exact plan, queue/lease/effect ve explicit migration/backup baglantilari eklendi. Kullanici DB migration'i yapilmadi. Regresyon kodu hazir, CALISTIRILMADI; docs/WORK_BAGIMLILIKLARI.md.
- Geri donus: her asamanin exact commit'ini yeni revert commit'iyle geri al; kullanici verisine veya operational DB'ye reset uygulama.
- Gorev 2 native Work islem secimi: route/dispatch allowlist'i ve komut haritasi Work islemlerine baglandi; Work key'i proje/Jira scope'u uretmez. Ince AGENTS girisi canonical dogal dil Work akisina baglandi. Regresyon kodu hazir, CALISTIRILMADI; host davranisi ve gercek kabul halen acik.
- Gorev 2 dogal dil tekil okuma: ask acik Work/gorev/numarali is referansini scoped canonical show/history/dependencies/checkpoints'e baglar. Mixed/belirsiz talepler clarification; bounded event/run/checkpoint pencereleri ve sifir provider. Capability/observability dependency komutlariyla hizalandi. Regresyon kodu hazir, CALISTIRILMADI. Create/transition/close dogal dil kabul kapisi acik.
- Gorev 2 dependency rapor/resume: bounded tek SQL aggregate dogal dil, work resume ve workspace resume'a baglandi. Canonical dependency blocker checkpoint yonlendirmesinden once gorunur; v8 oncesi unknown korunur. Regresyon kodu hazir, CALISTIRILMADI. Ruling: ara test ve worktree skill adimlari kullanicinin son test ve gercek kok talimatiyla uygulanmaz.
