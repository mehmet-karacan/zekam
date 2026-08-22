# Z Control Plane — Nihai Uygulama Başlangıç Görevi

## Görev kimliği

| Alan | Değer |
|---|---|
| Durum | **ARAŞTIRMA İLE ONAYLANDI — UYGULAMAYA HAZIR** |
| Yeni repository | `z-control-plane` |
| Python package | `z_control_plane` |
| CLI | `zctl` |
| İlk branch | `feat/bootstrap-control-plane` |
| Mimari biçim | Modüler monolit + ayrı worker süreçleri |
| Kanonik veri | PostgreSQL 18 |
| Semantic projection | pgvector, ilk profil BGE-M3 dense 1024 |
| Artifact storage | `ArtifactStore` portu; local CAS + S3/MinIO adaptörü |
| Queue | PostgreSQL durable queue; Redis yalnız isteğe bağlı wake-up |
| Dil | İç kod/sözleşme İngilizce; kullanıcı görünümü ve raporlar Türkçe |

## 1. Görevin amacı

Sıfırdan, model/sağlayıcı/CLI bağımsız bir mühendislik kontrol düzlemi kur. Sistem farklı projelerdeki talep, defect, iş, araştırma, karar ve uygulama süreçlerini kanonik olarak izlemeli; modeli veya istemciyi değiştirince kaybolmamalı; agent/subagent çalışmasını kilit, lease, fencing, checkpoint, claim, receipt ve bağımsız verification ile yönetmelidir.

Bu görev eski repository’leri birleştirme veya kaynak kodlarını kopyalama görevi değildir. KRCN Core, ZEKAM ve Context Vault yalnız sözleşme, acceptance fixture, negatif test ve kanıt kaynağıdır.

## 2. Başlangıç protokolü

Uygulamayı alan her model/ajan/geliştirici şu sırayı izler:

1. Bu dosyayı, `00_NIHAI_ARASTIRMA_RAPORU.md`, `02_ILK_DIK_EKSEN_VE_BACKLOG.md`, `03_Z_PROJECT_MANIFEST.yaml` ve `04_BASLANGIC_ADR_KARARLARI.md` dosyalarını tamamen oku.
2. Yeni, boş `z-control-plane` repository’si aç; eski repo içine yazma.
3. Başlangıç commit ve branch bilgisini kayıt altına al.
4. Önce Faz 0 sözleşme/test iskeletini kur; gerçek model çağrısı ekleme.
5. Her write operation için plan → authorization → claim → effect → receipt zincirini koru.
6. Her aşamada negatif testleri pozitif testlerle birlikte yaz.
7. Doküman iddiasını test veya durable receipt olmadan tamamlanmış işaretleme.
8. Source binding ile bağlanan dış projeleri salt-okunur tut.
9. Kullanıcı verisini, secret veya mevcut proje kodunu otomatik silme/taşıma.
10. Haricî engel yoksa kapsamı küçük PR’larla ilerlet; büyük yeniden yazım yapma.

## 3. Değişmez kurallar

- PostgreSQL dışındaki hiçbir projection kanonik değildir.
- Ana model/koordinatör subagent sayılmaz.
- Agentic işlerde minimum bir subagent vardır.
- Deterministik exact-read/validation/projection işlemlerinde subagent zorunlu değildir.
- Sistem çapında sabit global subagent maksimumu yoktur; her run için hesaplanır.
- Aynı yazılabilir logical resource’a aynı anda tek builder sahip olur.
- High/critical mutation’da builder’dan bağımsız verifier zorunludur.
- Stale fencing token sonucu yayımlayamaz.
- Non-read effect, durable claim ve terminal receipt olmadan tamamlanamaz.
- Kaynak project root’a direct write her zaman reddedilir.
- SecretRef dışında secret değeri core contract’a giremez.
- Model output, kullanıcı onayı veya authoritative state değildir.
- Semantic retrieval exact ID/status/policy/authority kararını geçemez.
- Sessiz retry ve “başarılı varsay” yoktur.
- Commit/push ayrı yetki kapsamıdır; patch üretmek bunları yetkilendirmez.

## 4. Teknoloji tabanı

### Zorunlu

