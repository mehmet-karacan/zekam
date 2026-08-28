# AKTİF GÖREV — Zekam Memory Learning Loop ve Obsidian Projection

## 0. Bu dosyanın kullanım kuralı

Bu dosya önce dış görev girdisi olarak `../zekam-girdi/AKTIF_GOREV.md` konumundan okunur.
Ajan görevi ve mevcut repo durumunu doğruladıktan sonra, görev girdisinde yapısal veya
güvenlik açısından problem yoksa bu dosya repo kökündeki `AKTIF_GOREV.md` dosyasına
kopyalanır ve artık güncel aktif görev olarak burada tutulur.

Önerilen akış:

```text
<workspace>/
├── zekam/
│   └── AKTIF_GOREV.md          ← doğrulama sonrası güncel aktif görev
└── zekam-girdi/
    └── AKTIF_GOREV.md          ← dışarıdan gelen görev girdisi
```

Kurallar:

1. Önce `../zekam-girdi/AKTIF_GOREV.md` okunur ve doğrulanır.
2. Görev, mevcut HEAD ve Zekam sözleşmeleriyle çelişiyorsa körlemesine kopyalanmaz; sorun
   raporlanır ve mevcut kök görev korunur.
3. Problem yoksa dış görev dosyası repo kökündeki `AKTIF_GOREV.md` üzerine kontrollü
   olarak kopyalanır.
4. Bundan sonra görev ilerledikçe kökteki `AKTIF_GOREV.md` güncel tutulur.
5. `zekam-girdi` yalnız giriş/handoff alanıdır; aktif çalışmanın yaşayan görev kaydı
   repo kökündeki `AKTIF_GOREV.md` olur.
6. `AKTIF_GOREV.yaml` veya kanonik PostgreSQL Work state varsa bunlarla drift oluşturulmaz;
   gerekli senkron Zekam'ın mevcut sözleşmelerine göre yapılır.

---

## 1. Görev kimliği

| Alan | Değer |
|---|---|
| Görev | Zekam Memory Learning Loop ve Obsidian Projection |
| Durum | Uygulamaya hazır görev spesifikasyonu |
| Öncelik | P0 + P1 |
| Analiz tarihi | 2026-08-27 |
| İncelenen `main` HEAD | `0be98185f4393e0f56db46164939b9873214123e` |
| Kanonik otorite | PostgreSQL Work Graph + Memory Continuity tabloları |
| İnsan arayüzü | Üretilmiş Markdown / Obsidian projeksiyonu |
| İlk gerçek istemci | Mevcut OpenCode yaşam döngüsü entegrasyonu |
| İkinci istemci hedefi | Öncelik Codex; doğrulanabilir lifecycle yüzeyi yoksa Claude Code |
| Çalışma modu | Önce shadow, kanıt sonrası enforced |
| Uzak model çağrıları | Varsayılan kapalı |
| GitHub yazma/push | Ayrı açık kullanıcı yetkisi olmadan yasak |

### Yeniden tabanlama kuralı

Uygulama başlangıcında `main` HEAD yukarıdaki SHA ile aynı değilse görev iptal edilmez.
Ajan:

1. Yeni HEAD’i kaydeder.
2. Bu görevdeki her boşluğu güncel kod üzerinde yeniden doğrular.
3. Artık kapanmış maddeleri tekrar yapmaz.
4. Yeni çakışmaları ve kapsam değişimini bir `baseline-drift` receipt’iyle görünür kılar.
5. Kullanıcı değişikliklerini korur; alakasız dosyaları geri almaz.

---

## 2. Misyon

Zekam’da hâlihazırda bulunan Memory Continuity Plane’i, uçtan uca çalışan ve
kanıtlanabilir bir **Memory Learning Loop** hâline getir.

Hedef, yapay zekânın Markdown dosyalarına kontrolsüz biçimde “kendi gerçeğini”
yazması değildir. Hedef şudur:

```text
typed lifecycle event
→ immutable ledger
→ deterministic delta / bounded observation
→ optional model-generated candidate
→ policy + provenance + dedupe + conflict checks
→ review / exact authorization
→ canonical memory revision
→ deterministic Obsidian projection
→ bounded next-session hydration
```

Obsidian bu mimaride yeni bir otorite veya ayrı bir bellek motoru olmayacaktır.
Yalnızca insanın okuyabildiği, bağlar arasında gezebildiği, gerektiğinde tamamen
silinip kanonik veriden yeniden üretilebildiği bir projeksiyon olacaktır.

---

## 3. Kaynak fikirlerin Zekam’a uyarlanmış özeti

İncelenen video altyazısındaki yararlı fikirler:

- oturum başlangıcında son durumun zorunlu yüklenmesi,
- oturum kapanışında yapılanlar, kararlar, açık işler ve sonraki adımın özetlenmesi,
- compaction öncesinde kaybı önleyen checkpoint,
- günlük notlar/daylog,
- WikiLink tabanlı bilgi bağlantıları,
- Git ile geçmiş ve cihazlar arası eşleme,
- modelden bağımsız düz Markdown,
- tekrar eden işlerden skill/prosedür çıkarma,
- başarısızlıkları kaydedip aynı hataya yeniden düşmeme,
- düşük maliyetli modellerle özet/hijyen, güçlü modellerle zor analiz,
- bilinmeyen alanı uydurmak yerine boş bırakma ve kaynağı belirtme,
- zamanla büyüyen, kullanıcıyla birlikte öğrenen ikinci beyin.

Aynen alınmaması gereken noktalar:

- kişisel, kurumsal ve gizli tüm veriyi aynı fiziksel vault/senkron sınırına toplamak,
- ham konuşmaları sınırsız saklamak,
- model çıktısını doğrudan doğrulanmış gerçek saymak,
- yapay zekânın güvenlik, CI, migration veya retention kurallarını kendi başına değiştirmesi,
- Git veya iCloud üzerinden secret/PII yaymak,
- yüksek riskli formları kaynak ve insan onayı olmadan otomatik göndermek,
- Mem0 veya başka bir servisi Work/Policy/Authorization otoritesine çevirmek.

---

## 4. Güncel Zekam teşhisi

### 4.1 Güçlü ve korunması gereken mevcut yapı

Güncel kod tabanı aşağıdaki temel taşlara zaten sahiptir:

1. **Kanonik PostgreSQL yaklaşımı**
   - Work state, lease, checkpoint, receipt, evidence ve memory continuity durumu DB’dedir.
   - Markdown/YAML çıktıları türetilmiş ve salt okunur projeksiyondur.
   - SQLite yalnız kısıtlı minimum profildir; tam otoriteye sessiz fallback yapamaz.

2. **Yaşam döngüsü sözleşmesi**
   - session start, checkpoint, pre-compaction ve close olayları için tipli modeller vardır.
   - digest, idempotency, boyut limiti ve lifecycle receipt zinciri bulunur.
   - transcript/secret benzeri payload’lar reddedilir.

3. **İstemci köprüsü**
   - Codex, Claude Code ve OpenCode için genel adapter kayıtları vardır.
   - `lifecycle-events-v2` yalnız exact contract doğrulamasıyla etkinleşir.
   - OpenCode için gerçek plugin/outbox/drain entegrasyonu bulunmaktadır.

