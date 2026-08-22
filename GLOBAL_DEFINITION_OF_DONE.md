# Zekam Global Definition of Done

Zekam yalnız aşağıdaki kabiliyetler gerçek kod, migration, test, evaluation, runbook ve
kanıtla sağlandığında kullanıma hazırdır. Demo, placeholder, TODO, sahte adapter veya yalnız
dokümantasyon tamamlanmış sayılmaz.

## A. Kurulum ve çekirdek

- [ ] Temiz Linux, macOS ve Windows/WSL kurulum akışı dokümante ve test edilmiştir.
- [x] `zekam doctor` DB, pgvector, object storage, queue, clients, models ve policy durumunu doğrular.
- [x] PostgreSQL 18 migration'ları temiz kurulum, upgrade ve rollback testlerinden geçer.
- [x] Core kodu ile `ZEKAM_HOME` kullanıcı verisi fiziksel olarak ayrıdır.
- [x] Proje source'ları kopyalanmadan logical binding ile yerinde okunabilir.
- [x] Backup/restore ve disaster-recovery tatbikatı kanıtlanmıştır.

## B. Proje, doğal dil ve Work Graph

- [x] Birden fazla bağımsız proje kayıt, alias, rebind ve capability profile ile yönetilir.
- [x] `gpu projesi` gibi alias'lar exact project'e deterministik çözülür.
- [x] Talep, defect, iş, task, subtask, decision ve research Work Item tipleri çalışır.
- [x] Work revision ve event geçmişi append-only ve optimistic concurrency korumalıdır.
- [x] `bugun ne islerimiz var`, `123 defect nerede kaldi` ve `nerede kaldik` sorguları kanonik state'ten yanıtlanır.
- [x] Aynı iş idempotency key ile ikinci kez oluşturulmaz veya yürütülmez.

## C. Agent harness ve execution runtime

- [x] Agentic her işte en az bir gerçek subagent enforcement seviyesinde zorunludur.
- [x] Koordinatör subagent sayılmaz ve sabit global maksimum yoktur.
- [x] DAG dependency ve resource conflict'e göre parallel/sequential route seçer.
- [x] PostgreSQL durable queue, lease, heartbeat, fencing ve logical lock çalışır.
- [x] Claim-before-effect ve terminal receipt-after-effect sözleşmesi uygulanır.
- [x] Interrupted mutation `recovery-required` olur; sessiz retry yapılamaz.
- [x] Strict Agent Result Envelope ve coordinator fan-in çalışır.
- [x] Builder ve risk bazlı bağımsız verifier kimlikleri ayrıdır.
- [x] Her meaningful step checkpoint ve continuity üretir.
- [x] Entegre proje mutation'ı yalnız detached worktree/sandbox içinde yapılır.

## D. Model envanteri, benchmark ve routing

- [x] 20 Model ID bağımsız inventory record olarak import edilir.
- [x] 19 teknik profil ile 20 kanonik kayıt farkı görünür provenance olarak korunur.
- [ ] Chat/code, embedding, reranker, Whisper, guardrail ve VL için farklı health/contract testleri vardır.
- [x] En az beş tekrarlı genel benchmark ve proje özel benchmark uygulanır.
- [x] Quality, reliability, verifier pass, latency, token, cost ve human correction ölçülür.
- [x] Model health, quarantine, cooldown ve stale evidence kuralları çalışır.
- [x] Codex <%40, Claude <%30 fallback kuralı yalnız güvenilir quota observation ile çalışır.
- [x] Bilinmeyen kota tahmin edilmez.
- [x] Model assignment açıklanabilir, digest-bound ve authority-free'dir.
- [x] Bounded model deliberation/fusion süre, tur, token ve kanıt bütçesine uyar.

## E. Knowledge Plane

- [x] BGE-M3 dense 1024 sürümlü ilk embedding profilidir.
- [x] PostgreSQL+pgvector, exact, FTS, alias/trigram, dense, RRF ve opsiyonel reranker çalışır.
- [ ] DOCX heading/table, dijital/taranmış PDF, TXT/MD, PNG/JPEG/TIFF ve OCR ingestion çalışır.
- [x] Orijinal artifact, normalized content, parser/chunker/embedding profile ve source revision korunur.
- [x] Git URL, archive ve izinli directory taraması kod çalıştırmadan yapılır.
- [x] AST/symbol chunking ve PL/SQL object ayrımı çalışır.
- [x] Oracle/PostgreSQL metadata retrieval satır verisini varsayılan olarak toplamaz.
- [x] Incremental re-index ve atomik active-version değişimi çalışır.
- [x] PDF page, DOCX heading/block, OCR bbox, code path/symbol/line ve DB object citation üretilir.
- [x] Golden retrieval evaluation Recall/MRR/nDCG ve no-answer oranlarını ölçer.
- [x] Retrieval sonucu görev durumu veya yetki kaynağı olamaz.