- Python `>=3.12`
- FastAPI (operation/API yüzeyi; ilk fazda minimal)
- Typer veya eşdeğer typed CLI
- Pydantic v2 + pydantic-settings
- SQLAlchemy 2.x
- Alembic
- PostgreSQL 18
- pgvector
- pytest + Hypothesis
- Ruff + mypy/pyright
- OpenTelemetry API/SDK
- JSON Schema (`schemas/`) ve canonical JSON digest yardımcıları

### İsteğe bağlı adaptör

- Redis: wake-up/cache; durable state değil
- S3/MinIO: artifact storage
- Vault: enterprise SecretBroker
- MCP: external tools/resources
- A2A: ileride external opaque agent federation
- rootless Podman/OCI: high-risk sandbox

### İlk sürümde çekirdek bağımlılık yapılmayacaklar

- LangChain/CrewAI/AutoGen benzeri agent framework’leri
- Celery/Temporal’in domain authority olması
- graph database
- ayrı vector database
- model-vendor SDK’sının domain katmanına sızması

## 5. Repository yapısı

```text
z-control-plane/
├── pyproject.toml
├── README.md
├── AGENTS.md
├── SECURITY.md
├── compose.yaml
├── .env.example
├── config/
│   ├── policies/
│   ├── benchmark-suites/
│   ├── project-profilers/
│   └── schemas/
├── schemas/
│   ├── work/
│   ├── runtime/
│   ├── models/
│   ├── research/
│   ├── knowledge/
│   └── governance/
├── src/z_control_plane/
│   ├── domain/
│   │   ├── common/
│   │   ├── projects/
│   │   ├── work/
│   │   ├── runtime/
│   │   ├── models/
│   │   ├── research/
│   │   ├── knowledge/
│   │   ├── memory_skills/
│   │   ├── governance/
│   │   └── operations/
│   ├── application/
│   │   ├── ports/
│   │   ├── projects/
│   │   ├── work/
│   │   ├── runtime/
│   │   ├── models/
│   │   ├── research/
│   │   ├── knowledge/
│   │   ├── memory_skills/
│   │   ├── governance/
│   │   └── operations/
│   ├── infrastructure/
│   │   ├── persistence/postgresql/
│   │   ├── queue/postgresql/
│   │   ├── artifacts/
│   │   ├── secrets/
│   │   ├── sandbox/
│   │   ├── retrieval/
│   │   ├── embeddings/
│   │   ├── model_adapters/
│   │   ├── source_adapters/
│   │   └── telemetry/
│   ├── interfaces/
│   │   ├── cli/
│   │   ├── api/
│   │   ├── workers/
│   │   └── scheduler/
│   └── bootstrap.py
├── migrations/
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── architecture/
│   ├── security/
│   ├── recovery/
│   └── evals/
└── docs/
    ├── adr/
    ├── architecture/
    ├── specifications/
    ├── operations/
    ├── progress/
    └── reports/
```

### Bağımlılık yönü

```text
interfaces → application → domain
                    ↑
             infrastructure
```

- Domain hiçbir framework, ORM, provider SDK, CLI veya filesystem import etmez.
- Application yalnız port kullanır.
- Infrastructure application portlarını uygular.
- Interface ürün kuralı tanımlamaz.
- Architecture testleri bu yönü CI’da zorunlu kılar.

## 6. PostgreSQL logical schema’ları

```text
core       realm, actor, project, alias, source_binding, module, capability_profile
work       work_item, work_revision, work_relation, work_event,
           intent, intent_revision, decision, decision_revision,
           plan, plan_revision, authorization
runtime    task_plan, task_step, run, attempt, queue_item, lease,
           resource_lock, checkpoint, effect_claim, effect_receipt,
           agent_result, verification, workflow_receipt
models     provider, client, model, model_revision, capability,
           health_record, benchmark_suite, benchmark_result,
           quota_pool, quota_observation, price_catalog, model_assignment
research   research_question, source_snapshot, claim, evidence_ref,
           contradiction, synthesis, citation_verdict, research_report
knowledge  source, source_version, artifact, normalized_manifest,
           citation, ingestion_job, ingestion_event, evaluation_run
memory     memory_record, memory_revision, learning_candidate,
           learning_observation, skill_candidate, skill_evaluation,
           skill_lifecycle
security   policy, capability_grant, provider_assurance, secret_ref, audit_event
ops        schedule, inbox_item, report_job, backup_manifest, health_event
```

