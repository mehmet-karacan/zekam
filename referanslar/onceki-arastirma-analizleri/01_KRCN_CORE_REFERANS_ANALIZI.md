# KRCN Core — Referans Analizi

## Değerlendirme özeti

KRCN Core, hedef sistem için en güçlü **yönetişim ve çalışma zamanı sözleşmesi** kaynağıdır. Project Capsule, Work Graph, agent queue, lease/fencing, exact plan, effect claim/receipt, model inventory/benchmark, continuity, memory hygiene ve skill lifecycle gibi başlıkları ayrıntılı biçimde tanımlar.

Yeni sistem KRCN kodunu olduğu gibi taşımamalı; bu sözleşmelerin özünü daha küçük bounded context’lerde, PostgreSQL merkezli ve ürün akışına göre yeniden kurmalıdır.

## Alınacak yetenekler

### 1. Project Capsule ve kaynak sınırı

- Her proje için taşınabilir kimlik, policy, work, knowledge, memory, runtime ve derived alanları.
- Kaynak repository’nin sisteme kopyalanmaması; read-only source binding ile yerinde okunması.
- Secret, fiziksel path, aktif lock ve lease bilgilerinin portable capsule dışında tutulması.
- İnsan tarafından okunabilir Markdown görünümünün kanonik veri değil projection olması.

### 2. Work Graph kanonikliği

- `request`, `defect`, `task`, `subtask`, `decision` ve ilişkilerin tek yetkili iş kaydı olması.
- Append-only olay/geçmiş, optimistic revision ve kanıt olmadan tamamlanamama.
- “Nerede kaldık?” ve “bugün ne var?” sorgularının vektör aramasından değil Work Graph ve runtime state’inden cevaplanması.
- Semantic index’in yalnız bulma ve context seçimi yapması.

### 3. Agent runtime güvenliği

- Queue item, attempt, lease, heartbeat, fencing token ve logical resource lock.
- Aynı işin birden fazla worker tarafından yazılmasını önleyen tek sahiplik.
- Parent/child path ile proje/task düzeyinde çatışma tespiti.
- Kesintiye uğramış write/network etkisinin sessiz retry yerine `recovery-required` olması.
- Read-only işlerin kontrollü replay edilebilmesi.

### 4. Effect Ledger

- Yan etki öncesi durable claim, sonuç sonrası terminal receipt.
- Aynı idempotency key için completed receipt varsa replay; claim var fakat receipt yoksa recovery.
- Modelin “başardım” demesinin tek başına yeterli olmaması.
- Queue/attempt/lease/fencing/plan/effect/result digest bağlarının doğrulanması.

### 5. Agent Result Envelope ve fan-in

- Tek biçimli sonuç durumları: `completed`, `partial`, `failed`, `blocked`, `recovery-required`, `abstained`.
- Bulgular, riskler, eksik adımlar, artifact/evidence referansları ve verifier verdict’i.
- Ham prompt, model çıktısı, secret, source text ve fiziksel path’in authoritative envelope’a girmemesi.
- Ana ajanın yalnız doğrulanmış child envelope ve receipt’lerden final durum üretmesi.

### 6. Adaptive routing

- Work classification, route, model assignment, admission, authorization ve delegation kararlarının ayrı tutulması.
- Hard gate’lerin model görüşünden önce uygulanması.
- Route seçenekleri: direct-read, single-worker, sequential DAG, parallel DAG, review-only, blocked, recovery-required.
- Aynı writable logical resource’a dokunan işlerin paralel çalıştırılmaması.
- Yeni routing policy’nin önce shadow mode’da mevcut kararlarla karşılaştırılması.

### 7. Model control plane

- Credential içermeyen model inventory.
- Health probe, quarantine/cooldown ve stale evidence kuralları.
- Proje capability profiline göre mikro benchmark suite.
- En az tekrarlı koşu, farklı worker/verifier modeli ve kalite–güvenilirlik–latency–token–maliyet ölçümü.
- Model kararının büyüklüğe göre değil doğrulanmış net değere göre verilmesi.
- Health sonucunun yetkinlik; benchmark sonucunun execution yetkisi sayılmaması.

### 8. Project capability profile

- Framework, dil, DB, test, UI ve module yeteneklerini yalnız dosya/manifest kanıtıyla çıkarma.
- Proje kodunu çalıştırmadan, symlink ve secret sınırlarıyla güvenli inspection.
- Spring 3.0 ile 3.5 veya aynı repodaki backend/frontend modüllerini ayrı capability scope’larında tutma.
- Eksik/partial profile ile model assignment yapmama.

