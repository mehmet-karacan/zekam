# ZEKAM — Referans Analizi

## Değerlendirme özeti

ZEKAM, hedef sistem için en temiz **doğal dil talebinden doğrulanmış değişikliğe giden dikey ürün akışı** örneğidir. KRCN Core kadar geniş değildir; fakat research → decision → plan → exact approval → worktree → verification → receipt zinciri daha doğrudan uygulanabilir bir temel sunar.

Yeni mimaride ZEKAM ayrı bir ikinci kontrol düzlemi olarak yaşamamalı. Güçlü ürün akışı, ortak Work Graph, runtime, model router ve Context Vault servislerini kullanan application workflow’larına dönüştürülmelidir.

## Alınacak yetenekler

### 1. Doğal dil amaç kabulü

- Türkçe veya İngilizce isteği Work + Intent + gerekirse ResearchQuestion kaydına dönüştürme.
- “Bunu araştır”, “GPU projesi”, “123 no’lu defect” gibi ifadelerde project alias ve konuşma konusu çözümleme.
- Belirsiz istekte konu uydurmama; güvenli biçimde eksik context veya proje seçimi üretme.
- Intent içinde `purpose`, `desired outcomes`, `success signals`, `non-goals`, `constraints`, `assumptions` tutma.
- Intent revision’larını append-only ve Work Item’a bağlı yönetme.

### 2. Kanıtlı araştırma workflow’u

- Yerel Markdown/transkript, Git repository ve güvenli HTTPS kaynaklarından araştırma.
- Kaynak snapshot, revision, content digest, locator ve exact excerpt kanıtı.
- Provider çağrısını DB transaction’ı dışında; normalize claim/report yazımını kısa ve atomik transaction’da yapma.
- Citation verifier’ın iddia, kaynak, excerpt, digest, question ve proje kapsamını doğrulaması.
- Çelişen iddiaları görünür tutma; ortalama alıp yeni “gerçek” üretmeme.

### 3. Decision ve Plan revision zinciri

- Araştırma raporundan ayrı, revision’lı Decision üretme.
- Her plan effect’inde action, path/resource, tool, network, data effect, dependency, beklenen çıktı, kabul ölçütü, test, evidence, risk ve rollback tanımlama.
- Bütün effect belgesini kanonik JSON + SHA-256 ile sabitleme.
- Plan değişince eski approval’ın otomatik geçersiz olması.

### 4. Exact ve tek kullanımlık approval

- Kullanıcının exact plan digest’ini vermesini yalnız o effect seti için onay sayma.
- Approval’ın expiry, revoke, consume ve replay kontrollerini atomik yürütme.
- Onayın başka plan, path, tool, network veya data scope’una taşınamaması.
- Düşük riskli/salt-okunur akışlar için aynı mekanizmanın gereksiz kullanıcı onayı üretmeyecek policy katmanıyla birleştirilmesi.

### 5. İzole uygulama

- Kaynak Git ağacını temiz ve read-only kabul etme.
- Değişikliği detached worktree/sandbox içinde uygulama.
- Değişen path’leri exact allowlist ile sınırlama; traversal/symlink reddi.
- Testleri agent’ın beyanına güvenmeden, shell’siz veya kontrollü executable allowlist ile sistemin yeniden çalıştırması.
- Patch’i artifact olarak saklama; hedef ağaca yalnız `git apply --check` ve doğrulama sonrasında taşıma.
- Commit/push işlemlerini ayrı ve açık bir karar olarak bırakma.

### 6. Continuity

- Context snapshot içinde güncel Work Item, Plan Revision, constraint, prohibited action, kalan token/maliyet/süre ve execution lineage.
- Checkpoint’te completed/pending effect partition’ının exact planla uyuşması.
- Handoff paketinin aktif approval veya lease taşımaması.
- Resume sırasında transcript yerine run/checkpoint/context/plan/approval zincirini doğrulama.

### 7. Ölçümlü loop ve öğrenme adayı