### Veri tabanı kuralları

- Kimlikler UUIDv7/ULID benzeri sortable ve portable olabilir; seçim ADR’de sabitlenir.
- Her mutable head’in immutable append-only revision/event geçmişi vardır.
- Cross-project ve cross-realm ilişkiler foreign key/constraint ile reddedilir.
- Public record’larda absolute path ve secret benzeri alanlar şema seviyesinde yasaktır.
- Canonical JSON digest, semantic-relevant tüm alanları kapsar.
- Row-level security ilk sürümde en az realm/project tablolarına uygulanır ve `FORCE ROW LEVEL SECURITY` ile doğrulanır.
- Derived index tabloları authority alanı taşımaz.

## 7. Ana domain kayıtları

### Project

```text
project_id, realm_id, slug, display_name, status, revision
aliases[], source_bindings[], capability_profile_ref
created_at, updated_at, record_digest
```

Alias çözümü confidence ile değil, açık candidate listesi ve ambiguity state’i ile çalışır. Bir ifade iki projeye uyuyorsa seçim ister; yanlış projeyi tahmin etmez.

### Work Item

Türler:

```text
request | defect | task | subtask | decision | research | operation
```

Durumlar:

```text
proposed → ready → active → verifying → completed
                    ↘ blocked ↗
proposed/ready/active/blocked/verifying → cancelled
completed/cancelled → archived
```

Reopen yeni görünür revizyondur.

### Intent

```text
purpose, desired_outcomes, success_signals, non_goals,
constraints, assumptions, acceptance_criteria
```

### Plan Revision

Her step:

```text
step_id, operation, dependencies, access(read/write), resources,
tools, network, data_categories, expected_output, acceptance,
verification, risk, rollback, budgets
```

### Authorization

```text
authorization_id, plan_revision_id, effect_digest, scope_digest,
actor_id, expires_at, state(approved|consumed|revoked),
provider_scope?, secret_refs?, authorization_digest
```

Tek kullanımlıdır ve run başlatılırken atomik tüketilir.

## 8. Runtime state machine’leri

### Run

```text
planned → queued → running → verifying → completed
                     │          └→ failed
                     ├→ blocked
                     ├→ partial
                     └→ recovery-required
```

### Step/Attempt

```text
planned → ready → queued → leased → running
                                  ├→ waiting-input
                                  ├→ verifying
                                  ├→ completed
                                  ├→ partial
                                  ├→ failed
                                  ├→ blocked
                                  ├→ recovery-required
                                  └→ cancelled
```

### Queue/lease

- Claim transaction’ı `SKIP LOCKED` ile queue row seçer.
- Claim ile attempt ve lease aynı transaction’da oluşur.
- Her yeni lease monoton fencing token üretir.
- Heartbeat exact owner digest + fencing token + unexpired lease ister.
- Expired read-only attempt idempotency policy’ye göre requeue edilebilir.
- Pending write/network claim receipt’sizse automatic retry yapılmaz.

### Effect ledger

```text
Validation Gate → Effect Claim → external/internal effect → Terminal Receipt
```

Receipt durumları:

```text
completed | failed | cancelled | indeterminate
```

`indeterminate`, `recovery-required` üretir.

## 9. Logical resource lock sözleşmesi

Logical resource’lar machine path değil portable referanstır:

```text
project:<project-id>
work:<project-id>:<work-id>
path:<project-id>:<relative-posix-path>
source:<project-id>:<binding-id>:<relative-posix-path>
database:<project-id>:<connection-ref>:<object-ref>
provider:<quota-pool-id>
artifact:<artifact-id>
```

- `read/read` uyumludur.
- `read/write` ve `write/write` çakışır.
- Parent/child path write çakışır.
- Project write, o projedeki bütün alt resource’larla çakışır.
- Aynı writable scope iki builder’a verilmez.
- Lock acquisition sırası deterministiktir; deadlock’ı azaltmak için canonical sort kullanılır.

## 10. Subagent policy

