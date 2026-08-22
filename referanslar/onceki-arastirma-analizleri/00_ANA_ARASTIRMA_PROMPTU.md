# Z — Ana Araştırma Promptu

> **Amaç:** Uygulamaya başlamadan önce, kalıcı bir yapay zekâ mühendislik kontrol düzleminin hedef mimarisini araştırmak ve karar verilebilir hâle getirmek. Bu aşamada kod yazma.

## Kullanılacak referanslar

Yalnızca aşağıdaki beş analizi başlangıç referansı kabul et; gerektiğinde güncel birincil teknik kaynaklarla doğrula:

1. [KRCN Core referans analizi](01_KRCN_CORE_REFERANS_ANALIZI.md)
2. [ZEKAM referans analizi](02_ZEKAM_REFERANS_ANALIZI.md)
3. [Context Vault referans analizi](03_CONTEXT_VAULT_REFERANS_ANALIZI.md)
4. [Avenox içeriklerinden çıkarılan ilkeler](04_AVENOX_REFERANS_ANALIZI.md)
5. [DevDan içeriklerinden çıkarılan ilkeler](05_DEVDAN_REFERANS_ANALIZI.md)

## Asıl hedef

Birbirinden farklı teknoloji, sürüm, kod tabanı, veri tabanı, talep, defect, iş ve araştırmalara sahip projeleri yöneten; model veya CLI değişse bile kaldığı yerden güvenle devam eden; çoklu model ve subagent çalışmasını ölçülebilir, kilitli, kanıtlı ve maliyet etkin biçimde koordine eden **modelden ve sağlayıcıdan bağımsız bir AI Engineering Control Plane** tasarla.

Bu çalışma mevcut üç repository’yi birleştirme projesi değildir. Güçlü sözleşmeleri ve doğrulanmış fikirleri al; çakışan, tekrarlı, karmaşık, ölü veya kanıtsız parçaları taşıma. Temiz bir repository ve modüler monolit ile sıfırdan başlanacak; model, CLI ve sağlayıcılar değiştirilebilir adaptörler olacaktır.

## Değişmez gereksinimler

