# Zekam Global Definition of Done

> **18 Ekim 2026 kapsam karari:** Bu belge genis tarihsel gereksinim envanteridir.
> Guncel teslimin tek yapilacaklar ve kabul listesi `AKTIF_GOREV.md` bolum 0.3'tur
> (Y-01..12). Zorunlu subagent, sabit model sayisi/cok modelli benchmark, otomatik
> evolution ve coklu platform/yayin hedefleri guncel teslimi bloke etmez.
> Asagidaki eski isaretler ve `kalite/GLOBAL_DOD.yaml` eski 83 kaydin tarihcesidir;
> kapsamdan cikarilan madde basarili/tamamlandi sayilmaz ve Work state degismez.
> Yeni buyruk veya ikinci aktif is listesi olarak uygulanmaz.


Asagidaki kabiliyetler onceki genis kabul kapsamidir; guncel kapanis icin aktif
gorevin 0.3 bolumundeki Y-12 uygulanir. Demo, placeholder veya yalniz dokumantasyon
calisan urun kaniti sayilmaz.

## Yeni mimari yeniden sınıflandırma durumu — 5 Eylül 2026

Bu listedeki 83 eski kabul işareti `AKTIF_GOREV.md` K-001, K-002 ve Bölüm 31.4
uyarınca sıfırlanmıştır. Her unchecked madde aksi açıkça belirtilmedikçe `pending`
sınıfındadır; eski PostgreSQL kanıtı yeni mimari için evidence değildir. Makine-okur
durum kaynağı `kalite/GLOBAL_DOD.yaml` da aynı nedenle 83/83 `pending` durumuna
alınmıştır. Eski platform kanıtları bağlayıcı görev dosyasının tarihsel kayıtlarında
izlenir; bunlar Windows kabulü üretmez.

Aşağıdaki legacy maddeler uygulanacak bir PostgreSQL gereksinimi değildir ve yeni
mimaride `removed-by-new-architecture` olarak sınıflandırılmıştır:

- PostgreSQL 18 migration temiz kurulum/upgrade/rollback kapısı,
- PostgreSQL durable queue/lease/lock kapısı,
- PostgreSQL+pgvector knowledge-index kapısı,
- Native PostgreSQL MemoryEngine kapısı,
- PostgreSQL acceptance ve migration-drift release kapıları.

Bu maddelerin yerel halefleri sırasıyla schema-v1 SQLite bootstrap/drift/recovery,
SQLite operational queue/claim/receipt/recovery, SQLite FTS5+sqlite-vec hibrit indeks,
SQLite local learning/memory ve local contract/chaos/DR testleridir. Global checkbox ancak
Windows hedefi için zorunlu platform, provider ve paket kanıtları tamamlanınca işaretlenir.
Mem0 adapter maddesi yeni mimaride
ayrı kabul kanıtı bulunmadığı için `pending` kalır.

## A. Kurulum ve çekirdek

- (Tarihsel referans) Temiz Linux ve Windows/WSL kurulum akışı dokümante ve test edilmiştir.
- (Tarihsel referans) `zekam doctor` local operational/learning/model/benchmark/routing/analytics store,
  knowledge index, queue, clients ve policy durumunu doğrular. — `pending`
- (Tarihsel referans) Legacy server migration yolu kaldırılmıştır; schema-v1 local bootstrap, upgrade,
  drift ve recovery kapıları geçer. — `removed-by-new-architecture`
- (Tarihsel referans) Core kodu ile `ZEKAM_HOME` kullanıcı verisi fiziksel olarak ayrıdır.
- (Tarihsel referans) Proje source'ları kopyalanmadan logical binding ile yerinde okunabilir.
- (Tarihsel referans) Backup/restore ve disaster-recovery tatbikatı kanıtlanmıştır.

## B. Proje, doğal dil ve Work Graph

- (Tarihsel referans) Birden fazla bağımsız proje kayıt, alias, rebind ve capability profile ile yönetilir.
- (Tarihsel referans) `gpu projesi` gibi alias'lar exact project'e deterministik çözülür.
- (Tarihsel referans) Talep, defect, iş, task, subtask, decision ve research Work Item tipleri çalışır.
- (Tarihsel referans) Work revision ve event geçmişi append-only ve optimistic concurrency korumalıdır.
- (Tarihsel referans) `bugun ne islerimiz var`, `123 defect nerede kaldi` ve `nerede kaldik` sorguları kanonik state'ten yanıtlanır.
- (Tarihsel referans) Aynı iş idempotency key ile ikinci kez oluşturulmaz veya yürütülmez.