```yaml
execution_policy:
  deterministic_operations:
    min_subagents: 0
  agentic_operations:
    min_subagents: 1
    coordinator_counts_as_subagent: false
    fixed_global_max_subagents: null
    concurrency: computed_per_run
```

Orchestrator şu shape’lerden birini seçer:

```text
direct-deterministic
single-subagent
sequential-dag
parallel-dag
review-only
blocked
recovery-required
```

`parallel-dag` yalnız en az iki bağımsız, lock-çakışmasız ve bütçeye sığan node varsa seçilir. “Her zaman iki ajan” veya “mümkün olan maksimum ajan” kuralı yoktur.

## 11. Agent Result Envelope v1

İlk JSON Schema şu zorunlu alanları içerir:

```json
{
  "schema_version": 1,
  "result_id": "...",
  "project_id": "...",
  "work_item_id": "...",
  "run_id": "...",
  "step_id": "...",
  "attempt_id": "...",
  "role": "researcher|builder|verifier|critic|citation-verifier",
  "execution_identity": "...",
  "model_assignment_id": "...",
  "input_manifest_digest": "sha256:...",
  "source_revision_digest": "sha256:...",
  "status": "completed|partial|failed|blocked|recovery-required|abstained",
  "findings": [],
  "evidence_refs": [],
  "artifact_refs": [],
  "risks": [],
  "missing_requirements": [],
  "next_safe_actions": [],
  "effect_claim_ref": null,
  "effect_receipt_ref": null,
  "verifier_ref": null,
  "result_digest": "sha256:..."
}
```

Şema raw prompt, model response, chain-of-thought, secret ve absolute path kabul etmez.

## 12. Model adapter ve assignment

### Adapter portu

```python
class ModelExecutionAdapter(Protocol):
    def describe(self) -> AdapterDescriptor: ...
    def discover_models(self) -> list[ModelInventoryCandidate]: ...
    def health_probe(self, request: HealthProbeRequest) -> HealthProbeResult: ...
    def execute(self, request: ModelExecutionRequest) -> ModelExecutionHandle: ...
    def poll(self, handle: ModelExecutionHandle) -> ModelExecutionEventBatch: ...
    def cancel(self, handle: ModelExecutionHandle) -> CancelResult: ...
    def observe_usage(self, handle: ModelExecutionHandle) -> UsageObservation: ...
    def observe_quota(self, pool: QuotaPoolRef) -> QuotaObservation: ...
```

### İlk adaptör sırası

1. Fake/fixture adapter.
2. OpenCode CLI.
3. Codex CLI.
4. Claude CLI.
5. OpenAI-compatible HTTP.
6. Native API adaptörleri yalnız ihtiyaç varsa.

### Model assignment

Hard eligibility → deterministic score → fallback list → assignment digest.

Bir assignment şu evidence’ı bağlar:

- project capability/workload digest;
- inventory/health/benchmark revision;
- quota observation;
- data/security classification;
- context/token estimate;
- price/latency evidence;
- worker/verifier exclusion set;
- policy revision.

Model kararı authority vermez ve provider çağrısını başlatmaz.

## 13. Project capability profile

Sabit persona yerine proje kanıtı kullanılır:

```text
modules[]
technologies[]
frameworks[]
versions[]
databases[]
test_commands[]
build_systems[]
quality_gates[]
sensitive_boundaries[]
workload_profiles[]
evidence_refs[]
coverage: complete | partial-safe
profile_digest
```

- Kaynak kodu çalıştırmadan manifest/dosya kanıtından çıkarılır.
- Partial-safe profil model assignment için yeterli sayılmaz.
- Spring 3.0 ve Spring 3.5 farklı profile/revision olarak korunur.
- “Sen Java mimarısın” yerine `architecture-design + spring-version-X + module-Y` workload kullanılır.

## 14. Context ve continuity contract’ı

### Context Manifest

```text
required_records[]
selected_candidates[]
omitted_candidates[] + reason
constraints[]
prohibited_actions[]
authorization_summary
source_revision
estimated_tokens
budget
manifest_digest
```

Zorunlu kayıt bütçeyi aşıyorsa fail-closed.

### Continuity Packet

