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

- Gorev 3 publication CLI composition: config-pinned public trust loader, strict plan/evidence/approval codec, native CLI plan/import/status/revoke/apply ve mutation admission baglandi. Imzali import exact replay idempotent; tarihsel completed apply yeni remote/trust islemi yapmaz. Regresyon kodu hazir, CALISTIRILMADI. Gercek producer enrollment/receipt producer ve native host enforcement kabulleri acik.

- 16 Ekim 2026 yeni hedef active; onceki duraklatilmis hedef degistirilmeden kalan tum kapsam icin yeni sayac baslatildi. Gorev 3 signed SQLite publication ledger v9, gercek source receipt/CAS dogrulamasi ve atomic live-claim consumption eklendi. Migration/backup/CLI db dry-run ve runtime version destekleri baglandi; kullanici DB migration'i yapilmadi. Regresyon kodu hazir, CALISTIRILMADI. Trusted producer provisioning, gercek test/verifier receipt adapter ve publication CLI composition acik.

- 16 Ekim 2026 kullanici devam karari: 15 Ekim 23:39 checkpoint duraklamasi kaldirildi. Gorev 3 typed Git publication service/gercek bounded transport ve canonical git.push claim/receipt baglantisi eklendi. Regresyon kodu hazir, CALISTIRILMADI. Production canonical authorization/evidence ledger adapter ve CLI composition siradaki acik baglantidir; native shell enforcement kabulu acik.

- 15 Ekim 2026 23:39 kullanici checkpoint/duraklama: docs/CHECKPOINT_2026-10-15_233926.md. Son gelistirme cb2918f; publication adapter kaynak incelemesi yapildi, yeni kod yazilmadi. Testler CALISTIRILMADI. Kullanici devam etmeden gelistirmeyi baslatma.

- Baslangic: 578238ef2aea1af614a766039cca3787a2e1e6f0. Kullanici bu sirali gelistirme ve son test duzenini onayladi.
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