### 9. Continuity ve memory hygiene

- Bounded Continuity Snapshot, digest-linked Work Journal ve authority taşımayan Finalized Handoff.
- Yeni model/CLI’nin transcript yüklemeden güncel doğrulanmış noktayı bulabilmesi.
- Stale, duplicate, conflict, unused ve retention adaylarını salt-okunur raporlama.
- Bellek değişikliğini ayrı gate ve revision ile yapma; geçmişi silmeme.
- Context effectiveness için required-evidence recall, kullanılan token, duplicate/stale oranı ve downstream başarı ölçümü.

### 10. Skill lifecycle

- `candidate → evaluated → approval-required → active → deprecated/retired` yaşam döngüsü.
- Tekrarlı kanıt, bağımsız evaluator/verifier ve farklı model kimlikleri.
- Skill’in kendini terfi ettirememesi; version, digest, test, izin ve rollback zorunluluğu.

## Yeniden tasarlanması gerekenler

- **Kod ölçeği:** Çok sayıda özellik ve büyük application/runtime modülleri yeni yapıya kopyalanmamalı. Her bounded context küçük application service ve açık portlara ayrılmalı.
- **Storage tercihi:** KRCN’deki proje başına SQLite queue/capsule yaklaşımının semantiği korunabilir; hedef mimaride kanonik concurrency ve tenant isolation PostgreSQL’de tutulmalı. SQLite yalnız offline/export projection seçeneği olabilir.
- **Approval yoğunluğu:** Her küçük mutation için ayrı kullanıcı etkileşimi yerine, risk sınıfı ve exact effect setine bağlı tek kullanımlık authorization bundle kullanılmalı.
- **Record çoğalması:** Aynı kavramı temsil eden karar, plan, route, assignment ve projection kayıtlarının sahipliği tek bir source-of-truth haritasında belirlenmeli.
- **Doküman–kod ayrımı:** Geniş spesifikasyonlar değerli olsa da “uygulandı” iddiası yalnız test, migration, runtime receipt ve acceptance evidence ile doğrulanmalı.
- **Yerel path taşınabilirliği:** Public/portable kayıtlarda logical refs; makine path’leri yalnız local binding tablosunda kalmalı.

## Al / Yeniden Tasarla / Alma

| Karar | İçerik |
|---|---|
| **Al** | Work Graph, source no-copy, capability profile, lease/fencing/lock, claim/receipt, result envelope, independent verifier, continuity, model inventory/health/benchmark, memory hygiene, skill lifecycle |
| **Yeniden tasarla** | PostgreSQL merkezli runtime, approval UX, bounded context sınırları, projection yerleşimi, model quota telemetry, dashboard API’leri |
| **Alma** | Mevcut repository yapısını topluca kopyalama, dokümana bakarak tamamlandı varsayma, her özelliği ilk sürüme alma, semantic index’i authority sayma |

## Hedef sistemdeki rolü

KRCN Core’dan alınacak bölüm **Control Plane Kernel** olmalıdır:

```text
Project Registry + Work Graph
        ↓
Policy / Authorization / Admission
        ↓
Task Planner + Runtime + Effect Ledger
        ↓
Model Decision + Agent Result Fan-in
        ↓
Continuity / Learning / Skill Governance
```

Context Vault’ın ingestion/retrieval işi veya ZEKAM’ın research-to-delivery ürün akışı bu çekirdeğin içine tekrar yazılmamalı; portlar üzerinden bağlanmalıdır.

## İncelenen başlıca repository belgeleri

- `README.md`
- `WORK-GRAPH.md`
- `AGENT-RUNTIME-QUEUE.md`
- `AGENT-RESULT-ENVELOPE.md`
- `ADAPTIVE-ROUTING.md`
- `MODEL-INVENTORY-HEALTH.md`
- `MODEL-BENCHMARK-RUNNER.md`
- `MODEL-DECISION-SERVICE.md`
- `PROJECT-CAPABILITY-PROFILE.md`
- `PROJECT-MODEL-BENCHMARK-SUITES.md`
- `CONTINUITY.md`
- `MEMORY-HYGIENE.md`
- `SKILL-LIFECYCLE.md`
- `RESEARCH-ORCHESTRATION.md`
- `SOURCE-CODE-RAG.md`
- `UNIFIED-RETRIEVAL.md`
