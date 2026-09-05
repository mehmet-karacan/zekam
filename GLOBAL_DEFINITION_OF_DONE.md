# Zekam Global Definition of Done

Zekam yalnız aşağıdaki kabiliyetler gerçek kod, migration, test, evaluation, runbook ve
kanıtla sağlandığında kullanıma hazırdır. Demo, placeholder, TODO, sahte adapter veya yalnız
dokümantasyon tamamlanmış sayılmaz.

## Yeni mimari yeniden sınıflandırma durumu — 5 Eylül 2026

Bu listedeki 83 eski kabul işareti `AKTIF_GOREV.md` K-001, K-002 ve Bölüm 31.4
uyarınca sıfırlanmıştır. Her unchecked madde aksi açıkça belirtilmedikçe `pending`
sınıfındadır; eski PostgreSQL kanıtı yeni mimari için evidence değildir. Makine-okur
durum kaynağı `kalite/GLOBAL_DOD.yaml` da aynı nedenle 83/83 `pending` durumuna
alınmıştır. Mac'e özgü geçici kabul ve K-013 ertelemeleri bağlayıcı görev dosyasının
Bölüm 24 ve 33 kayıtlarında izlenir; bunlar global veya Windows kabulü üretmez.

Aşağıdaki legacy maddeler uygulanacak bir PostgreSQL gereksinimi değildir ve yeni
mimaride `removed-by-new-architecture` olarak sınıflandırılmıştır:

- PostgreSQL 18 migration temiz kurulum/upgrade/rollback kapısı,
- PostgreSQL durable queue/lease/lock kapısı,
- PostgreSQL+pgvector knowledge-index kapısı,
- Native PostgreSQL MemoryEngine kapısı,
- PostgreSQL acceptance ve migration-drift release kapıları.

Bu maddelerin yerel halefleri sırasıyla schema-v1 SQLite bootstrap/drift/recovery,
SQLite operational queue/claim/receipt/recovery, SQLite FTS5+sqlite-vec hibrit indeks,
SQLite local learning/memory ve local contract/chaos/DR testleridir. Haleflerin Mac
kanıtları mevcut olsa da global checkbox ancak aynı kriterin tüm zorunlu platform,
provider ve paket kanıtları tamamlanınca işaretlenir. Mem0 adapter maddesi yeni mimaride
ayrı kabul kanıtı bulunmadığı için `pending` kalır.

## A. Kurulum ve çekirdek

- [ ] Temiz Linux, macOS ve Windows/WSL kurulum akışı dokümante ve test edilmiştir.
- [ ] `zekam doctor` local operational/learning/model/benchmark/routing/analytics store,
  knowledge index, queue, clients ve policy durumunu doğrular. — `pending`
- [ ] Legacy server migration yolu kaldırılmıştır; schema-v1 local bootstrap, upgrade,
  drift ve recovery kapıları geçer. — `removed-by-new-architecture`
- [ ] Core kodu ile `ZEKAM_HOME` kullanıcı verisi fiziksel olarak ayrıdır.
- [ ] Proje source'ları kopyalanmadan logical binding ile yerinde okunabilir.
- [ ] Backup/restore ve disaster-recovery tatbikatı kanıtlanmıştır.

## B. Proje, doğal dil ve Work Graph

- [ ] Birden fazla bağımsız proje kayıt, alias, rebind ve capability profile ile yönetilir.
- [ ] `gpu projesi` gibi alias'lar exact project'e deterministik çözülür.
- [ ] Talep, defect, iş, task, subtask, decision ve research Work Item tipleri çalışır.
- [ ] Work revision ve event geçmişi append-only ve optimistic concurrency korumalıdır.
- [ ] `bugun ne islerimiz var`, `123 defect nerede kaldi` ve `nerede kaldik` sorguları kanonik state'ten yanıtlanır.
- [ ] Aynı iş idempotency key ile ikinci kez oluşturulmaz veya yürütülmez.

## C. Agent harness ve execution runtime

- [ ] Agentic her işte en az bir gerçek subagent enforcement seviyesinde zorunludur.
- [ ] Koordinatör subagent sayılmaz ve sabit global maksimum yoktur.
- [ ] DAG dependency ve resource conflict'e göre parallel/sequential route seçer.
- [ ] Local operational store durable queue, lease, heartbeat, fencing ve logical lock
  çalışır. — `reimplemented-and-verified` (Mac)
- [ ] Claim-before-effect ve terminal receipt-after-effect sözleşmesi uygulanır.
- [ ] Interrupted mutation `recovery-required` olur; sessiz retry yapılamaz.
- [ ] Strict Agent Result Envelope ve coordinator fan-in çalışır.
- [ ] Builder ve risk bazlı bağımsız verifier kimlikleri ayrıdır.
- [ ] Her meaningful step checkpoint ve continuity üretir.
- [ ] Entegre proje mutation'i yalniz bagli gercek source rootunda, tek-writer kilidiyla yapilir.

## D. Model envanteri, benchmark ve routing

- [ ] 20 Model ID bağımsız inventory record olarak import edilir.
- [ ] 19 teknik profil ile 20 kanonik kayıt farkı görünür provenance olarak korunur.
- [ ] Chat/code, embedding, reranker, Whisper, guardrail ve VL için farklı health/contract testleri vardır.
- [ ] En az beş tekrarlı genel benchmark ve proje özel benchmark uygulanır.
- [ ] Quality, reliability, verifier pass, latency, token, cost ve human correction ölçülür.
- [ ] Model health, quarantine, cooldown ve stale evidence kuralları çalışır.
- [ ] Codex <%40, Claude <%30 fallback kuralı yalnız güvenilir quota observation ile çalışır.
- [ ] Bilinmeyen kota tahmin edilmez.
- [ ] Model assignment açıklanabilir, digest-bound ve authority-free'dir.
- [ ] Bounded model deliberation/fusion süre, tur, token ve kanıt bütçesine uyar.