4. **Aday derleyici ve kalıcılık altlığı**
   - candidate-only compiler;
   - sınıflandırma, schema sanitize, dedupe, supersession, conflict ve quarantine;
   - watermark, candidate, hygiene ve compiler receipt kalıcılığı;
   - secret ve riskli sınıfların otomatik terfiye kapatılması vardır.

5. **Hydration ve projeksiyon tazeliği**
   - MUST/SHOULD/ON_DEMAND/NEVER_AUTO_LOAD katmanları;
   - token bütçesi;
   - source HEAD + migration head + DB digest bağlaması;
   - stale projeksiyonda fail-closed davranışı tasarlanmıştır.

6. **Governance ve operasyon**
   - shadow/enforced upgrade protokolü;
   - exact authorization;
   - bağımsız verifier ayrımı;
   - doctor/repair;
   - worker, scheduler, job, lease, claim ve terminal receipt modeli vardır.

7. **Bellek tasarım belgeleri**
   - Native PostgreSQL MemoryEngine’in otorite olması,
   - Mem0’nun yalnız opsiyonel adapter olması,
   - candidate → reviewed → active → superseded/revoked/archived yaşam döngüsü,
   - semantic/procedural/failure/preference sınıfları önceden tanımlanmıştır.

### 4.2 Güncel açıklar

#### P0 — Projeksiyon kapanış tutarsızlığı

Repo kökündeki üretilmiş aktif görev projeksiyonu “completed” görünürken eski source
HEAD’i taşımakta ve hâlâ yapılacak güvenli adım göstermektedir. Bu, tasarlanan
projection freshness invariant’ının kapanış/release yolunda kesin zorunluluk olarak
her durumda uygulanmadığını gösterir.

**Karar:** `completed`, lease release ve release gate; aynı transaction/closure receipt
zincirinde güncel source HEAD, migration head, DB digest ve projeksiyon receipt’i olmadan
başarılı olamaz.

#### Operasyonel karar — CI bilinçli olarak manuel kalacak

`quality.yml` ve `package-acceptance.yml` yalnız `workflow_dispatch` ile çalışmaktadır.
Bu durum şu an bir açık değildir; GitHub kullanım süresi/limiti nedeniyle bilinçli
operasyon tercihidir.

**Karar:** otomatik `pull_request` / `push` CI tetiklemeleri bu görev kapsamında
açılmayacaktır. Mevcut manuel CI korunur. Kullanıcı ileride açıkça isterse otomatik CI
ayrı bir değişiklik olarak etkinleştirilebilir. Yerel testler ve bağımsız verifier yine
zorunludur; GitHub CI çalıştırılması gerektiğinde kullanıcı tarafından manuel başlatılır.

#### P0 — Hook kayıt yapısı var, semantik orchestration eksik

`memory_hooks.py` gerekli tetikleyicileri tanır; fakat varsayılan handler’lar ağırlıklı
olarak “observation accepted” seviyesinde sabit sonuç döndürür. Hook ile:

- lifecycle ledger,
- checkpoint,
- candidate compilation,
- projection refresh,
- gap/repair

arasında tek bir üretim orchestration yolu açık biçimde bağlanmamıştır.

**Karar:** hook hiçbir zaman doğrudan Markdown yazmamalı. Hook, tipli olayı
`MemoryContinuityOrchestrator` üzerinden ledger/outbox/job zincirine vermelidir.

#### P0 — Derleyici var, sürekli öğrenme döngüsü kanıtlanmış değil

`MemoryCandidateCompiler` ve PostgreSQL persistence katmanı vardır; fakat incelenen üretim
yüzeylerinde session close/pre-compaction veya scheduler/worker’dan derleyiciye giden
uçtan uca, crash-safe ve replay-safe orchestration yolu açıkça kanıtlanmamıştır.

**Karar:** event-driven enqueue + durable scheduled catch-up birlikte uygulanmalıdır.

#### P0 — İkinci gerçek harness eksik

Genel Codex/Claude/OpenCode adapter’ları vardır; gerçek runtime entegrasyonu ve kapsamlı
E2E kanıtı OpenCode tarafında görünmektedir. İkinci somut istemcinin aynı lifecycle
sözleşmesini gerçekten uyguladığı kanıtlanmalıdır.

**Karar:** öncelik Codex’tir. Codex’in kurulu sürümünde exact ve güvenilir lifecycle
yüzeyi bulunamazsa Claude Code seçilir; seçim ADR ile belgelenir. Destek varmış gibi
taklit edilmez.

#### P1 — Obsidian/Markdown insan projeksiyonu yok

Mevcut projeksiyon altyapısı görev/hydration için güçlüdür; fakat daylog, karar, bilgi,
skill, failure ve bağlantı grafiğini insan için düzenleyen deterministik Obsidian profili
yoktur.

#### P1 — Skill/failure/daylog akışları uçtan uca değil

Tasarım belgeleri semantic/procedural/failure memory’yi tarif etmektedir. Bunların
gözlemden adaya, review’dan active revision’a ve Obsidian görünümüne kadar tamamlanmış
tek bir operasyon yolu gereklidir.

#### P1 — Yüksek riskli otomasyon için ortak güvenlik kapısı yok

Videodaki form doldurma örneği Zekam’da doğrudan uygulanmamalıdır. Alan bazlı kaynak,
güven, bilinmeyeni boş bırakma, önizleme ve insan onayı için reusable guard gerekir.

---

## 5. Değiştirilemez mimari kararlar

### K-01 — Tek kanonik otorite

Kanonik gerçek PostgreSQL’dedir. Obsidian, Markdown, Git, Mem0, embedding index,
cache veya herhangi bir model otorite üretemez.

### K-02 — Model çıktısı yalnız adaydır

Model tarafından çıkarılan özet, ilişki, skill, tercih veya karar:

- active memory olamaz,
- Work state değiştiremez,
- policy gevşetemez,
- lease/approval üretemez,
- exact authorization yerine geçemez.

### K-03 — Obsidian salt okunur projeksiyondur

Obsidian içindeki üretilmiş notlar varsayılan olarak elle düzenlenmez. İnsan düzeltmesi
gerekiyorsa ayrı bir `feedback/correction` komutu ile kanonik candidate veya correction
evidence oluşturulur; sonraki projection build notu yeniden üretir.

### K-04 — Ham transcript kalıcı bellek değildir

Ham prompt/response/transcript:

- Git’e yazılmaz,
- otomatik hydration’a girmez,
- secret/PII içerme riski nedeniyle yalnız açık politika altında kısa süreli local CAS’ta
  tutulabilir,
- active semantic/procedural memory üretmek için tek başına kanıt sayılmaz.

### K-05 — Realm ve güven sınırları ayrıdır

Aynı Zekam motoru birden çok realm destekleyebilir; fakat:

- kişisel,
- kurumsal,
- kamuya açık

veriler aynı fiziksel senkron hedefinde zorunlu olarak birleşmez. Cross-realm retrieval
varsayılan kapalıdır. Kurumsal veri kişisel Git/iCloud’a çıkamaz.

### K-06 — Git veritabanı değildir

Git:

- kod,
- public-safe projection,
- sanitize edilmiş manifest/receipt,
- geri alınabilir sürüm geçmişi