```text
project/work/run identities
current goal/status/step
completed/pending steps
verified decisions
risks/blockers
first exact reads
next safe action
source/work/run revisions
packet_digest
grants_authority: false
carries_active_lease: false
```

## 15. Knowledge Plane sınırı

Faz 7’ye kadar yalnız port ve identity kayıtları kurulur. Faz 7’de Context Vault aktif görevinin doğrulanmış yönü uygulanır:

- source/version/artifact;
- normalized content;
- parser/chunker/embedding profile;
- BGE-M3 dense 1024;
- exact/identifier/path/symbol;
- PostgreSQL FTS ve trigram;
- RRF;
- optional reranker;
- citation/provenance;
- golden evaluation ve controlled re-index.

`knowledge` hiçbir zaman Work state veya authorization döndürmez.

## 16. Security contract

### Source no-write

- Binding gerçek source root’u gösterir.
- Core source’u yerinde ve read-only açar.
- Mutating run için `Z_HOME/calisma-alanlari/<run>/<attempt>/` worktree oluşturulur.
- Patch source’a ancak exact plan + authorization + verification sonrasında uygulanabilir.
- Commit/push ayrıca ayrı scope ister.

### Secret Broker

```python
class SecretBroker(Protocol):
    def lease(self, ref: SecretRef, scope: SecretUseScope) -> SecretLease: ...
    def revoke(self, lease: SecretLeaseRef) -> None: ...
```

SecretLease public serialization içermez. Child boundary’de kısa ömürlü injection yapılır; prompt/context’e eklenmez.

### Prompt injection

- Source/document/model output untrusted data’dır.
- İçindeki talimatlar yetki, scope veya tool erişimi değiştiremez.
- Tool call her seferinde core policy tarafından complete mediation’dan geçer.
- Open-ended shell/tool varsayılan olarak yoktur; typed capability tercih edilir.

## 17. İnsan görünümü

Kullanıcı klasörü:

```text
Z_HOME/projeler/<slug>/
  proje.md
  talepler/<id>/
  defectler/<id>/
  isler/<id>/
  arastirmalar/<id>/
  kararlar/<id>/
  raporlar/
```

Bunlar readable projection/drop-zone’dur. Her Markdown dosyası kanonik ID/revision/digest başlığı taşır ve kullanıcıya kanonik kaynak olmadığını açıklar.

## 18. Fazlar ve kabul kapıları

### Faz 0 — Foundation

Teslimler:

- repository scaffold;
- pyproject/quality tools;
- ADR-001..009;
- compose PostgreSQL + object store;
- settings ve doctor;
- canonical JSON/digest/time/ID utilities;
- architecture tests;
- CI.

Kabul:

```text
ruff, type check, unit, architecture, migration upgrade/downgrade
zctl doctor
secret scanner
```

geçer. Gerçek model çağrısı yoktur.

### Faz 1 — Project + Work

Teslimler:

- core/work tabloları;
- project alias/source binding;
- Work Graph ve Intent revisions;
- readable projection;
- CLI project/work/today/resume.

Kabul:

- exact defect sorgusu vector olmadan çalışır;
- ambiguous alias fail-closed;
- source root değişmez;
- history append-only.

### Faz 2 — Runtime

Teslimler:

- task plan/DAG;
- PostgreSQL queue;
- leases/fencing/locks;
- checkpoints;
- effect ledger;
- result envelope;
- fake adapter ve verifier;
- subagent policy.

Kabul:

- duplicate idempotency;
- worker crash;
- stale fence;
- lock conflict;
- claim-without-receipt;
- partial/failed fan-in

recovery testleri geçer.

### Faz 3 — Models

Teslimler:

- inventory/health/quarantine;
- benchmark suite/runner;
- quota pools/observations;
- deterministic assignment;
- OpenCode, ardından Codex/Claude adapters.

Kabul:

- unsupported workload seçilmez;
- health/stale/quarantined model dışlanır;
- quota yüzdesi bilinmiyorsa uydurulmaz;
- verifier exclusion uygulanır;
- adapter output strict parse edilir.

### Faz 4 — Natural language + Context

Teslimler:

- `zctl ask`;
- intent classifier;
- project alias/defect resolution;
- context manifest;
- continuity packet.