## E. Knowledge Plane

- [ ] BGE-M3 dense 1024 sürümlü ilk embedding profilidir.
- [ ] SQLite FTS5+sqlite-vec exact, lexical, dense, RRF ve opsiyonel reranker çalışır.
  — `reimplemented-and-verified` (Mac)
- [ ] DOCX heading/table, dijital/taranmış PDF, TXT/MD, PNG/JPEG/TIFF ve OCR ingestion çalışır.
- [ ] Orijinal artifact, normalized content, parser/chunker/embedding profile ve source revision korunur.
- [ ] Git URL, archive ve izinli directory taraması kod çalıştırmadan yapılır.
- [ ] AST/symbol chunking ve PL/SQL object ayrımı çalışır.
- [ ] Oracle/PostgreSQL metadata retrieval satır verisini varsayılan olarak toplamaz.
- [ ] Incremental re-index ve atomik active-version değişimi çalışır.
- [ ] PDF page, DOCX heading/block, OCR bbox, code path/symbol/line ve DB object citation üretilir.
- [ ] Golden retrieval evaluation Recall/MRR/nDCG ve no-answer oranlarını ölçer.
- [ ] Retrieval sonucu görev durumu veya yetki kaynağı olamaz.

## F. Bellek, öğrenme ve skills

- [ ] Local SQLite learning/memory store üretim çekirdeği olarak çalışır.
  — `reimplemented-and-verified` (Mac)
- [ ] Mem0 OSS adapter'ı opsiyonel ve aynı port arkasında çalışır.
- [ ] Working, episodic, semantic, procedural, preference ve failure memory ayrıdır.
- [ ] User/project/work/run/agent scope isolation testleri geçer.
- [ ] Exact+lexical+vector+entity+temporal memory retrieval açıklanabilirdir.
- [ ] Memory candidate bağımsız evidence ve verifier olmadan active olmaz.
- [ ] Duplicate, stale, conflict ve retention hygiene raporlanır; otomatik silme yapılmaz.
- [ ] Tekrarlı hatalar learning candidate olur; kanıt olmadan guideline/skill'e terfi etmez.
- [ ] Skill candidate, evaluation, approval, activate/deprecate/retire yaşam döngüsü çalışır.
- [ ] Memory veya Mem0 Work Graph durumunu, policy'yi veya authority'yi sahiplenmez.

## G. Araştırma ve uygulama teslimi

- [ ] Doğal dil araştırma isteği Work+Intent+ResearchQuestion'a dönüşür.
- [ ] En az bir researcher subagent, critic/synthesizer ve risk bazlı citation verifier çalışır.
- [ ] Kaynak snapshot, claim, contradiction ve exact citation kanıtı tutulur.
- [ ] Araştırma raporu Decision ve exact Plan'a ayrı review ile dönüşür.
- [ ] Implementation yalnız approved path/resource scope'ta worktree içinde yapılır.
- [ ] Testler Zekam tarafından bağımsız yeniden çalıştırılır.
- [ ] Patch, receipt ve rollback artifact'ı üretilir.
- [ ] Commit/push kullanıcı ve policy kurallarına uyar.

## H. Secret, policy ve güvenlik

- [ ] Secret Broker logical SecretRef çözer; plaintext model context'ine girmez.
- [ ] Prompt/log/vector/artifact/report/backup içinde secret sızıntı testleri geçer.
- [ ] Network default-deny ve outbound disclosure/authorization uygulanır.
- [ ] Path traversal, symlink escape, archive bomb ve source-root escape fail-closed'dur.
- [ ] Untrusted document/repository talimatları asla sistem komutu sayılmaz.
- [ ] Güvenli read-only sorgular gereksiz onay istemez.
- [ ] Network, secret, mutation, DB write, push ve destructive effect exact one-shot authorization gerektirir.
- [ ] Yetkilendirilmiş planın child step'leri drift yoksa anlamsız ikinci onay istemez.

## I. Scheduler, rapor ve dashboard

- [ ] `gelen-belgeler` watcher idempotent ingestion/research job üretir.
- [ ] Gece model health, project scan, memory hygiene, recovery ve araştırma işleri çalışır.
- [ ] Sabah genel ve proje bazlı rapor oluşur.
- [ ] Rapor model, kaynak, agent, token, cost, quota, failure, contradiction ve next action içerir.
- [ ] Dashboard Work Graph, queue, model, retrieval, memory ve scheduler projection'larını gösterir.
- [ ] Dashboard veya OpenTelemetry kanonik state olmaz.
- [ ] Obsidian/sinaps benzeri graph görünümü güvenli derived projection olarak sağlanır.

## J. Kalite, dokümantasyon ve release

- [ ] Unit, integration, security, property, concurrency, local-store acceptance ve E2E
  testleri desteklenen platformlarda geçer. — `reimplemented-and-verified` (Mac)
- [ ] Lint, format, type check, dependency audit, local schema drift ve dead-code kapıları
  geçer. — `reimplemented-and-verified` (Mac)
- [ ] İnsan okunur Türkçe belgeler gerçek kodla çelişmez.
- [ ] Commit mesajları Türkçe anlamlı ve ASCII-only'dir.
- [ ] Kritik akışlarda placeholder, mock-only production path veya ölü kod yoktur.
- [ ] Release manifesti, SBOM, checksum ve rollback talimatı vardır.
- [ ] Zekam tekil kimlik testi package, CLI, environment, home, schema ve DB alias'ı bırakmaz.

Global DoD'nin tek bir maddesi bile kanıtsızsa ürün `tamamlandi` sayılmaz.