için kullanılabilir. Secret, raw transcript, kişisel belge veya tam DB dump Git’e girmez.

### K-07 — Mem0 opsiyoneldir

Mem0 daha sonra:

- working/instant cache,
- external semantic index,
- hızlandırıcı adapter

olarak eklenebilir. Native PostgreSQL çalışması Mem0 olmadan devam eder. Mem0 sonucu
untrusted external projection sayılır.

### K-08 — Self-healing ile self-authority ayrıdır

Sistem bozuk projeksiyonu yeniden üretebilir; fakat kendi güvenlik politikasını,
migration’ını, CI kuralını veya retention kararını tek başına değiştiremez.

### K-09 — Bilinmeyen veri uydurulmaz

Özellikle form, resmi belge, ödeme, vergi, sözleşme veya iletişim otomasyonunda kaynak
bulunamayan alan boş kalır ve `missing/unknown` olarak raporlanır.

### K-10 — Uzak model çağrısı varsayılan kapalıdır

Candidate extraction için model zorunlu değildir. Deterministik extractor çalışabilmeli;
model gerekiyorsa Provider Gate, sınıflandırma, redaction, bütçe ve receipt uygulanır.

---

## 6. Hedef mimari

```text
┌─────────────────────────────────────────────────────────────────────┐
│ Codex / Claude Code / OpenCode / CLI / Worker                       │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ content-free, typed lifecycle event
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ClientLifecycleBridge + exact adapter contract                      │
│ schema • size limit • secret/transcript rejection • idempotency     │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Immutable Lifecycle Ledger + Outbox + Receipts                      │
└───────────────┬───────────────────────────────┬─────────────────────┘
                │                               │
                │ start                         │ close/pre-compact/checkpoint
                ▼                               ▼
┌──────────────────────────┐     ┌────────────────────────────────────┐
│ Bounded Hydration        │     │ Memory Learning Orchestrator       │
│ MUST/SHOULD/ON_DEMAND    │     │ enqueue • watermark • replay       │
└──────────────────────────┘     └─────────────────┬──────────────────┘
                                                  ▼
                                  ┌────────────────────────────────────┐
                                  │ Candidate Compiler                 │
                                  │ deterministic delta first         │
                                  │ optional governed model extractor │
                                  └─────────────────┬──────────────────┘
                                                    ▼
                                  ┌────────────────────────────────────┐
                                  │ Candidate / Conflict / Quarantine  │
                                  │ Hygiene findings + receipts        │
                                  └─────────────────┬──────────────────┘
                                                    │ exact review/promotion
                                                    ▼
                                  ┌────────────────────────────────────┐
                                  │ Canonical Memory Revisions         │
                                  │ semantic • procedural • failure    │
                                  │ preference • episodic              │
                                  └──────────────┬─────────────────────┘
                                                 │
                         ┌───────────────────────┴─────────────────────┐
                         ▼                                             ▼
          ┌──────────────────────────────┐           ┌──────────────────────────┐
          │ Context Compiler / Retrieval │           │ Obsidian Projection      │
          │ source + validity + budget   │           │ deterministic/read-only  │
          └──────────────────────────────┘           └──────────────────────────┘
```

### Kapanış davranışı

`session_close` kullanıcıyı uzun bir model çağrısını beklemeye zorlamaz.

1. Final checkpoint yazılır.
2. Bounded close summary observation kaydedilir.
3. Compiler job/outbox kaydı atomik biçimde enqueue edilir.
4. Zorunlu projection freshness ve lifecycle receipt’leri doğrulanır.
5. Close receipt yayımlanır.
6. Worker candidate compilation ve Obsidian projection catch-up işlemlerini yürütür.
7. Sonraki session start, yalnız doğrulanmış ve güncel hydration paketini yükler.

Close sırasında deterministik mini-derleme yapılabilir; fakat uzak provider çağrısı close
transaction’ını açık tutamaz.

---

## 7. Hakikat sınıfları

| Sınıf | Otorite | Örnek | Otomatik hydrate |
|---|---|---|---|
| Work state | Kanonik | aktif görev, lease, checkpoint | Evet, MUST |
| Evidence | Kanıt | test sonucu, source revision, receipt | Gerektiğinde |
| Observation | Ham olmayan gözlem | session close delta | Hayır |
| Candidate | Öneri | yeni karar/skill/failure adayı | Hayır |
| Active memory revision | Yardımcı kanonik bilgi | doğrulanmış domain kuralı | Politika ile |
| Projection | Türetilmiş görünüm | Obsidian notu | Kaynak değildir |
| External cache | Otoritesiz | Mem0 sonucu | Doğrulama sonrası |
| Raw transcript | Hassas geçici veri | tam konuşma | Asla otomatik değil |

---

## 8. Uygulama paketleri

# P0-A — Kapanış, projeksiyon ve görev handoff doğruluğu

## Amaç

“Tamamlandı” durumunun eski projeksiyon veya eksik receipt ile oluşmasını engelle;
ayrıca `zekam-girdi` → repo kökü aktif görev handoff’unu güvenli ve deterministik yap.

## Yapılacaklar

1. `zekam close`, work completion ve lease release yollarında ortak bir
   `ProjectionFreshnessReleaseGate` kullan.
2. Gate şu exact bağları doğrulasın:
   - source HEAD,
   - source tree digest,
   - migration head,
   - database revision digest,
   - active work revision,
   - projection receipt digest,
   - lifecycle receipt completeness.
3. `completed` + pending next-safe-action gibi durumları invariant ihlali say.
4. Stale root projection:
   - release’i bloklasın,
   - doctor’da görünür olsun,
   - güvenli ve idempotent projection refresh planı üretsin.
5. `.github/workflows/quality.yml` ve `.github/workflows/package-acceptance.yml`
   mevcut manuel `workflow_dispatch` davranışını korusun; otomatik `pull_request`/`push`
   tetiklemeleri kullanıcı açıkça istemedikçe eklenmesin.
6. `../zekam-girdi/AKTIF_GOREV.md` için bir handoff doğrulaması uygula:
   - dosya varlığı ve okunabilirliği,
   - görev formatı,
   - scope/güvenlik ihlali,
   - güncel HEAD ile çelişki,
   - aktif lease/work state ile uyum
   kontrol edilsin.
7. Doğrulama başarılıysa `../zekam-girdi/AKTIF_GOREV.md` kontrollü/atomic biçimde repo
   kökündeki `AKTIF_GOREV.md` üzerine kopyalansın; ardından kök dosya yaşayan aktif görev
   olarak güncel tutulsun. Doğrulama başarısızsa kökteki mevcut dosya korunmalı.
8. Tüm mutating CLI yollarında `MemoryAdmissionService.assert_mutating_admission`
   veya eşdeğer tek kapının gerçekten çağrıldığını contract/E2E testleriyle kanıtla.
9. SQLite minimum profilinin full continuity admission gibi davranamadığını negatif test et.

## Kabul ölçütleri

- Güncel olmayan projeksiyonla `close/completed/release` başarısız.
- Refresh sonrası aynı komut başarılı ve exact receipt üretir.
- CI manuel `workflow_dispatch` olarak kalır; otomatik tetikleyici bu görevde eklenmez.
- Geçerli `zekam-girdi/AKTIF_GOREV.md` doğrulama sonrası köke alınır ve kök görev güncel tutulur.
- Geçersiz/çelişkili görev girdisi kökteki aktif görevin üzerine yazamaz.
- Mutating komutların admission bypass testi yoktur; her yeni mutating yüzey ortak kapıyı
  kullanır.

