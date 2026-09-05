# Measured Loop Operations Runbook

Bu runbook migration 76 ile eklenen olcumlu loop execution plane'ini isletmek ve
ariza durumunda fail-closed karar vermek icindir. PostgreSQL tek authority'dir.
CLI/Obsidian gorunumu yetki veya basari kaniti degildir; raw transcript, secret ve PII
hicbir komuta, loga, checkpoint'e veya projection'a yazilmaz.

## 1. Baslangic ve salt okunur durum

Repository protokollerini (`AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md`) uygula.
Ardindan sadece gerekli exact kimliklerle su gorunumleri oku:

```powershell
zekam doctor --json
zekam work resume --json
zekam loop assess --work-item-id <WORK_ITEM_ID> --json
zekam loop status <LOOP_ID> --json
zekam loop attempts <LOOP_ID> --json
zekam loop progress <LOOP_ID> --json
```

Kabul edilebilir baslangic:

- migration head 76 ve pending migration yok;
- recovery-required veya receipt'siz claim yok;
- loop objective, plan, policy, source revision ve validator manifest digestleri current;
- attempt 2+ icin predecessor ve exact `LoopProgressPacket` var;
- builder ile verifier assignment/execution identity farkli;
- provider/network default deny.

`zekam loop` komutlari salt okunurdur. Pause/drain/cancel, retry, rollback, migration,
effect veya provider cagrisina yetki vermez.

## 2. Topology karari

Topology TaskPlan olusturulmadan once `TopologyPlanner` ile uretildigi plan digestine
baglanir. Karar tablosu:

| Kosul | Topology | Operator davranisi |
|---|---|---|
| Tek, deterministik, effectsiz is | `direct` | Bir kez calistir; loop acma |
| Olcum yok, bagimsiz degil, pahali veya retry-safe degil | `single-pass` | Tek deneme; sonucu verifier'a ver |
| Ayni artifact, ucuz external olcum, reversible ve receipt-bound | `bounded-loop` | Stable objective ve sabit butceyle loop |
| Yaratici cesitlilik, izole candidate'lar | `tournament` | Candidate'lari birbirinden gizle; bagimsiz selector ve gerekiyorsa human gate |
| Farkli deliverable ve gercek dependency/parallel hazirlik | `graph` | DAG dependency/resource kapilarini koru; fan-in receipt uret |
| Geri alinmaz veya high-risk effect | `queue-human-review` | Exact insan yetkisi gelmeden effect calistirma |
| Measurement/reversibility/coordination gibi kritik alan bilinmiyor | `blocked` | Eksik kaniti tamamla; topology uydurma |

Kanıt: `src/zekam/application/topology_planner.py` (`TopologyPlanner`) ve
`src/zekam/application/graph_execution.py` (`GraphExecutionRecorder`).

## 3. Normal loop yasam dongusu

1. Stable objective, directional metric vector, immutable validator asset manifest,
   source/plan/policy digestleri ve butceler kaydedilir.
2. Orchestrator logical attempt icin deterministik bir ID ve `max_attempts=1` job uretir.
3. Existing worker exact capability ile job'u claim eder; admission current job,
   ordinal, novelty ve packet baglarini denetler.
4. Builder artifact'i degistirir; validator dis kaynakli metric evidence uretir. Model
   self-report progress sayilmaz.
5. Progress compiler bounded packet uretir; DB progress decision'i packet ve metric
   digestlerine baglar.
6. Completion terminal ise loop kapanir. Active ise ayni transaction sinirinda yalniz
   bir sonraki attempt job'i enqueue edilir.
7. Her effect claim-before-effect, terminal receipt ve checkpoint ile kapanir.

Bir metrik yalniz yonune gore `minimum_meaningful_delta` esigini gectiyse ilerleme
sayilir. Yeni evidence retry hakkina destek olabilir; olculmus iyilesme yoksa progress
degildir.

## 4. Plateau ve manual review

Belirtiler:

- `zekam loop progress` son satirlarda `progress_state=plateau` veya
  `stop_reason=no-progress` gosterir;
- ayni patch, hypothesis veya failure fingerprint tekrar eder;
- stall limit dolmustur.

Yap:

1. Yeni job enqueue etme; loop'u canonical control event ile `paused` duruma getir.
2. Objective, validator manifest, source ve policy digestlerini yeniden dogrula.
3. Yeni, gercek external evidence varsa yeni Intent/Plan revision ile degerlendir.
4. Evidence yoksa terminal sonucu `manual-review`/blocked olarak tut; retry verme.
5. Human reviewer kararini exact authorization ve reason digest ile kaydetmeden
   `active` durumuna donme.