## F. Bellek, öğrenme ve skills

- [x] Native PostgreSQL MemoryEngine üretim çekirdeği olarak çalışır.
- [x] Mem0 OSS adapter'ı opsiyonel ve aynı port arkasında çalışır.
- [x] Working, episodic, semantic, procedural, preference ve failure memory ayrıdır.
- [x] User/project/work/run/agent scope isolation testleri geçer.
- [x] Exact+lexical+vector+entity+temporal memory retrieval açıklanabilirdir.
- [x] Memory candidate bağımsız evidence ve verifier olmadan active olmaz.
- [x] Duplicate, stale, conflict ve retention hygiene raporlanır; otomatik silme yapılmaz.
- [x] Tekrarlı hatalar learning candidate olur; kanıt olmadan guideline/skill'e terfi etmez.
- [x] Skill candidate, evaluation, approval, activate/deprecate/retire yaşam döngüsü çalışır.
- [x] Memory veya Mem0 Work Graph durumunu, policy'yi veya authority'yi sahiplenmez.

## G. Araştırma ve uygulama teslimi

- [x] Doğal dil araştırma isteği Work+Intent+ResearchQuestion'a dönüşür.
- [x] En az bir researcher subagent, critic/synthesizer ve risk bazlı citation verifier çalışır.
- [x] Kaynak snapshot, claim, contradiction ve exact citation kanıtı tutulur.
- [x] Araştırma raporu Decision ve exact Plan'a ayrı review ile dönüşür.
- [x] Implementation yalnız approved path/resource scope'ta worktree içinde yapılır.
- [x] Testler Zekam tarafından bağımsız yeniden çalıştırılır.
- [x] Patch, receipt ve rollback artifact'ı üretilir.
- [x] Commit/push kullanıcı ve policy kurallarına uyar.

## H. Secret, policy ve güvenlik

- [x] Secret Broker logical SecretRef çözer; plaintext model context'ine girmez.
- [x] Prompt/log/vector/artifact/report/backup içinde secret sızıntı testleri geçer.
- [x] Network default-deny ve outbound disclosure/authorization uygulanır.
- [x] Path traversal, symlink escape, archive bomb ve source-root escape fail-closed'dur.
- [x] Untrusted document/repository talimatları asla sistem komutu sayılmaz.
- [x] Güvenli read-only sorgular gereksiz onay istemez.
- [x] Network, secret, mutation, DB write, push ve destructive effect exact one-shot authorization gerektirir.
- [x] Yetkilendirilmiş planın child step'leri drift yoksa anlamsız ikinci onay istemez.

## I. Scheduler, rapor ve dashboard

- [x] `gelen-belgeler` watcher idempotent ingestion/research job üretir.
- [x] Gece model health, project scan, memory hygiene, recovery ve araştırma işleri çalışır.
- [x] Sabah genel ve proje bazlı rapor oluşur.
- [x] Rapor model, kaynak, agent, token, cost, quota, failure, contradiction ve next action içerir.
- [x] Dashboard Work Graph, queue, model, retrieval, memory ve scheduler projection'larını gösterir.
- [x] Dashboard veya OpenTelemetry kanonik state olmaz.
- [x] Obsidian/sinaps benzeri graph görünümü güvenli derived projection olarak sağlanır.

## J. Kalite, dokümantasyon ve release

- [x] Unit, integration, security, property, concurrency, PostgreSQL acceptance ve E2E testleri geçer.
- [x] Lint, format, type check, dependency audit, migration drift ve dead-code kapıları geçer.
- [x] İnsan okunur Türkçe belgeler gerçek kodla çelişmez.
- [x] Commit mesajları Türkçe anlamlı ve ASCII-only'dir.
- [x] Kritik akışlarda placeholder, mock-only production path veya ölü kod yoktur.
- [x] Release manifesti, SBOM, checksum ve rollback talimatı vardır.
- [x] Zekam tekil kimlik testi package, CLI, environment, home, schema ve DB alias'ı bırakmaz.

Global DoD'nin tek bir maddesi bile kanıtsızsa ürün `tamamlandi` sayılmaz.