---

# P0-B — Memory Learning Orchestrator ve durable compiler worker

## Amaç

Mevcut hook, lifecycle ledger, candidate compiler, PostgreSQL store ve worker’ı tek üretim
döngüsüne bağla.

## Önerilen sorumluluk

Yeni bir application servisi oluştur:

```text
src/zekam/application/memory_continuity_orchestrator.py
```

Bu servis:

- lifecycle event’i doğrular,
- immutable ledger’a append eder,
- event türüne göre hydration/checkpoint/close planı üretir,
- compiler outbox/job kaydı açar,
- watermark ve idempotency anahtarı belirler,
- sonuç receipt’ini kaydeder,
- projection catch-up gereksinimini işaretler,
- gap/recovery durumunu görünür kılar.

## Hook bağlantısı

`memory_hooks.py` içindeki varsayılan placeholder handler’lar:

- doğrudan dosya yazmayacak,
- doğrudan model çağırmayacak,
- kanonik memory terfi ettirmeyecek,
- yalnız `MemoryContinuityOrchestrator` komutunu çalıştıracak.

Tetikleyici matrisi:

| Hook | Zorunlu işlem |
|---|---|
| `session_start` | exact hydration oluştur, receipt yaz, mutation admission’dan önce tamamla |
| `checkpoint` | bounded structured delta kaydet |
| `pre_compaction` | checkpoint + compaction boundary receipt |
| `session_close` | final checkpoint + close observation + compiler enqueue |
| `compact` sonrası | önceki checkpoint bağını doğrula, gap varsa fail-closed |
| client crash/idle | bounded orphan/gap gözlemi, sessiz “completed” yok |

## Compiler job

Mevcut scheduler/worker yapısını genişlet:

- yeni job: `memory-candidate-compile`,
- önerilen interval: `5m`,
- overlap: `skip`,
- misfire: `run-once`,
- close/pre-compaction event’i aynı işi hemen enqueue edebilir,
- schedule, kaçırılmış veya yarım kalmış işleri catch-up eder.

Job davranışı:

1. Watermark’tan sonraki eligible lifecycle/observation kayıtlarını claim et.
2. Source sırasını deterministik kur.
3. Policy ve sınıflandırmayı uygula.
4. Deterministik extractor ile temel adayları üret.
5. Model extractor gerekiyorsa ayrı provider-call job kullan; DB transaction açıkken
   provider çağırma.
6. Model sonucunu untrusted schema-bound proposal olarak parse et.
7. Mevcut `MemoryCandidateCompiler.compile_batch` akışına ver.
8. candidate/conflict/quarantine/hygiene ve compiler receipt’lerini tek kısa transaction’da
   kaydet.
9. Watermark yalnız terminal receipt sonrası ilerlesin.
10. Crash, claim sonrası receipt yokluğu veya digest mismatch durumunda
    `recovery-required` üret; sessiz retry yapma.

## Aday üretim kaynakları

Eligible:

- explicit user statement receipt,
- Work/Run/Research/Verification evidence,
- trusted imported record,
- accepted lifecycle structured delta,
- doğrulanmış failure/root-cause evidence.

Tek başına eligible olmayan:

- model cevabı,
- ham transcript,
- private reasoning,
- doğrulanmamış tahmin,
- dış cache sonucu,
- kaynaksız web metni.

## Kabul ölçütleri

- Aynı event iki kez işlendiğinde tek candidate/receipt oluşur.
- Out-of-order olay deterministik biçimde conflict/gap üretir.
- Worker restart sonrası watermark doğru devam eder.
- Provider kapalıyken deterministik akış çalışır.
- Provider açıkken payload redacted, bounded ve receipt’li olur.
- Secret/PII/raw-transcript candidate active yola giremez.
- `compiler-shadow-report` backlog, lag, quarantine ve son receipt’i gösterir.
- Shadow modda active memory değişmez.

---

# P0-C — İkinci gerçek istemci/harness

## Amaç

OpenCode dışında en az bir gerçek istemcinin aynı lifecycle sözleşmesini eksiksiz
uyguladığını kanıtla.

## Seçim kuralı

1. Kurulu ve kullanılan Codex sürümünün lifecycle/hook/notification yüzeyini gerçek
   çalışma ortamında keşfet.
2. Exact contract version, event ordering ve local outbox mümkünse Codex’i uygula.
3. Codex bu garantileri vermiyorsa Claude Code’u incele ve uygula.
4. Hiçbiri exact sözleşmeyi sağlayamıyorsa “destekleniyor” etiketi verme;
   generic adapter pasif kalır ve gap raporlanır.
5. Seçimi `docs/adr/` altında kısa ADR ile kaydet.

## Zorunlu davranış

- content-free lifecycle payload,
- local durable outbox,
- idempotent drain,
- exact adapter version,
- session start/checkpoint/pre-compaction/close parity,
- secret/transcript reddi,
- offline çalışmada event kaybı yerine pending outbox,
- duplicate event replay güvenliği.

## E2E

Aynı sentetik iş akışı OpenCode ve ikinci harness ile çalıştırılır. Her ikisi için:

- aynı lifecycle invariant seti,
- aynı hydration sınıfları,
- aynı candidate batch digest,
- istemciye özel kimlik/provenance,
- duplicate üretmeyen replay

kanıtlanır.

---

# P1-D — Deterministik Obsidian projeksiyonu

## Amaç

İnsanın Zekam hafızasını rahat okuyabildiği, bağlantılar arasında gezebildiği ve Git’te
güvenle izleyebildiği bir Markdown görünümü üret.

## Profil ayrımı

En az iki profil:

### `private-local`

- yalnız aynı realm ve local güven sınırı,
- internal kayıtlar policy’ye göre bulunabilir,
- secret, raw transcript, credential ve private reasoning yine dışarı çıkmaz,
- varsayılan olarak Git ignore,
- yerel Obsidian kullanımı için.

### `public-safe`

- yalnız `public` sınıfı,
- portable relative linkler,
- absolute path, host, kullanıcı adı, e-posta, token, connection string yok,
- Git’e alınabilen profil.

Opsiyonel sonraki profil:

### `mobile-sanitized`

- minimal metadata,
- hassas alanları çıkarılmış,
- ayrı sync target,
- varsayılan kapalı.

## Realm kuralı

Her realm ayrı projection root kullanır. Örnek:

```text
ZEKAM_HOME/projections/obsidian/<realm>/<profile>/
```

Kişisel ve kurumsal realm aynı iCloud/Git hedefinde birleştirilmez.

## Dizin standardı

```text
00_HOME/
  INDEX.md
  BUGUN.md
  ACIK_ISLER.md

01_ACTIVE/
  PROJELER/
  CALISMA_OGELERI/

02_DECISIONS/
  YYYY/
  <decision-id>-<slug>.md

03_KNOWLEDGE/
  KAVRAMLAR/
  SISTEMLER/
  VARLIKLAR/

04_SKILLS/
  <skill-id>-<slug>.md

05_FAILURES/
  <failure-id>-<slug>.md

06_DAYLOGS/
  YYYY/
  YYYY-MM-DD.md

07_RELATIONS/
  ORPHANS.md
  CONFLICTS.md
  SUPERSEDED.md

90_ARCHIVE/

_META/
  README.md
  manifest.json
  projection-receipt.json
  source-map.json
  schema-version
```