- Mimari kalıcı; Codex, Claude, OpenCode ve modeller geçicidir.
- Proje kaynak kökleri salt okunur kabul edilir. Bütün yazma işlemleri sistemin çalışma alanı, sandbox veya detached worktree’sinde yapılır.
- Sohbet geçmişi, Markdown özetleri ve vektör sonuçları kanonik durum değildir. “Nerede kaldık?” sorusu Work Graph, checkpoint, run ve receipt kayıtlarından cevaplanır.
- Her proje kendi teknoloji/sürüm/capability profilini taşır. Sabit “sen testçisin/sen mimarsın” promptları yerine işin gerektirdiği yetenek ve proje kanıtı kullanılır.
- Önemsiz olmayan her işte en az iki bağımsız yürütme kimliği bulunur. Ancak aynı yazma işi iki modele verilmez: araştırmacı–eleştirmen, builder–verifier veya iki ayrık alt iş gibi tamamlayıcı kapsamlar kullanılır.
- Ana ajan; bağımlılık, risk, kaynak çatışması ve kilitleri analiz ederek paralel, sıralı veya tek-worker yürütme seçer.
- Her subagent sonucu `completed`, `partial`, `failed`, `blocked`, `recovery-required` veya `abstained` olarak, kanıt ve artifact referanslarıyla ana ajana döner. Serbest metin başarı beyanı yeterli değildir.
- Lease, fencing token, idempotency key, logical resource lock, effect claim ve terminal receipt olmadan yazma/haricî etki tamamlanmış sayılamaz.
- Model seçimi; proje benchmark’ı, capability, sağlık, güvenlik sınıfı, context sınırı, kalan kota, maliyet, hız ve gözlenen başarıyla yapılır. Codex `%40`, Claude `%30` gibi eşikler policy/config verisidir; koda gömülmez.
- Limit dolduğunda devam, aynı kanonik state ve continuity paketi üzerinden başka model/CLI ile yapılır.
- OpenCode model dizini keşfedilir; çalışan modeller sağlık testi ve zor benchmark’lardan geçirilir. Sonuçlar hem makinece okunur kayıtlara hem insanın anlayacağı Türkçe Markdown raporlarına dönüşür.
- Araştırma zinciri gerektiğinde birden çok model kullanır: soru/kapsam üretimi → bağımsız araştırma → mimari eleştiri → sentez → citation doğrulama → uygulanabilir karar/plan.
- Model tartışmaları sınırlı tur, süre, token ve kanıt bütçesiyle yürür; sonsuz konuşma veya çoğunluk oylaması yapılmaz.
- Kod, veri tabanı metadatası, belgeler, talepler, defectler, işler, kararlar, araştırmalar ve doğrulanmış öğrenimler aranabilir olmalıdır. Secret, credential, private key ve hassas ham içerik hiçbir zaman prompta, loga veya vektör indeksine girmez.
- PostgreSQL kanonik veri tabanıdır; pgvector türetilmiş semantic index için kullanılır. Object storage ham/normalize artifact’ları saklar; Redis yalnız geçici sinyal/kuyruk hızlandırıcısıdır.
- 1024 boyutlu mevcut embedding modeli ilk sürümde korunur. Dense arama tek başına kullanılmaz; exact kimlik/path/symbol, PostgreSQL full-text, fuzzy alias ve dense retrieval rank fusion ile birleştirilir. Reranker opsiyonel ve ölçümle etkinleştirilir.
- Parser, chunker, embedding, kaynak veya policy değişiminde kontrollü re-index gerekir. Her vektör kayıtlı profile ve kaynak revision’ına bağlıdır.
- Bellek; çalışma/continuity, episodic run, onaylı semantic knowledge, procedural skill ve öğrenme adayları olarak ayrılır. Bir hata veya tek gözlem sistemi otomatik değiştiremez.
- Tekrarlanan ve kanıtlanan yöntemler skill adayı olur; bağımsız evaluation, proje uyumluluğu, izin manifesti, sürüm, digest, onay ve rollback sonrasında etkinleşir.
- Secret Broker yalnız referans taşır; gerçek değer gerektiğinde adapter sınırında kısa ömürlü ve dar kapsamlı enjekte edilir. Model secret değerini görmez.
- Düşük riskli salt-okunur ve önceden yetkilendirilmiş işler otomatik ilerler. Kaynak kodu, veri tabanı, network, secret veya üretim etkisi taşıyan işler exact kapsamlı ve tek kullanımlık yetki ister. Her küçük adım için onay istenmez.
- İnsanların göreceği klasör ve rapor adları Türkçe olabilir: `projeler`, `talepler`, `defectler`, `isler`, `arastirmalar`, `modeller`, `raporlar`. İç paket ve protokol adları İngilizce kalabilir.
- Inbox, zamanlayıcı ve gün başlangıcı görevleri; bırakılan belgeyi tarayabilir, gece araştırma yapabilir ve sabah kanıtlı, uygulanabilir rapor hazırlayabilir. Yaptığı her adım kayıt altındadır.
- Dashboard ilk sürümün ön koşulu değildir; fakat event, run, lock, model, token, maliyet, hata, öğrenim ve “bugün ne var?” görünümünü besleyecek veri baştan üretilir.

## Araştırılması gereken mimari

Aşağıdaki sınırları netleştir:

