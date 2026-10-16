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

- Gorev 6 WP-11.01 trial usage: optional salt okunur native-usage raporu exact source receipts/assignment/artifact chain ve local grade'den uretilir. Frozen aggregate/digest degismez; selected trial scope/model/baseline/candidate coverage, partial null totals ve separate estimated cost eklendi. Yeni call/yazma sifir; full account/billing kaniti degildir. Regresyon kaynaklari hazir, CALISTIRILMADI; docs/OPENCODE_NATIVE_USAGE.md.

- Gorev 5 WP-09/10 native usage: desteklenen exact OpenCode v2.0.29 schema kaynagindan step_finish token component ve host USD estimate optional CAS artifact'e baglandi. Legacy/partial null, native skill iki-step coverage ve total/identity readback eklendi; strict artifact consumers guncellendi. Tournament historical/new selector ayri kapsamda raporlar. Provider requests/billed cost bilinmez, hard-cap/gercek native kabul acik. Regresyon kaynaklari hazir, CALISTIRILMADI; docs/OPENCODE_NATIVE_USAGE.md.

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