## Frontmatter sözleşmesi

Her üretilmiş not en az şunları taşır:

```yaml
---
schema: zekam-obsidian-note/v1
id: <canonical-id>
realm: <realm-slug>
truth_class: active-memory
memory_class: semantic
status: active
classification: internal
source_refs:
  - <portable-evidence-ref>
source_digest: sha256:...
record_digest: sha256:...
projection_digest: sha256:...
confidence: 0.92
valid_from: 2026-08-27T00:00:00Z
valid_until:
last_verified_at: 2026-08-27T00:00:00Z
supersedes:
superseded_by:
generated_at: 2026-08-27T00:00:00Z
editable: false
---
```

Not gövdesinde:

- kısa özet,
- neden önemli,
- kaynak/kanıtlar,
- ilişkiler,
- geçerlilik/tazelik,
- conflict/supersession durumu,
- insan için sonraki güvenli eylem

bulunur.

## WikiLink üretimi

WikiLink modelin serbest çağrışımıyla değil:

1. canonical `memory_relation`,
2. exact project/work/evidence bağı,
3. approved high-confidence relation,
4. controlled alias map

üzerinden üretilir.

Belirsiz ilişki active link yapılmaz; `relation-candidate` olarak ayrı raporlanır.

## Atomic projection build

1. DB snapshot/source binding al.
2. Staging dizinine tüm notları üret.
3. Manifest, dosya digest’leri ve source-map oluştur.
4. Privacy scanner ve link checker çalıştır.
5. Aynı snapshot için byte-level determinism doğrula.
6. Projection receipt yaz.
7. Atomic directory swap yap.
8. Hata olursa önceki geçerli projeksiyon olduğu gibi kalır.

## CLI yüzeyi

Mevcut `zekam memory` grubuna aşağıdaki eşdeğer işlevler eklenir:

```text
zekam memory obsidian-plan
zekam memory obsidian-apply --plan-digest ... --uygula
zekam memory obsidian-verify
zekam memory obsidian-status
```

İsimler mevcut CLI konvansiyonuna göre uyarlanabilir; davranış değişmez:

- plan salt okunur,
- apply exact plan digest ve gerekli authorization ister,
- verify DB ↔ manifest ↔ file digest parity kontrol eder,
- status tazelik ve son receipt’i gösterir.

---

# P1-E — Daylog, karar, bilgi, skill ve failure döngüleri

## Daylog

Daylog kanonik otorite değildir. Aşağıdakilerden deterministik üretilir:

- o gün kapanan lifecycle session’ları,
- tamamlanan Work item’ları,
- alınan kanonik kararlar,
- oluşturulan candidate’lar,
- açık kalan işler,
- doğrulanmış failure/recovery kayıtları.

Format:

```text
Bugün tamamlananlar
Kararlar
Yeni öğrenilenler
Açık kalanlar
Riskler / engeller
Yarın için kanonik next-safe-action
Kaynak receipt’ler
```

Model daylog metnini güzelleştirebilir; fakat içerik listesi ve kaynak bağları deterministik
olmalıdır.

## Karar belleği

Bir karar active olması için:

- karar konusu,
- seçilen seçenek,
- reddedilen seçenekler,
- gerekçe,
- source revision/evidence,
- geçerlilik,
- owner/reviewer,
- supersession bağı

taşır.

“Model böyle önerdi” tek başına karar kanıtı değildir.

## Skill/procedural memory

Skill kendi kendine aktifleşmez.

Önerilen akış:

```text
tekrarlı başarılı execution evidence
→ skill candidate
→ fixture / replay
→ farklı reviewer
→ gerekiyorsa bağımsız verifier
→ exact promotion authorization
→ active procedural memory
→ generated skill projection
```

Skill kaydı:

- hangi problem sınıfını çözdüğü,
- precondition,
- deterministic adımlar,
- yasaklı durumlar,
- gerekli tool/capability sürümleri,
- test fixture,
- başarı ve hata metrikleri,
- rollback/recovery,
- kaynak evidence

taşır.

## Failure memory

Failure memory için occurrence key:

```text
normalized problem class
+ project capability digest
+ tool/adapter/version
+ root-cause digest
```

Root cause doğrulanmadıysa kayıt `hypothesis` kalır. Aynı hata geldiğinde Context Compiler:

- doğrulanmış failure/procedural memory’yi öne çıkarır,
- daha önce reddedilmiş yaklaşımı görünür biçimde yasaklı öneri yapar,
- ortam veya source revision farkını gösterir.

---

# P1-F — Yönetilen self-healing ve self-modification

## Yetki matrisi

| İşlem | Otonom | Review | Exact insan yetkisi | Yasak |
|---|---:|---:|---:|---:|
| Stale projeksiyonu yeniden üretme | ✓ |  |  |  |
| Index/cache rebuild | ✓ |  |  |  |
| Candidate üretme | ✓ |  |  |  |
| Secret şüphesini quarantine etme | ✓ |  |  |  |
| Gap/incident açma | ✓ |  |  |  |
| Relation önerme | ✓ | ✓ |  |  |
| Skill önerme | ✓ | ✓ |  |  |
| Candidate active terfi |  | ✓ | ✓ |  |
| Merge/supersede/revoke |  | ✓ | ✓ |  |
| Retention/purge |  | ✓ | ✓ |  |
| Migration uygulama |  |  | ✓ |  |
| Security/privacy policy değiştirme |  |  | ✓ |  |
| CI/branch protection değiştirme |  |  | ✓ |  |
| Uzak adapter/sync açma |  |  | ✓ |  |
| Modelin doğrudan canonical memory yazması |  |  |  | ✓ |
| Approval bypass |  |  |  | ✓ |
| Secret’i projection/Git’e çıkarma |  |  |  | ✓ |
| Force-push/history rewrite |  |  |  | ✓ |
| Root `AKTIF_GOREV.*` dosyasını elle otorite sayma |  |  |  | ✓ |

Doctor/repair yalnız önceden tanımlı, digest-bound ve testli recipe’leri otonom
uygulayabilir. Yeni repair recipe üretimi candidate/review sürecine girer.

---

# P1-G — Yüksek riskli form ve belge otomasyonu koruması

## Amaç

Videodaki “vault’tan form doldurma” kabiliyetini Zekam’a güvenli bir reusable primitive
olarak kazandırmak; otomatik gönderim sistemi kurmak değil.

## Ortak `FieldEvidence` sözleşmesi

Her alan için:

```text
field_name
normalized_value
display_value
source_ref
source_digest
source_revision
extracted_at
confidence
classification
validation_rules
status = verified | conflicting | missing | expired | prohibited
```

## Kurallar

- `missing`, `conflicting`, `expired` veya `prohibited` alan doldurulmaz.
- Bir model değeri uyduramaz veya “makul varsayım” kullanamaz.
- Form doldurulmadan önce read-only preview üretilir.
- Preview:
  - doldurulacak alan,
  - değer,
  - kaynak,
  - confidence,
  - boş bırakma nedeni
  gösterir.