## C. Agent harness ve execution runtime

- (Tarihsel referans) Agentic her işte en az bir gerçek subagent enforcement seviyesinde zorunludur.
- (Tarihsel referans) Koordinatör subagent sayılmaz ve sabit global maksimum yoktur.
- (Tarihsel referans) DAG dependency ve resource conflict'e göre parallel/sequential route seçer.
- (Tarihsel referans) Local operational store durable queue, lease, heartbeat, fencing ve logical lock
  çalışır. — `reimplemented-and-verified` (Windows hedefi)
- (Tarihsel referans) Claim-before-effect ve terminal receipt-after-effect sözleşmesi uygulanır.
- (Tarihsel referans) Interrupted mutation `recovery-required` olur; sessiz retry yapılamaz.
- (Tarihsel referans) Strict Agent Result Envelope ve coordinator fan-in çalışır.
- (Tarihsel referans) Builder ve risk bazlı bağımsız verifier kimlikleri ayrıdır.
- (Tarihsel referans) Her meaningful step checkpoint ve continuity üretir.
- (Tarihsel referans) Entegre proje mutation'i yalniz bagli gercek source rootunda, tek-writer kilidiyla yapilir.

## D. Model envanteri, benchmark ve routing

- (Tarihsel referans) 20 Model ID bağımsız inventory record olarak import edilir.
- (Tarihsel referans) 19 teknik profil ile 20 kanonik kayıt farkı görünür provenance olarak korunur.
- (Tarihsel referans) Chat/code, embedding, reranker, Whisper, guardrail ve VL için farklı health/contract testleri vardır.
- (Tarihsel referans) En az beş tekrarlı genel benchmark ve proje özel benchmark uygulanır.
- (Tarihsel referans) Quality, reliability, verifier pass, latency, token, cost ve human correction ölçülür.
- (Tarihsel referans) Model health, quarantine, cooldown ve stale evidence kuralları çalışır.
- (Tarihsel referans) Codex <%40, Claude <%30 fallback kuralı yalnız güvenilir quota observation ile çalışır.
- (Tarihsel referans) Bilinmeyen kota tahmin edilmez.
- (Tarihsel referans) Model assignment açıklanabilir, digest-bound ve authority-free'dir.
- (Tarihsel referans) Bounded model deliberation/fusion süre, tur, token ve kanıt bütçesine uyar.

## E. Knowledge Plane

- (Tarihsel referans) Yerel multilingual-e5-small ONNX INT8 dense 384 sürümlü embedding profilidir.
- (Tarihsel referans) SQLite FTS5+sqlite-vec exact, lexical, dense, RRF ve opsiyonel reranker çalışır.
  — `reimplemented-and-verified` (Windows hedefi)
- (Tarihsel referans) DOCX heading/table, dijital/taranmış PDF, TXT/MD, PNG/JPEG/TIFF ve OCR ingestion çalışır.
- (Tarihsel referans) Orijinal artifact, normalized content, parser/chunker/embedding profile ve source revision korunur.
- (Tarihsel referans) Git URL, archive ve izinli directory taraması kod çalıştırmadan yapılır.
- (Tarihsel referans) AST/symbol chunking ve PL/SQL object ayrımı çalışır.
- (Tarihsel referans) Oracle/PostgreSQL metadata retrieval satır verisini varsayılan olarak toplamaz.
- (Tarihsel referans) Incremental re-index ve atomik active-version değişimi çalışır.
- (Tarihsel referans) PDF page, DOCX heading/block, OCR bbox, code path/symbol/line ve DB object citation üretilir.
- (Tarihsel referans) Golden retrieval evaluation Recall/MRR/nDCG ve no-answer oranlarını ölçer.
- (Tarihsel referans) Retrieval sonucu görev durumu veya yetki kaynağı olamaz.

## F. Bellek, öğrenme ve skills

- (Tarihsel referans) Local SQLite learning/memory store üretim çekirdeği olarak çalışır.
  — `reimplemented-and-verified` (Windows hedefi)
