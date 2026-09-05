# Zekam Güncel ADR Kararları

Bu özet `AKTIF_GOREV.md` içindeki bağlayıcı kararları tekrarlar; bağımsız authority
değildir. Ayrıntılı ölçüm ve riskler `docs/adr/` altındaki karar kayıtlarındadır.

## ADR-001 — Modüler monolit

**Karar:** Tek domain/application çekirdeği, ayrı ve bounded süreçler kullanılır.
**Neden:** Dağıtık transaction ve çift authority oluşmasını önler.

## ADR-002 — Yerel operational authority

**Karar:** Mac-first operational store CPython SQLite schema v1'dir. Queue, lease,
claim, receipt, work, session, registry ve policy state burada tutulur.
**Sınır:** Windows x64 kabulü K-013 kapsamında deferred durumdadır.

## ADR-003 — Yeniden üretilebilir knowledge index

**Karar:** Exact ve lexical arama SQLite FTS5, dense arama sqlite-vec ile sürümlü
generation içinde çalışır. Kaynak manifesti ve artifact bytes authority'dir; indeks
silinip yeniden üretilebilir.

## ADR-004 — Derived analytics

**Karar:** DuckDB yalnız immutable raw event/run artifact'larından yeniden kurulan
analytics projection'dır; operational mutation veya authority taşımaz.

## ADR-005 — Legacy veri kaynağı değildir

**Karar:** Eski server veritabanına bağlantı, migration, dump, export/import, ETL,
karşılaştırma veya yeni state üretmek için okuma yasaktır. Core Docker gerektirmez.

## ADR-006 — Agent harness core özelliğidir

**Karar:** DAG, lease, fence, lock, claim, receipt, checkpoint, recovery ve bağımsız
verifier sözleşmeleri provider veya istemciye bırakılmaz.

## ADR-007 — Agentic işte gerçek subagent

**Karar:** Koordinatörden ayrı en az bir child gerekir; aynı yazılabilir logical
resource'a yalnız bir builder atanır.

## ADR-008 — Provider-neutral model gateway

**Karar:** Exact model ID, provider, client, device, revision, quota observation ve
secret reference ayrı tutulur. Bilinmeyen bilgi tahmin edilmez.

## ADR-009 — Gerçek embedding profili

**Karar:** Mac'te sürümlü BAAI/bge-m3 1024 profilinin gerçek çıktısı kullanılır.
Feature hash semantic embedding sayılmaz; gerçek provider yoksa durum açıkça
`lexical-only-degraded` olur. Windows/OpenCode provider yolu mimaride korunur.

## ADR-010 — Yerel learning ve memory

**Karar:** Memory, failure, lesson ve skill lifecycle yerel SQLite learning store'da
candidate→review→active zinciriyle tutulur. Raw model/transcript çıktısı doğrudan
authority olamaz; Mem0 zorunlu değildir.

## ADR-011 — Exact authorization ve one-shot effect

**Karar:** Read-only işlem otomatik olabilir. Yan etki exact plan/scope/expiry,
claim-before-effect ve terminal receipt ister; receipt'siz claim sessiz retry edilmez.

## ADR-012 — External source koruması

**Karar:** Proje kaynağı logical binding ile yerinde okunur. Mutation yalnız açık yetki
ve tek-writer kilidiyle exact source rootunda yapılır; kopya, mirror veya detached
worktree üretilmez. Akıllı Kasa RAG fixture'ı salt okunurdur.

## ADR-013 — Tekil kimlik ve release sınırı

**Karar:** Package, CLI, environment ve home yüzeyinde yalnız Zekam kimliği kullanılır.
Commit mesajı Türkçe anlamlı ve ASCII-only'dir; push ayrı açık kullanıcı onayı ister.