- Resmi/finansal/hukuki/kurumsal belgede insan onayı zorunludur.
- Varsayılan davranış `fill-only`; `submit/send` kapalıdır.
- Submit için ayrı exact authorization ve terminal receipt gerekir.
- CAPTCHA, MFA, imza, ödeme veya hukuki beyan otomatik bypass edilmez.
- Sonuç kanıtı ekran görüntüsüne değil mümkünse yapılandırılmış response/receipt’e bağlanır.
- Hassas alanlar log/projection’a maskeli düşer.

## Negatif testler

- Kaynaksız vergi numarası uydurulmaz.
- Eski adres current sayılmaz.
- İki kaynak çelişiyorsa alan boş kalır.
- Preview olmadan submit mümkün değildir.
- Fill yetkisi submit yetkisi sayılmaz.
- Secret/public-safe projection’a sızmaz.

---

# P1-H — Legacy bellek dokümanları ve Mem0

Mevcut:

- `bellek/BELLEK_MIMARISI_MEM0_UYARLAMASI.md`
- `bellek/BELLEK_YASAM_DONGUSU_VE_HIJYEN.md`
- `bellek/MEMORY_ENGINE_PORTU.md`

belgeleri değerlidir; fakat elle güncellenen paralel runtime hakikati olmamalıdır.

Yapılacak:

1. Belgeleri ADR/reference statüsüne getir.
2. Uygulanan sözleşmeler için güncel source path ve schema revision ekle.
3. Runtime durum, backlog veya current policy bilgisi içeriyorsa bunu üretilmiş projeksiyona
   taşı.
4. Çelişen veya eski bölüm varsa silme yerine `superseded-by` bağıyla arşivle.
5. Mem0 adapter’ını bu görevin P0 kapsamına alma.
6. Yalnız `MemoryEngine` portunun capability/no-dual-authority testlerini koru.
7. Mem0 sonraki ayrı görevde, native engine tamamen çalışır durumdayken opsiyonel cache/index
   olarak eklenebilir.

---

# P2 — Gözlemlenebilirlik, kalite ve UX

## Zorunlu ölçümler

- lifecycle event sayısı ve eksik event oranı,
- hydration admission başarısı/blok nedeni,
- compiler backlog ve en eski kayıt yaşı,
- candidate üretim/dedupe/conflict/quarantine oranı,
- candidate review/accept/reject oranı,
- active memory kullanım ve verifier başarısı,
- projection freshness ve build süresi,
- broken link/orphan relation sayısı,
- public-safe privacy finding sayısı,
- provider call/redaction/budget kullanımı,
- ikinci harness parity sonucu,
- recovery-required job/claim/receipt sayısı.

## SLO/invariant

- Required lifecycle receipt completeness: `%100`
- Release anında projection mismatch: `0`
- Aynı idempotency key için duplicate active candidate: `0`
- Public-safe secret/PII finding: `0`
- Aynı snapshot için projection digest farklılığı: `0`
- Unknown field’in otomatik uydurulması: `0`
- Receipt’siz completed effect: `0`

Sayısal latency/backlog eşikleri gerçek shadow ölçümünden sonra policy’ye yazılır; bu görev
kanıtsız sabit performans rakamı uydurmaz.

---

## 9. Dosya düzeyi uygulama rehberi

Aşağıdaki liste hedef sorumlulukları gösterir. Güncel HEAD’de eşdeğer modül varsa yenisini
oluşturmak yerine onu genişlet.

### Değişmesi beklenen mevcut dosyalar

```text
.github/workflows/quality.yml
.github/workflows/package-acceptance.yml

src/zekam/application/memory_hooks.py
src/zekam/application/memory_continuity.py
src/zekam/application/client_lifecycle_bridge.py
src/zekam/application/memory_candidate_compiler.py
src/zekam/application/continuity_projection.py
src/zekam/application/worker.py

src/zekam/infrastructure/postgres/memory_continuity_repository.py

src/zekam/interfaces/cli/memory.py
src/zekam/interfaces/cli/worker.py

src/zekam/domain/scheduler.py

config/memory_continuity_policy.yaml
config/memory_routing_policy.yaml

AGENTS.md
00_BASLA.md
README.md
```

Dokümanlar yalnız davranış gerçekten değiştiğinde güncellenir; koddan önce gerçek dışı
özellik ilan edilmez.

### Oluşması muhtemel yeni modüller

```text
src/zekam/application/memory_continuity_orchestrator.py
src/zekam/application/memory_compiler_composition.py
src/zekam/application/obsidian_projection.py
src/zekam/application/high_risk_autofill_guard.py

src/zekam/infrastructure/clients/<selected_harness>_lifecycle.py

docs/adr/ADR-xxxx-second-lifecycle-harness.md
docs/architecture/MEMORY_LEARNING_LOOP.md
docs/architecture/OBSIDIAN_PROJECTION.md
```

### Migration kuralı

Yeni migration yalnız mevcut `0055/0056` şemasında gerçekten eksik kalıcı alan/tablo/index
varsa eklenir.

- Önce schema diff çıkar.
- Mevcut tabloyla çözülebilen şey için migration yazma.
- Yeni migration forward-only, transactional ve fresh/upgrade DB testli olmalı.
- Migration head, code, policy, projection receipt ve component stamp birlikte güncellenmeli.
- Down migration zorunlu değilse bile operational rollback planı zorunludur.

---

## 10. Veri sözleşmeleri

### 10.1 Lifecycle event

```yaml
schema: zekam-lifecycle-event/v2
event_id: <uuid7>
realm_id: <uuid>
project_id: <uuid>
work_item_id: <uuid-or-null>
session_id: <portable-session-id>
client_kind: opencode
client_version: <exact-version>
adapter_contract: lifecycle-events-v2
event_type: session_start
occurred_at: <timezone-aware>
source_head: <git-sha-or-null>
payload_digest: sha256:...
idempotency_key: sha256:...
classification: internal
payload:
  checkpoint_ref: <portable-ref-or-null>
  changed_work_item_ids: []
  evidence_refs: []
  decision_refs: []
  failure_refs: []
```

Payload şu içerikleri taşımaz:

- transcript,
- prompt,
- response,
- chain-of-thought/private reasoning,
- bearer/token/password/secret,
- tam dosya içeriği,
- kontrolsüz stack trace,
- absolute sensitive path.

### 10.2 Memory candidate

```yaml
schema: zekam-memory-candidate/v1
candidate_id: <uuid7>
memory_class: semantic
scope:
  realm_id: <uuid>
  project_id: <uuid>
  work_item_id:
subject_keys: []
summary: <bounded-sanitized-text>
evidence_refs: []
source_revisions: []
producer:
  kind: deterministic-extractor
  model:
classification: internal
confidence: 0.0
status: candidate
conflicts_with: []
supersedes: []
idempotency_key: sha256:...
record_digest: sha256:...
```

### 10.3 Compiler receipt

```yaml
schema: zekam-memory-compiler-receipt/v1
batch_id: <uuid7>
watermark_before: <value>
watermark_after: <value>
source_digest: sha256:...
policy_digest: sha256:...
extractor_digest: sha256:...
input_count: 0
candidate_count: 0
deduped_count: 0
conflict_count: 0
quarantine_count: 0
provider_call_receipts: []
status: completed
receipt_digest: sha256:...
grants_authority: false
```