1. **Project Registry ve Project Capsule:** proje kimliği, doğal dil alias’ları, source binding, capability profili, teknoloji/sürüm/module kanıtı ve Türkçe insan görünümü.
2. **Work Graph:** talep, defect, iş, alt iş, karar, bağımlılık, blocker, kabul ölçütü, evidence, owner, durum ve geçmiş.
3. **Orchestrator ve Runtime:** DAG planlama, minimum iki tamamlayıcı ajan, paralellik, queue, lease, fencing, lock, checkpoint, retry/recovery, claim/receipt ve result fan-in.
4. **Model Control Plane:** inventory, provider/CLI adapter’ları, health, quarantine, project benchmark, runtime observation, quota telemetry, fiyat/latency ve deterministic assignment.
5. **Research Factory:** doğal dil niyeti, soru parçalama, kaynak politikası, bağımsız araştırma, karşı kanıt, contradiction, citation ve decision/plan üretimi.
6. **Knowledge Plane / Context Vault:** sürümlü ingestion, normalize içerik, code/DB/document/OCR parser’ları, hybrid retrieval, citations, re-index, evaluation ve provenance.
7. **Memory ve Context Compiler:** kanonik kayıtları değiştirmeyen bütçeli context derleme, continuity snapshot, handoff, dedupe, conflict, freshness, retention ve öğrenme terfisi.
8. **Skill Platform:** referans tabanlı özel skill kütüphanesi, dinamik seçim, izinler, testler, lifecycle ve proje uyumluluğu.
9. **Execution Security:** source no-write, sandbox/worktree, path allowlist, network default-deny, secret broker, content redaction, prompt injection sınırı ve bağımsız doğrulama.
10. **Operations:** scheduler/inbox, observability, günlük ve proje bazlı raporlar, backup/restore, disaster recovery, dashboard API’leri ve self-health.

## İstenen araştırma çıktısı

Çıktıyı uzun bir teori metni yerine karar odaklı maddelerle hazırla:

- Bir sayfalık hedef sistem tanımı ve mimari ilkeler.
- Bounded-context/modül haritası; her modülün sahibi olduğu ve olmadığı veriler.
- Kanonik veri ile rebuild edilebilir projection/index ayrımı.
- Beş referans için `Al / Yeniden Tasarla / Alma` matrisi.
- Ana domain kayıtları ve kritik state machine’ler.
- Agent/subagent, lock, lease, claim, receipt ve result envelope sözleşmeleri.
- Model inventory, benchmark ve kota düşüşü/fallback karar algoritması.
- Context, memory, RAG, 1024-d vector ve hybrid retrieval tasarımı.
- Secret, sandbox, provider ve approval güvenlik modeli.
- Önerilen Türkçe çalışma dizini ile PostgreSQL/object-storage yerleşimi.
- İlk yürüyen dikey dilimden tam sisteme kadar aşamalı uygulama sırası ve her aşamanın ölçülebilir kabul ölçütleri.
- En önemli teknik riskler, doğrulanması gereken varsayımlar ve ertelenecek özellikler.

## Araştırma kuralları

- Doküman iddiasını kod/test kanıtı olmadan “tamamlanmış” sayma.
- Eski repository kodlarını doğrudan kopyalamayı varsayma; sözleşme ve test vakalarını yeniden değerlendir.
- İlk sürümü mikroservis, graph database veya çoklu vector database ile gereksiz büyütme.
- Semantic retrieval’i görev durumu, yetki, policy veya tamamlanma kanıtının önüne geçirme.
- Büyük context’i kalite sanma; yalnız gerekli kayıt ve skill’leri bütçeli, açıklanabilir biçimde seç.
- “Daha çok ajan”ı başarı ölçütü yapma. Başarı; doğrulanmış çıktı, düşük tekrar, düşük hata, düşük token/maliyet ve güvenli recovery ile ölçülür.
- Self-learning adı altında aktif policy, skill veya belleği otomatik değiştirme.
- Güncel olmayan model, fiyat, API, RAG veya veritabanı davranışlarını güncel birincil kaynaklarla doğrula.
- Son rapor, uygulama ekibinin yeni repository’yi açıp ilk ADR ve backlog’u çıkarabileceği kadar kesin; fakat henüz kod üretmeyecek kadar mimari seviyede olmalıdır.