Public write CLI bulunmaz. Control event yalniz application/repository katmaninin
`runtime.record_loop_control_event` kapisindan exact plan ve authorization ile
uretilmelidir; elle tablo UPDATE/INSERT yapma.

## 5. Invalid measurement

Su durumlardan biri invalid sayilir:

- producer self-report;
- measurement identity ile verifier identity ayni;
- metric eksik, sinir disi, yon/role/spec ile uyumsuz;
- packet metric vector veya progress decision digest drift;
- secret/PII/raw transcript anahtari/degeri.

Yap:

1. Attempt'i basarili sayma ve next job enqueue etme.
2. `invalid-measurement` stop reason/evidence digestini kaydet; ham measurement veya
   transcript'i loglama.
3. Artifact degisikligi loop-owned ise bolum 10'daki exact inverse rollback'i kullan.
4. Measurement adapterini farkli execution identity ile yeniden dogrula. Ayni evidence'i
   sessiz retry etme.
5. Guvenli sanitize fixture ile `tests/security/test_measured_loop_wp14_security.py`
   negatif kapilarini calistir.

## 6. Validator drift

Drift gostergeleri: manifest digest, validator spec, source revision, builder/verifier
assignment veya fixture/metric/threshold asset digestlerinden biri current contract ile
eslesmez.

Yap:

1. Loop'u pause et; current attempt'i continue/replay etme.
2. Validator assetlerini degistirmeden manifesti kaynaktan yeniden hesapla.
3. Builder write scope'unun validator logical ref'leriyle kesisip kesismedigini kontrol et.
4. Degisiklik gercekse yeni objective/Intent/Plan/policy revision olustur; eski packet'i
   stale say.
5. Bagimsiz verifier yeni manifest ve current source ile sifirdan olcum yapsin.

Builder test, fixture, metric veya threshold assetini duzeltemez; bu degisiklik ayri
reviewed validator degisikligidir.

## 7. Graph deadlock veya blocker

Belirtiler:

- ready node yok, fakat terminal olmayan node var;
- predecessor tamamlanmadan node baslatilmak isteniyor;
- logical resource conflict iki node'u ayni anda bloke ediyor;
- gercek interval overlap yokken parallelism iddia ediliyor;
- coordination maliyeti is maliyetini geciyor.

Yap:

1. `zekam loop graph --work-item-id <WORK_ITEM_ID> --json` ile critical path,
   overlap, resource/dependency wait ve terminal state'i oku.
2. TaskPlan DAG'da eksik/cycle dependency ve logical resource mode'larini incele.
3. Running effect varsa receipt/recovery durumunu once uzlastir; job'u raw silme.
4. Dependency gercek degilse yeni Plan revision yap. Parallelism kaniti yoksa
   `single-pass` veya daha basit topology'ye don.
5. `recovery-required` node varken fan-in completed receipt uretme.

`GraphExecutionRecorder` exact TaskPlan step setini, predecessor zamanlarini, conflict
olmayan overlap'i ve fan-in receipt'ini zorunlu kilar.

## 8. Effect sonrasi crash

En kritik ayrim effect'in dis dunyada gerceklesip gerceklesmedigidir:

```text
claim var + terminal receipt yok
=> recovery-required
=> silent retry yasak
```

Yap:

1. Lease, claim, idempotency key, fencing token, adapter digest ve checkpoint'i oku.
2. Provider/adapter'in salt okunur reconciliation kaniti ile effect'i exact hedefte ara.
3. Effect gerceklestiyse yeni effect calistirmadan ayni claim'e terminal completed receipt
   ve checkpoint bagla.
4. Gerceklesmedigi kanitlanabiliyorsa reviewed RecoveryReconciliationPlan olustur; eski
   attempt'i sessizce yeniden calistirma.
5. Kanit yetersizse `blocked-effect-uncertain`/manual review olarak birak.

Queue job completion tek basina effect basarisi degildir. Receipt ve adapter evidence
eslesmeden Work veya loop terminal success olamaz.

## 9. Pause, drain ve cancel

- **pause:** Yeni attempt admission/enqueue kapanir; running effect'i kesmez. Inceleme veya
  gecici operator durusu icindir.
- **drain:** Yeni attempt kapanir; mevcut running job'lar receipt/checkpoint ile guvenli
  terminale gider.
- **cancel:** Yeni attempt kalici kapanir. Running effect varsa once receipt/recovery
  uzlastirilir; raw job delete veya blanket process kill terminal kanit degildir.

Her state gecisi current plan digest, exact authorization ID/digest ve sanitize reason
digest ile immutable `runtime.loop_control_event` olarak kaydedilir. `paused`, `draining`
veya `cancelled` iken `DurableLoopOrchestrator` ve DB binding yeni attempt'i fail-closed
reddeder. Yeniden `active` ancak yeni reviewed control event ve current contract
dogrulamasi ile olabilir.