### 10.4 Projection receipt

```yaml
schema: zekam-obsidian-projection-receipt/v1
realm_id: <uuid>
profile: public-safe
source_head: <git-sha>
source_tree_digest: sha256:...
migration_head: 56
database_revision_digest: sha256:...
memory_snapshot_digest: sha256:...
manifest_digest: sha256:...
file_count: 0
privacy_scan_digest: sha256:...
link_check_digest: sha256:...
projection_digest: sha256:...
status: completed
grants_authority: false
```

---

## 11. Test planı

### Unit

- lifecycle payload schema/size/classification,
- transcript/secret key rejection,
- hook → orchestrator command mapping,
- idempotency key determinism,
- compiler ranked ordering,
- candidate dedupe/conflict/supersession,
- frontmatter rendering,
- WikiLink alias collision,
- field evidence status,
- self-modification authority matrix.

### Integration — PostgreSQL

- fresh DB migration,
- mevcut DB upgrade,
- event ledger append/replay,
- compiler watermark claim,
- store output atomicity,
- crash before/after receipt,
- recovery-required reconciliation,
- candidate promotion authorization,
- projection snapshot read consistency,
- realm/RLS isolation,
- SQLite minimum profile negative parity.

### E2E

Önerilen testler:

```text
tests/e2e/test_memory_learning_loop_runtime.py
tests/e2e/test_cross_harness_memory_continuity.py
tests/e2e/test_memory_compiler_worker_runtime.py
tests/e2e/test_obsidian_projection_roundtrip.py
tests/e2e/test_projection_freshness_release_gate.py
tests/e2e/test_high_risk_autofill_guard.py
```

Senaryolar:

1. Session start hydration olmadan mutation reddedilir.
2. Checkpoint → pre-compaction → close tam receipt zinciri üretir.
3. Compaction öncesi crash gap oluşturur; sessiz veri kaybı olmaz.
4. Aynı event replay duplicate candidate üretmez.
5. Worker crash ve restart watermark’ı bozmaz.
6. OpenCode ve ikinci harness aynı invariant setini geçer.
7. Shadow mode active memory değiştirmez.
8. Enforced mode yalnız exact authorization sonrası etkinleşir.
9. Obsidian projection aynı snapshot için byte-identical olur.
10. Atomic swap hatasında önceki projeksiyon korunur.
11. Stale root projection close/release’i bloklar.
12. Public-safe export secret/PII içermez.
13. Broken WikiLink build’i başarısız eder.
14. Unknown form alanı boş kalır.
15. Fill receipt submit yetkisi sağlamaz.
16. Receipt’siz effect `completed` olamaz.
17. PostgreSQL kapalıyken full continuity SQLite’a sessiz düşmez.

### Security

- prompt injection içeren imported note,
- path traversal,
- symlink escape,
- absolute path sızıntısı,
- token/credential regex,
- e-posta/PII,
- cross-realm retrieval,
- public/private profile karışması,
- malicious Markdown/frontmatter,
- forged projection receipt,
- stale policy digest,
- self-promotion by same actor/model,
- external cache authority escalation.

### CI kabulü

- `ruff`,
- format check,
- type check,
- unit/integration/E2E seçimi,
- fresh DB migration,
- upgrade DB migration,
- security/privacy suite,
- package build/install/smoke,
- projection determinism fixture

Bu kontroller yerelde zorunlu olarak çalıştırılmalıdır. GitHub Actions tarafında mevcut
manuel `workflow_dispatch` korunur; kullanıcı ayrıca isterse manuel CI da çalıştırılır.
Otomatik PR/push tetikleyicisi bu görevin kabul koşulu değildir.

---

## 12. Rollout

### Gate 1 — Shadow orchestration

- Hook → ledger → compiler worker çalışır.
- Candidate üretilir; active memory değişmez.
- Backlog, conflict ve quarantine ölçülür.
- Provider kapalı tutulur.

### Gate 2 — Projection

- `private-local` ve `public-safe` build edilir.
- Determinism/privacy/link testleri geçer.
- Obsidian yalnız bu üretilmiş dizini açar.
- Legacy elle yazılan bellek runtime otoritesi olmaktan çıkar.

### Gate 3 — Enforced lifecycle

- Session start hydration ve close receipt admission için zorunlu olur.
- Stale projection/gap fail-closed çalışır.
- OpenCode + ikinci harness parity geçer.

### Gate 4 — Kontrollü promotion

- Önce preference ve düşük riskli semantic candidate’lar.
- Sonra failure/procedural candidate’lar bağımsız review ile.
- Skill active etme ayrı authorization ister.

### Gate 5 — Opsiyonel provider/cache

- Gerçek ihtiyaç ve ölçüm varsa düşük maliyetli summarizer route’u açılır.
- Mem0 ayrı görev ve ayrı adapter olarak değerlendirilir.
- Native engine hiçbir aşamada Mem0’ya bağımlı olmaz.

---

## 13. Rollback ve recovery

- Feature mode exact authorization ile `enforced → shadow → disabled` alınabilir.
- Worker job definition pause edilebilir; ledger ve receipts silinmez.
- Obsidian projeksiyonu silinip kanonik snapshot’tan yeniden üretilebilir.
- Eski geçerli projection manifest’i atomic rollback için tutulur.
- Candidate’lar active’e otomatik çevrilmez; rollback’te candidate state korunabilir.
- Active memory değişikliği overwrite edilmez; superseding/revoke revision üretilir.
- External provider/cache devre dışı kaldığında native deterministic yol devam eder.
- Claim var, terminal receipt yoksa iş `recovery-required` olur; sessiz retry yasaktır.
- Git history rewrite/force-push rollback yöntemi değildir.
- DB restore ayrı backup/restore runbook ve restore drill receipt’i ister.

---

## 14. Tamamlanma ölçütleri

Görev yalnız aşağıdakilerin tümü sağlandığında tamamlandı sayılır:

- [ ] Güncel HEAD üzerinde boşluklar yeniden doğrulandı.
- [ ] Repo-root projection stale iken close/release bloklanıyor.
- [ ] Completed durum ile pending next-safe-action çelişkisi engelleniyor.
- [ ] CI mevcut manuel `workflow_dispatch` modunda korundu; otomatik tetikleyici eklenmedi.
- [ ] Tüm mutating yollar ortak lifecycle admission kapısından geçiyor.
- [ ] Hook’lar placeholder değil, orchestrator’a bağlı.
- [ ] Compiler event-driven enqueue + scheduled catch-up ile üretimde çalışıyor.
- [ ] Watermark, idempotency, crash recovery ve terminal receipt E2E kanıtlandı.
- [ ] OpenCode dışında bir gerçek harness exact lifecycle sözleşmesini geçti.
- [ ] Obsidian `private-local` ve `public-safe` profilleri deterministik üretiliyor.
- [ ] Obsidian dosyaları kanonik kaynak değil ve read-only/generated olarak işaretli.
- [ ] WikiLink’ler kanonik relation/source bağlarından üretiliyor.
- [ ] Daylog, decision, skill ve failure görünümleri oluşturuldu.
- [ ] Skill active etmek review + exact authorization istiyor.
- [ ] High-risk autofill guard unknown alanı boş bırakıyor ve submit’i ayrı yetkiye bağlıyor.
- [ ] Public-safe privacy scan sıfır finding ile geçiyor.
- [ ] Fresh DB ve upgrade DB testleri geçiyor.
- [ ] SQLite minimum profil full authority gibi davranamıyor.
- [ ] Full test suite ve package acceptance geçiyor.
- [ ] Bağımsız verifier builder’dan farklı model ve execution identity ile kanıt üretti.
- [ ] `zekam close` güncel projection/receipt üretip lease’i güvenli kapattı.
- [ ] Repo temiz veya yalnız açıkça belgelenmiş kullanıcı değişiklikleri var.
- [ ] Kullanıcı açıkça istemedikçe push yapılmadı.