- (Tarihsel referans) Mem0 OSS adapter'ı opsiyonel ve aynı port arkasında çalışır.
- (Tarihsel referans) Working, episodic, semantic, procedural, preference ve failure memory ayrıdır.
- (Tarihsel referans) User/project/work/run/agent scope isolation testleri geçer.
- (Tarihsel referans) Exact+lexical+vector+entity+temporal memory retrieval açıklanabilirdir.
- (Tarihsel referans) Memory candidate bağımsız evidence ve verifier olmadan active olmaz.
- (Tarihsel referans) Duplicate, stale, conflict ve retention hygiene raporlanır; otomatik silme yapılmaz.
- (Tarihsel referans) Tekrarlı hatalar learning candidate olur; kanıt olmadan guideline/skill'e terfi etmez.
- (Tarihsel referans) Skill candidate, evaluation, approval, activate/deprecate/retire yaşam döngüsü çalışır.
- (Tarihsel referans) Memory veya Mem0 Work Graph durumunu, policy'yi veya authority'yi sahiplenmez.

## G. Araştırma ve uygulama teslimi

- (Tarihsel referans) Doğal dil araştırma isteği Work+Intent+ResearchQuestion'a dönüşür.
- (Tarihsel referans) En az bir researcher subagent, critic/synthesizer ve risk bazlı citation verifier çalışır.
- (Tarihsel referans) Kaynak snapshot, claim, contradiction ve exact citation kanıtı tutulur.
- (Tarihsel referans) Araştırma raporu Decision ve exact Plan'a ayrı review ile dönüşür.
- (Tarihsel referans) Implementation yalnız approved path/resource scope'ta worktree içinde yapılır.
- (Tarihsel referans) Testler Zekam tarafından bağımsız yeniden çalıştırılır.
- (Tarihsel referans) Patch, receipt ve rollback artifact'ı üretilir.
- (Tarihsel referans) Commit/push kullanıcı ve policy kurallarına uyar.

## H. Secret, policy ve güvenlik

- (Tarihsel referans) Secret Broker logical SecretRef çözer; plaintext model context'ine girmez.
- (Tarihsel referans) Prompt/log/vector/artifact/report/backup içinde secret sızıntı testleri geçer.
- (Tarihsel referans) Network default-deny ve outbound disclosure/authorization uygulanır.
- (Tarihsel referans) Path traversal, symlink escape, archive bomb ve source-root escape fail-closed'dur.
- (Tarihsel referans) Untrusted document/repository talimatları asla sistem komutu sayılmaz.
- (Tarihsel referans) Güvenli read-only sorgular gereksiz onay istemez.
- (Tarihsel referans) Network, secret, mutation, DB write, push ve destructive effect exact one-shot authorization gerektirir.
- (Tarihsel referans) Yetkilendirilmiş planın child step'leri drift yoksa anlamsız ikinci onay istemez.

## I. Scheduler ve raporlama

- (Tarihsel referans) `gelen-belgeler` watcher idempotent ingestion/research job üretir.
- (Tarihsel referans) Gece model health, project scan, memory hygiene, recovery ve araştırma işleri çalışır.
- (Tarihsel referans) Sabah genel ve proje bazlı rapor oluşur.
- (Tarihsel referans) Rapor model, kaynak, agent, token, cost, quota, failure, contradiction ve next action içerir.
- (Tarihsel referans) Gözlem/reporting projection'ları kanonik state olmaz.

## J. Kalite, dokümantasyon ve release

- (Tarihsel referans) Unit, integration, security, property, concurrency, local-store acceptance ve E2E
  testleri desteklenen platformlarda geçer. — `reimplemented-and-verified` (Windows hedefi)
- (Tarihsel referans) Lint, format, type check, dependency audit, local schema drift ve dead-code kapıları
  geçer. — `reimplemented-and-verified` (Windows hedefi)
- (Tarihsel referans) İnsan okunur Türkçe belgeler gerçek kodla çelişmez.
- (Tarihsel referans) Commit mesajları Türkçe anlamlı ve ASCII-only'dir.
- (Tarihsel referans) Kritik akışlarda placeholder, mock-only production path veya ölü kod yoktur.
- (Tarihsel referans) Release manifesti, SBOM, checksum ve rollback talimatı vardır.
- (Tarihsel referans) Zekam tekil kimlik testi package, CLI, environment, home, schema ve DB alias'ı bırakmaz.

Global DoD'nin tek bir maddesi bile kanıtsızsa ürün `tamamlandi` sayılmaz.