## 10. Quota ve butce fallback

Butce/timeout sinyalleri: kalan attempt, token, cost veya time sifir; worker capacity yok;
provider/network default deny; measurement/action maliyet orani esik ustu.

Yap:

1. Yeni job enqueue etme; mevcut job'u receipt/checkpoint ile terminale getir.
2. External measurement yerel ve ucuz degilse `single-pass` veya manual review'e don.
3. Provider cagrisini otomatik fallback olarak acma. Exact provider plan, cagri butcesi ve
   ayri kullanici authorization olmadan remote call yapma.
4. Budget artisi gerekiyorsa yeni policy/Plan revision ve bagimsiz review iste; eski
   packet/policy ile devam etme.
5. Observatory'de actual/reserved budget ve omission count'u sanitize olarak kaydet.

## 11. Regression rollback

Rollback yalniz `metric-regression`, `validator-invalid` veya `security-invalid` icin ve
yalniz loop-owned degisiklikte uygulanir:

1. Attempt oncesi baseline, allowed paths ve protected user-dirty digestini al.
2. Exact change set ve inverse patch digestini uret.
3. Source HEAD, allowed paths ve protected state degismediyse apply-check calistir.
4. Yalniz inverse patch'i uygula; `git reset --hard`, blanket checkout veya kullanici
   dosyasini silme.
5. Post-state digest ve rollback receipt'i bagimsiz verifier ile dogrula.

Kanıt: `src/zekam/application/loop_rollback.py`,
`src/zekam/infrastructure/git/loop_patch.py` ve
`tests/e2e/test_measured_loop_wp14.py` rollback yolculugu.

## 12. Stale progress packet

Packet su alanlardan biri degisirse stale'dir: objective digest, source revision, plan
digest, policy revision digest, validator manifest digest, predecessor attempt veya
ordinal.

Yap:

1. Packet'i hydrate/dispatch etme ve yeni job bind etme.
2. Current contracti PostgreSQL'den oku; Markdown veya sohbetten authority uretme.
3. Degisiklik planliysa yeni Intent/Plan/policy revision ve yeni external baseline ile
   sifirdan packet zinciri baslat.
4. Degisiklik beklenmiyorsa drift'i security/operational defect olarak kaydet.
5. Attempt 2+ contextine sadece bounded, transcript-free `LoopProgressPacket` koy;
   `src/zekam/application/loop_progress_hydration.py` current binding'i zorunlu kilar.

## 13. Onceki projection ile uzlastirma

Migration 76 oncesi projection olmasi hata degildir. Repository once
`to_regclass('runtime.loop_policy_v2')` ile measured plane'in varligini kontrol eder;
tablo yoksa eski Work projection'ini bozmadan measured satirlari bos birakir
(`src/zekam/infrastructure/postgres/markdown_projection_repository.py`).

Upgrade sonrasi:

1. PostgreSQL migration head ve current Work/loop state'i dogrula.
2. Mevcut Obsidian store'u kullan; yeni vault/store kurma.
3. Salt okunur plan al, sonra exact projection apply yetkisiyle deterministic generation
   uret. Projection authority degildir.
4. `zekam memory obsidian-status --project-id <PROJECT_ID> --profile private-local` ile
   CURRENT,
   manifest, file digest ve projection receipt'i current DB digestine karsi dogrula.
5. Stale projection varsa close/release yapma. Kanonik DB'den yeniden uret; projection
   dosyalarini elle duzeltme.
6. Root `AKTIF_GOREV.md`/YAML projection'ini de kanonik Work/Run/receipt ile uzlastir.

## 14. Kapanis ve acceptance

Loop veya Work ancak su zincir eslesiyorsa tamamlanir:

```text
current Work ve Plan revision
+ terminal run/loop state
+ attempt/job checkpointleri
+ external metric evidence ve progress decision
+ bagimsiz verifier sonucu
+ effect claim/terminal receipt varsa exact eslesme
+ current source HEAD
+ current Obsidian/root projection receipt
```

Kapanista:

1. Unit, integration, real PostgreSQL E2E ve security testlerini calistir.
2. Builder'dan farkli model/execution identity ile independent verifier al.
3. Claim-without-receipt, pending lease/recovery ve stale projection sayisini sifirla.
4. `zekam close` kapilarini receipt-bound tamamla; projection'i kanonik state ile yeniden
   uzlastir.
5. GitHub Actions `workflow_dispatch` olarak kalir. Acik user authorization olmadan
   otomatik CI, provider call, commit, push veya PR olusturma.