Kabul:

- “gpu projesi 123 defect” tek kanonik işe bağlanır;
- model değişimi resume’ı bozmaz;
- context bütçesi açıklanabilir;
- secret/path sızmaz.

### Faz 5 — Evidence Research

Teslimler:

- research factory;
- minimum bir researcher subagent;
- source snapshot/claim/evidence;
- contradiction/citation verification;
- Türkçe report.

Kabul:

- evidence’sız claim doğrulanmış olmaz;
- missing mandatory source `partial/blocked` olur;
- aynı source kopyası bağımsız corroboration sayılmaz.

### Faz 6 — Sandboxed Delivery

Teslimler:

- Decision/Plan/Authorization;
- detached worktree;
- tek builder;
- validation/test runner;
- independent verifier;
- patch/receipt.

Kabul:

- plan dışı file reddi;
- stale source reddi;
- test fail → no apply;
- replay → no duplicate effect;
- commit/push yok.

### Faz 7 — Knowledge Plane

Teslimler Context Vault active taskıyla uyumludur; fakat temiz bounded context olarak yazılır.

Kabul:

- 1024 vector profile versioned;
- hybrid retrieval eval;
- citation;
- stale/re-index;
- no source execution;
- secret skip/redaction.

### Faz 8 — Memory/Skills/Scheduler

Kabul:

- learning promotion için iki farklı gözlem + verifier;
- skill activation exact approval;
- scheduled job scope/budget/receipt;
- sabah report kanıtlı.

### Faz 9 — API/Dashboard/Portability

Kabul:

- dashboard projection olarak çalışır;
- API aynı application use-case’lerini çağırır;
- backup/restore drill;
- active lease export edilmez.

## 19. İlk değiştirilecek/oluşturulacak dosyalar

```text
pyproject.toml
README.md
AGENTS.md
SECURITY.md
compose.yaml
.env.example
src/z_control_plane/domain/common/*.py
src/z_control_plane/bootstrap.py
src/z_control_plane/interfaces/cli/app.py
src/z_control_plane/infrastructure/persistence/postgresql/*.py
migrations/env.py
schemas/runtime/agent-result-envelope-v1.json
tests/architecture/test_dependency_direction.py
tests/contract/test_canonical_digest.py
tests/integration/test_migration_roundtrip.py
docs/adr/ADR-001-*.md ... ADR-009-*.md
```

## 20. Global Definition of Done

Proje aşağıdakilerin tümü kanıtlanmadan “tamamlandı” sayılmaz:

- Project/Work state model veya sohbetten bağımsızdır.
- Agentic iş minimum bir subagent kullanır.
- Paralellik lock ve DAG’a göre dinamiktir.
- Aynı write scope tek builder’dadır.
- Lease/fencing/idempotency/recovery testleri geçer.
- Effect claim/receipt zorunludur.
- Strict result envelope ve independent verification vardır.
- Model inventory/health/benchmark/quota routing çalışır.
- OpenCode modelleri keşfedilir ve raporlanır.
- Codex/Claude fallback continuity ile çalışır.
- Source root’a direct write yapılamaz.
- Secret model/context/vector/loga girmez.
- Natural-language project/defect resolution exact Work’e bağlanır.
- Evidence research kaynak ve citation taşır.
- Sandboxed implementation exact plan dışında değişiklik yapmaz.
- Knowledge Plane BGE-M3 1024 hybrid retrieval ve re-index uygular.
- “Bugün ne var?” kanonik Work/Schedule kaydından cevaplanır.
- Gece işleri scope/budget/receipt ile denetlenir.
- Backup/restore ve operasyon runbook’ları doğrulanır.
- Dokümantasyon gerçek test/receipt ile uyumludur.

## 21. İlerleme kaydı

Bu bölüm her çalışma oturumunda güncellenir:

```text
Son güncelleme:
Çalışan istemci/model:
Koordinatör execution identity:
Subagent execution identity/identities:
Repository/branch:
Son commit:
Aktif faz:
Aktif Work ID:
Tamamlanan son step:
Çalıştırılan testler:
Test sonucu:
Effect claim/receipt:
Bilinen engeller:
Recovery durumu:
Bir sonraki exact adım:
```