---

## 15. Yasak kısa yollar

- Root `AKTIF_GOREV.md` dosyasını elle düzenleyip kanonik durumu taklit etmek.
- Obsidian’ı ayrı source-of-truth yapmak.
- Session transcript’ini “kolay olsun” diye Markdown’a dökmek.
- Model özetini doğrudan active memory yazmak.
- Hook içinde uzun provider çağrısı yapmak.
- DB transaction açıkken dış modele/Mem0’ya gitmek.
- Idempotency yerine “aynı görünüyorsa atla” yaklaşımı kullanmak.
- Secret/PII testlerini yalnız regex’e bırakmak.
- Manuel CI tercihinden dolayı testleri atlamak veya doğrulama kanıtı üretmemek.
- İkinci harness desteğini gerçek E2E olmadan ilan etmek.
- Aynı actor/model’in procedural skill üretip kendi kendine onaylaması.
- Public ve private projection’ı aynı manifest/sync target’a karıştırmak.
- Bilinmeyen form alanını varsayımla doldurmak.
- Hata sonrası history rewrite veya kullanıcı değişikliklerini silmek.
- Mevcut worker/scheduler yerine gereksiz paralel daemon kurmak.
- Mem0’yu canonical Work/Memory/Policy veritabanı yapmak.

---

## 16. Uygulayıcı ajanın çalışma protokolü

1. Gerçek repo kökünde olduğunu doğrula.
2. `git status --short --branch` ile kullanıcı değişikliklerini kaydet.
3. `AGENTS.md` ve `00_BASLA.md` başlangıç protokolünü uygula.
4. `zekam continue --project-key zekam` çalıştır; lease/checkpoint/doctor durumunu doğrula.
5. Önce `../zekam-girdi/AKTIF_GOREV.md` dosyasını dış görev girdisi olarak oku ve doğrula.
6. Girdi mevcut HEAD, scope, güvenlik ve aktif work state ile uyumluysa dosyayı kontrollü
   olarak repo kökündeki `AKTIF_GOREV.md` üzerine kopyala. Problem varsa kopyalama; mevcut
   kök görevi koru ve sorunu raporla.
7. Kopyalama sonrasında repo kökündeki `AKTIF_GOREV.md` yaşayan aktif görev kaydıdır; iş
   ilerledikçe bunu güncel tut. `AKTIF_GOREV.yaml`/DB state ile drift yaratma.
8. Güncel HEAD ile baseline farkını çıkar.
9. Her P0 boşluğu kod ve testle yeniden kanıtla; mevcut çözümü yeniden yazma.
10. Önce test/contract, sonra en küçük doğru uygulama, sonra E2E yap.
11. Security/privacy invariant’larını özellikten sonra değil aynı değişiklikte ekle.
12. Migration ancak gerçek schema ihtiyacı varsa yaz.
13. Her etkili adım için plan/claim/receipt/idempotency sözleşmesine uy.
14. Uzak çağrıları varsayılan kapalı tut.
15. Alakasız kullanıcı dosyalarını değiştirme, stash/reset/clean yapma.
16. Bağımsız verifier çalıştır.
17. Sonunda:
    - değişen dosyaları,
    - migration durumunu,
    - test komutlarını ve sonuçlarını,
    - CI durumunu,
    - projection/receipt digest’lerini,
    - bilinen açıkları,
    - rollback adımlarını
    raporla.
18. `zekam close --project-key zekam` ile kapanışı tamamla.
19. Açık kullanıcı yetkisi olmadan commit/push/PR oluşturma. CI otomasyonu veya GitHub
    workflow tetikleyici değişikliği de açık kullanıcı talebi olmadan yapılmaz.

---

## 17. Beklenen teslimatlar

1. Memory Learning Orchestrator uygulaması.
2. Durable compiler worker/scheduler entegrasyonu.
3. Hook → lifecycle → compiler → candidate → projection E2E akışı.
4. İkinci gerçek harness ve ADR.
5. Obsidian projection generator + CLI.
6. `private-local` ve `public-safe` profil politikaları.
7. Daylog/decision/knowledge/skill/failure projection’ları.
8. High-risk autofill guard.
9. Projection freshness release gate.
10. Manuel CI politikasının korunması ve yerel/bağımsız doğrulama kanıtları.
11. Unit/integration/E2E/security testleri.
12. Architecture ve operasyon dokümanları.
13. Bağımsız verification evidence.
14. Doğrulanmış `zekam-girdi` → kök `AKTIF_GOREV.md` handoff’u, güncel aktif görev ve close receipt.
15. Push yapılmadığını veya ayrıca yetkilendirilmişse exact push/PR receipt’ini belirten sonuç
    raporu.

---

## 18. Kısa yürütme promptu

```text
Zekam repo kökünde çalış. AGENTS.md ve 00_BASLA.md başlangıç protokolünü uygula;
git/lease/checkpoint/doctor durumunu doğrula. Önce ../zekam-girdi/AKTIF_GOREV.md
dosyasını oku ve güncel HEAD, scope, güvenlik ve aktif work state ile doğrula. Problem
yoksa bu dosyayı kontrollü olarak repo kökündeki AKTIF_GOREV.md üzerine kopyala ve bundan
sonra kökteki AKTIF_GOREV.md dosyasını yaşayan/güncel aktif görev olarak tut; problem
varsa kökteki mevcut görevin üzerine yazma ve problemi raporla. Mevcut Memory Continuity
Plane’i yeniden yazmadan hook → immutable lifecycle ledger → durable compiler worker →
candidate/review/promotion → deterministik Obsidian projection → bounded hydration
döngüsünü tamamla. PostgreSQL tek otorite, Obsidian salt okunur projeksiyon, model çıktısı
yalnız candidate, remote calls varsayılan kapalı olsun. Stale projection close/release’i
bloklasın ve OpenCode yanında ikinci gerçek harness E2E geçsin. CI mevcut manuel
workflow_dispatch modunda kalsın; pull_request/push otomatik CI tetikleyicilerini ben
açıkça istemedikçe etkinleştirme. Secret, PII ve raw transcript’i projection/Git’e
çıkarma; high-risk alanlarda kaynaksız değeri boş bırak ve submit’i ayrı exact yetkiye
bağla. Tüm yerel testleri, bağımsız verifier’ı ve zekam close kapanışını çalıştır; ben
açıkça yetkilendirmedikçe commit/push/PR oluşturma.
```