- Loop başlamadan metric, yön, hedef, minimum gelişim, iteration/stall/cost limitlerini sabitleme.
- Test/eval/benchmark/ledger dışındaki subjektif “daha iyi oldu” beyanını ilerleme saymama.
- Bağımsız verifier ile ölçüm.
- Tekrarlı hata/başarıyı evidence bağlı LearningCandidate’a dönüştürme.
- En az iki farklı gözlem ve bağımsız doğrulama olmadan TEST/EVAL/GUIDANCE/SKILL terfisi yapmama.

### 8. Katman sınırları ve persistence

- Domain’in framework/ORM/provider SDK bilmemesi.
- Application use-case ve portları; infrastructure adapter’ları; CLI/API interface’leri.
- PostgreSQL full profil ile realm/RLS; SQLite lite profilin açık ve sınırlı seçilmesi.
- Chat, rapor, index ve özetin kanonik state sayılmaması.

## Yeniden tasarlanması gerekenler

- **Tek repository/tek commit kanıtı:** Mevcut “tamamlandı” iddiaları gerçek runtime, test ve migration sonuçlarıyla yeniden doğrulanmalı; yalnız README kabul edilmemeli.
- **Eksik platform yetenekleri:** Model inventory/health/benchmark, gelişmiş scheduler, skill lifecycle, memory hygiene, unified retrieval ve dashboard ortak Control Plane modüllerinden gelmeli.
- **Orchestrator birleşimi:** ZEKAM’ın kendi job/runtime kavramları, KRCN’den alınan ortak queue–lock–claim–receipt çekirdeğiyle tekleştirilmeli.
- **Approval deneyimi:** Exact digest güvenliği korunmalı; ancak salt-okunur, günlük ve önceden policy ile yetkili işler otomatik yürüyebilmeli.
- **Context compiler:** Her model çağrısı için zorunlu context manifesti, selected/omitted gerekçeleri ve token bütçesi olmalı.
- **Provider adapters:** Codex’e özel ürün akışı değil, OpenCode/Claude/Codex ve gelecekteki CLI’lar için aynı execution contract kullanılmalı.

## Al / Yeniden Tasarla / Alma

| Karar | İçerik |
|---|---|
| **Al** | Natural-language intake, revision’lı intent, evidence research, citation verification, Decision/Plan effect digest, one-shot approval, detached worktree, allowlist, independent verification, checkpoint/handoff, measured loop |
| **Yeniden tasarla** | Ortak runtime ve model router entegrasyonu, risk bazlı approval, çoklu provider adapters, scheduler, context compiler ve artifact service |
| **Alma** | ZEKAM’ı ikinci source-of-truth yapmak, yalnız Markdown raporuyla state yönetmek, Codex’e ürün bağımlılığı, test edilmemiş tamamlandı iddiaları |

## Hedef sistemdeki rolü

ZEKAM’dan alınacak bölüm **Research-to-Delivery Application Flow** olmalıdır:

```text
Natural Language Goal
  → Work + Intent
  → Evidence Research
  → Decision
  → Exact Plan
  → Authorization
  → Sandboxed Implementation
  → Independent Verification
  → Patch + Receipt + Continuity
```

Bu akış, KRCN’den türetilen control-plane sözleşmelerini ve Context Vault bilgi servislerini kullanmalı; kendi paralel state, queue, memory veya retrieval sistemini oluşturmamalıdır.

## İncelenen başlıca repository belgeleri

- `README.md`
- `ANA_AMAC_YETENEK_IZLENEBILIRLIK_MATRISI.md`
- `NATURAL_LANGUAGE_GOAL_INTAKE.md`
- `EVIDENCE_RESEARCH_WORKFLOW.md`
- `INTENT_CONTEXT_LEARNING_AND_RESEARCH.md`
- `EXACT_APPROVAL_AND_CONTINUITY.md`
- `ADAPTIVE_ROUTER_AND_WORKER_RUNTIME.md`
- `MARKDOWN_REPORT_DELIVERY.md`
