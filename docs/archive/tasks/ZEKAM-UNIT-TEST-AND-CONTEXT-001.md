---
schema: zekam-active-task/v2
task_id: ZEKAM-UNIT-TEST-AND-CONTEXT-001
status: APPROVED_ACTIVE_TASK
title: Zekam Unit Test Engineering ve Yalin Baglam Skill Entegrasyonu
created_at: 2026-10-02T22:55:59+03:00
baseline_repository: mehmet-karacan/zekam
baseline_branch: main
baseline_head: 4042ad598814a5d12bb1e9493d03ec169050427b
baseline_commit_subject: "duzeltme: tek projenin sismis RAG state'i resume paketini dusurmesin"
baseline_is_fixed_revision: true
legacy_postgresql_data_import: FORBIDDEN
postgresql_runtime_dependency: FORBIDDEN
docker_required_for_zekam_core: false
ui_surface: FORBIDDEN
push_authorized: false
runtime_test_evidence_at_task_creation: NOT_EXECUTED
---

# AKTIF_GOREV.md

## 1. Görev: araştırmayı çalışan Zekam yeteneğine dönüştür

**Bu bir araştırma veya öneri istemi değil, uygulama görevidir.** Kullanıcı bu dosyayı uygulamak üzere bir coding modeline verdiğinde, aşağıdaki kapsamı Zekam'ın gerçek kaynak kökünde geliştir, test et ve mevcut kabul/kanıt zincirine bağla. Aynı araştırmayı baştan yapıp yalnız yeni raporla dönme. Kaynak değişmişse yalnız ilgili farkı doğrula; kapsam içi teknik uygulama ayrıntılarını gerekçeli seçerek ilerle.

Birlikte teslim edilecek iki sonuç vardır:

1. **Genel Zekam:** modele sürekli yüklenen, tekrarlı, tarihsel veya göreve ilgisiz talimatları azalt; gerekli yerel bilgi ve işleme özel rehberi doğru zamanda getir. Yetki, kaynak sahipliği, recovery, bağımsız doğrulama ve kanıt bütünlüğünü koru. Değişiklik hem gerçek şablon üreticilerini hem istemci projection'larını hem de ilgili testleri kapsasın.
2. **Unit Test Engineering:** kullanıcı bir veya daha fazla kaynak dosya ve coverage hedefi verdiğinde Zekam içinde keşif, gerçek ölçüm, davranış analizi, test planlama, test üretme, çalıştırma, hata ayrıştırma, yeniden ölçme ve eksiklere göre devam etme yeteneği çalışsın. İlk gerçek adaptör Java/Maven/JUnit/JaCoCo olsun; çekirdek ve skill sözleşmesi tek modele veya dile bağımlı olmasın.

Ana ilke: **Hedef ve kanıt nettir; çözüm yöntemi gerekçeli biçimde esnektir. Model düşünür ve üretir, çalıştırıcı ölçer, Zekam yetkiyi ve sürekliliği yönetir.** Testler yalnız if/satır çalıştırmak için değil, gerçek davranış hatalarını yakalamak için üretilir.

### 1.1 Kullanıcı deneyimi ve bitmiş ürün

Kullanıcı “Şu dosyaların unit testlerini yaz, hedef %90” dediğinde sistem kapsamı çözer; hangi ölçünün ne anlama geldiğini gösterir; ortamı ve mevcut testleri inceleyip başlangıç durumunu çıkarır. Önemli davranış açıklarını, önceliği ve tahmini katkının belirsizliğini sunar. “Bu kapsam ve bütçeyle başlayayım mı?” ilk plan onayıdır. Kullanıcı zaten aynı exact planı onaylamışsa tekrar onay istemez.

Onaylı test-only kapsamda makul yöntemler tüketilene, hedef ve kalite kapıları geçilene veya kanıtlı bir durma koşulu oluşana kadar döngü sürer. Her küçük test veya repair için kullanıcıya dönülmez. Yeni bir yetki, kapsam, bütçe veya production davranışı değişikliği gerektiğinde ayrı karar gerekir. Kullanıcı “dur/devam/nerede kaldık?” dediğinde yeni model/oturum önceki sonuçları kanonik kayıttan anlayabilir.

**Yalnız SKILL.md, JSON şeması, prompt veya sahte sonuç döndüren CLI teslim sayılmaz.** Gerçek yerel Maven süreci, gerçek test raporu, gerçek JaCoCo ölçümü ve Zekam'ın mevcut agent dispatch/receipt zinciriyle çalışan uçtan uca yol gerekir. Desteklenmeyen teknoloji ve çalıştırılmayan model kombinasyonu dürüstçe belirtilir.

### 1.2 Bu dosyanın statüsü

Headerdaki `APPROVED_ACTIVE_TASK`, mevcut `zekam-active-task/v2` parserının sözleşme değeridir; tek başına operational authorization, claim, lease, model çağrısı, kurulum veya başarı kaydı üretmez. Kullanıcının bu dosyayı uygulama için vermesi bu geliştirme kapsamını seçer; mevcut mekanik izinler yine doğrulanır.

Bu görev hazırlanırken repository salt okunur incelendi. **Kullanıcının makinesinde Zekam testleri, Maven/JaCoCo/PIT veya canlı model kampanyası çalıştırılmadı; repository ve global ayarlar değiştirilmedi.** Hazırlık sırasında belge biçimi kontrolü yapılması runtime kabul kanıtı değildir.

Bu belge bir geliştirme sözleşmesidir; tamamını gelecekte her kullanıcı isteğine system prompt olarak ekleme. Bölümleri rol/görev bağlamına göre derle. Önceki araştırma dosyaları yararlı referanstır, bu göreve başlayabilmek için zorunlu ek dosyalar değildir.

## 2. Kapsam, yetki ve mevcut işe güvenli geçiş

### 2.1 İki farklı production kavramını karıştırma

Bu görev **Zekam'ın kendi kaynak kodunda geliştirme yapılmasını** ister. Aşağıdaki test-only kısıtı ise geliştirilen skill'in ileride test üreteceği **hedef projenin production koduna** ilişkindir. “Production'a dokunmak yasak” diyerek Zekam implementasyonunu yapmaktan kaçınma; “Zekam'a geliştirme onaylandı” diyerek GPU/SKY/Akış veya başka hedef projeyi değiştirme.

İzin verilen çalışma: bağlı gerçek Zekam kökünde bu kapsama ait kaynak/test/şema/migration/belge değişiklikleri, mevcut onaylı yerel araçlarla testler, Zekam'a ait sentetik kabul fixture'ları ve repository dışındaki çalışma artifact'ları. Yeni bağımlılık/araç kurulumu, gerçek kullanıcı-geneli config dağıtımı, yeni provider/remote veri aktarımı, benchmark kampanyası ve başka projede mutation mevcut exact izin akışına tabidir. Push yetkisi yoktur. Commit/branch için mevcut kullanıcı yetkisi ve repository politikası geçerlidir; bu dosya bunları kendiliğinden açmaz.

Mevcut, kullanıcı tarafından açılmış coding oturumunu ve onun yetkili dispatch yolunu kullan. Yeni provider veya model kimliği icat etme; mevcut oturum yetkisini tüm modeller için sınırsız benchmark izni sayma. Alt ajan gerekiyorsa gerçek invocation kullan; rol yaparak ikinci ajan veya bağımsız review kanıtı üretme.

### 2.2 Görev devri

İlk olarak güncel `AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md`, yaşayan görev/projection ve ilgili Global DoD durumunu oku. Bu görev seçildiğinde eski yaşayan görevi yanlışlıkla çalıştırma veya içeriğini kaybetme.

- Önceki `AKTIF_GOREV.md` ve üretilmiş YAML'ın exact byte/digest kaydını mevcut arşiv düzeniyle koru. Kullanıcı yeni dosyayı köke önceden koymuşsa eski committed sürümü Git'ten, kayıp olabilecek yerel durumu ise mevcut recovery kayıtlarından ayır; kayıp yerel içeriği yeniden üretilmiş gibi sunma.
- Eski `ZEKAM-RAG-ROUTER-QUALITY-002` görevinin açık işleri ve Global DoD eksikleri açık kalır. Bu görevle tamamlandı sayılmaz. Talimat/istemci iyileştirmeleriyle örtüşen eski R06/R08 gibi kayıtları yeniden iş yaratmadan ilişkilendir; ilişkili kayıtların güncel durumunu yerelde doğrula.
- `ActiveTaskContract.load(Path('AKTIF_GOREV.md'))` ve mevcut projection üretim/doğrulama yolunu kullan. Elle digest yazılmış bağımsız YAML authority oluşturma. Komut/API adını önce koddan veya `--help` ile doğrula.
- Aktif lease, kaynak sahibi veya recovery-required iş varsa duplicate mutation başlatma. Eski alakasız backlog'u sessizce kapatma; bütün geçmiş backlog'u bu yeni teslimin gereksiz önkoşulu da yapma.

### 2.3 Yerel baseline

Bağlı gerçek source root, `git status --short`, HEAD, branch, imported `zekam.__file__`, etkin config kaynağı, Python/OpenCode/Java/Maven sürümleri, active task digest ve mevcut Work/plan revision kaydedilsin. Secret/kişisel veri değerlerini kaydetme. Bu belgedeki baseline sabit araştırma referansıdır; yerel HEAD ilerlemişse yeni duruma ilgili diff ile uyarlanır, eski revision'a reset edilmez.

Mevcut test/lint/type-check sonuçlarını gerçekten çalıştır ve var olan arızaları yeni regresyondan ayır. Önceki belgelerdeki test sayıları bu koşunun sonucu değildir. Güvenli bağımsız işleri sürdür; bilinen ortam arızalarını saklamak için test silme/skip ekleme veya tüm eski hataları bu kapsamda düzeltmeye çalışma.

User dirty değişiklikleri korunur. `reset --hard`, toplu checkout/stash, mirror, geçici kullanıcı-projesi klonu veya detached worktree kullanılmaz. Bağlı gerçek kök kuralı korunur. Yeni, Zekam-owned sentetik test fixture'ı üretmek kullanıcı projesini kopyalamak değildir; fixture kaydı ve artifact alanı ayrı tutulur.

## 3. Yeniden araştırma ve karşılaştırmadan çıkan kararlar

Bu bölümdeki kararlar, önceki D01–D34 adaylarının uygulama seçimidir. **Çekirdek** bu görevin implementasyon kapsamıdır. **Sınırlı pilot** mekanizma ve kontrollü kabul içerir, herkese otomatik açılmaz. **Ertelendi** bu görevin DoD'sine gizlice eklenmez. Tasarım seçimi runtime yetkisi değildir.

### 3.1 Kararı değiştiren veya netleştiren karşılaştırmalar

| Konu | Yeniden kontrol edilen bulgu | Bu görevdeki sonuç |
|---|---|---|
| Talimatları kısaltmak | OpenAI, gereksiz reçeteler ve göreve ilgisiz zorunlu okumaların yeniden değerlendirilmesini öneriyor. Anthropic ise kırılgan işlemlerde kesin prosedürü koruyor. | Kelime/satır kotası değil, göreve uygun bilgi ve korunmuş işlem sınırı esas. [E01,E02] |
| “Skill'ler kötüdür” genellemesi | SkillsBench yararı modele/harness'e ve göreve göre değiştiriyor; ağır tek-yol reçeteleri bazı görevleri kötüleştirebiliyor. | Genel silme yok; kontrollü eşleştirilmiş değerlendirme var. [E03] |
| Eski görev talimatları | `opencode.json`, kendisini superseded olarak tanımlayan `NIHAI_UYGULAMA_PROMPTU.md` dosyasını otomatik yüklüyor. | Tarihsel belge korunur, aktif gereksinimler kaybolmadan otomatik instruction listesinden çıkarılır. [R01,R02] |
| Managed metin değişimi | Global bootstrap mevcut gövdeyi exact eşitlikle doğruluyor. Yalnız yeni sabit yazmak eski kurulumlarda drift yaratabilir. | Bilinen eski sürümden atomik, sahiplik kontrollü geçiş ve rollback birlikte geliştirilir. [R03] |
| Hazır loop'u kullanmak | `loop_service.py` / `loop_orchestrator.py` ortak kontratlar içeriyor; `measured_loop_runtime.py` somut PostgreSQL loader ve native-only driver içeriyor. | Ortak kontratlar uyarlanır; eski runtime'ın yeni Java/OpenCode yoluna hazır olduğu varsayılmaz. PostgreSQL geri gelmez. [R04–R06] |
| Yeni context platformu kurmak | Context compiler/recipe ve paired scaffolding-ablation evaluator zaten var. | Bunlara küçük ekleme; ikinci context/deney/authority platformu yok. [R07,R08] |
| Maven exit 0 | Surefire'ın hiç test bulunmadığında başarısız olmasını sağlayan ayarı varsayılan olarak kapalı olabilir. JaCoCo report hedefinin varsayılan fazı verify'dır. | Test discovery ve taze rapor ayrıca doğrulanır; `mvn test` tek başına ölçüm garantisi değildir. [E06,E07] |
| Satır/branch yorumu | JaCoCo kısmen yürütülen satırı covered sayabilir; exception handling branch sayacına dahil değildir. | Resmî sayaç anlamı korunur; exception davranışları coverage'dan ayrıca planlanır. [E05] |
| Çok test = iyi test | TestForge execution ve coverage feedback'ini kullanıyor; bu, sadece sayıyı artırmanın başarı kanıtı olduğu anlamına gelmiyor. | Davranış/oracle planı ve gerçek geri besleme; kota ve kesin yüzde vaatleri yok. [E09] |
| Mutation skoru | PIT killed, survived, no coverage ve çalışma sorunlarını ayırıyor. | Risk odaklı opt-in pilot; evrensel zorunlu skor veya bütün mutantları öldürme hedefi yok. [E08] |

Bu kaynak karşılaştırmaları tasarım gerekçesidir, Zekam üzerinde ölçülmüş hız/kalite kazanımı değildir. Önceki raporlardaki ETH v3 ayrıntısının HTML sayfası bu yeniden kontrolde açılamadı; abstract erişildi. Dolayısıyla eski sürüme ait ayrıntılı sayıları yeni deney sonucu veya kesin uzunluk yasası olarak kullanma. [E10]

### 3.2 D01–D20: test mühendisliği

| ID | Seçim | Uygulanacak anlam |
|---|---|---|
| D01 | Çekirdek | Varsayılan, seçilen her dosyada line hedefidir; ağırlıklı toplam ayrıca gösterilir. Açık aggregate/branch hedefi de desteklenir. |
| D02 | Çekirdek | Mevcut Work/assignment/claim/receipt/loop sözleşmeleri; yeni authority yok. |
| D03 | Çekirdek | Java + Maven + mevcut JUnit + JaCoCo gerçek adaptörü; diğer diller adapter kontratıyla genişler, destek taklidi yapılmaz. |
| D04 | Çekirdek | Beklenen sonuç kaynağı, belirsizliği ve normative/characterization ayrımı. |
| D05 | Çekirdek | Coverage artmayan fakat yeni davranış/assertion/regression değeri olan test korunabilir. |
| D06 | Çekirdek | Compile/fixture/oracle/infra/flaky/gerçek ürün hatası ayrı sınıflandırılır. |
| D07 | Çekirdek | Son adayda taze, kapsamı belirli regression + coverage + bağımsız verifier. |
| D08 | Çekirdek | Mevcut context/continuity'ye bağlı role özgü sınırlı kanıt; Java analizini Python AST desteğiyle karıştırma. |
| D09 | Çekirdek altyapı, canlı kabul izinli | Model × istemci × görev qualification; kampanya planı ve gerçek sonuç ayrımı. |
| D10 | Sınırlı pilot | Opsiyonel, hedefe daraltılmış PIT adaptörü, sonuç ayrıştırma ve küçük gerçek kabul fixture'ı. |
| D11 | Çekirdek | Plateau'da ölçüm/bağlam denetimi ve bütçeli alternatif strateji; hemen refactor kararı yok. |
| D12 | Çekirdek yöntem serbestliği | Uygun invariant varsa property/metamorphic/stateful test; yeni framework zorunlu değil. |
| D13 | Çekirdek | Analiz/plan/üretim/doğrulama sorumlulukları; her tur altı ayrı ajan töreni yok. |
| D14 | Ertelendi | EvoSuite entegrasyonu; bu görevde ikinci jeneratör motoru kurulmaz. |
| D15 | Ertelendi | Tam Java CFG/path solver/Panta-HITS motoru; gerekli minimum dosya/sembol eşleme kapsamda kalır. |
| D16 | Ertelendi | Ayrı adversarial-agent platformu ve AI mutant jeneratörü; verifier adversarial senaryo düşünebilir. |
| D17 | Çekirdek kapı | Ayrı onaylı minimal testability refactor ve yeniden doğrulama akışı. |
| D18 | Çekirdek | Test yazma, build, bağımlılık, ağ/model ve production değişimi ayrı izinler. |
| D19 | Çekirdek | Ölçülmüş ilerleme, kalıcı attempt, bütçe, pause/cancel/recovery ve dürüst stop reason. |
| D20 | Çekirdek | İnce skill paketi + kodla yürütme + qualification; kaynak ve lisans izi. |

### 3.3 D21–D34: genel Zekam

| ID | Seçim | Uygulanacak anlam |
|---|---|---|
| D21 | Çekirdek | Hedef, değişmez sınır, yerel gerçek, yöntem önerisi ve kanıt şartını ayır. |
| D22 | Çekirdek | Superseded belgeyi otomatik yükten çıkar; arşiv ve gerekli güncel bilgiyi koru. |
| D23 | Çekirdek | Coordinator/global/bootstrap route kapsamlarını uzlaştır; Jira/RAG/hata ayrıntısını ilgili bağlamda yükle. |
| D24 | Çekirdek | Mevcut manifest/diagnostic'e etkin yükün kaynağı, digest'i, nedeni ve ölçüm niteliği ekle. |
| D25 | Çekirdek | Kanonik generator + bilinen eski digest migration'ı + rollback + kullanıcı metni koruması. |
| D26 | Sınırlı pilot | Kanıtlı model–istemci ihtiyacına küçük yardımcı referans; marka bazlı skill çatalları yok. |
| D27 | Çekirdek | Davranış/risk/oracle planı; test sayısı kota değildir, yeni keşfe açık. |
| D28 | Çekirdek | Skill discovery, doğru tetikleme, reference yükleme ve sonuç kalitesi ayrı test edilir. |
| D29 | Çekirdek | Sınırları sabit, yöntem ve rol tekrarları gereksinime bağlı iterasyon. |
| D30 | Çekirdek, otomatik silme yok | Hata önce kod/regression/yerel rehber/kural olarak sınıflanır; her hata global prompt'a eklenmez. |
| D31 | Çekirdek altyapı, canlı kabul izinli | Aynı hedef ve güvenlik altında paired ablation; sadece metin kısalması başarı değildir. |
| D32 | Çekirdek | Mevcut yapıda küçük ortak standart; yeni büyük sistem yok. |
| D33 | Çekirdek | Kritik sözlü sınırın gerçek işlem engeli ve negatif test karşılığı. |
| D34 | Çekirdek | Öneri, onaylı kapsam, operational durum ve gerçek ölçüm birbirinin yerine geçmez. |

## 4. Gerçek repository'ye entegrasyon haritası

Aşağıdaki yollar baseline'da incelenmiştir veya ilgili mevcut girişlerdir. Uygulamada source ve çağrı grafiğini doğrula; sadece sınıf/dosya var diye uçtan uca çalışıyor varsayma. Yeni API isimlerini varmış gibi çağırma.

| Mevcut alan | Bu görevde kullanım / sınır |
|---|---|
| `src/zekam/application/active_task_contract.py` | Görev kimliği ve generated projection; şemayı gereksiz değiştirme. |
| `src/zekam/domain/agents.py`, `application/agent_dispatch.py`, `application/loop_service.py` | Rol, assignment, invocation ve admission zinciri; davranış alanlarını mevcut zarfla ilişkilendir. |
| `application/loop_orchestrator.py`, `loop_progress_compiler.py`, `loop_control.py` | Attempt-job binding, ilerleme, kesinti ve kontrol semantiği; gerçek SQLite adaptör yolunu bul/doğrula. |
| `application/measured_loop_runtime.py` | PostgreSQL/native-only bağımlılıklar var. Bu dosyayı doğrudan yeni runtime sanma; ihtiyaç kadar yerel composition ekle. |
| `application/context_compiler.py`, `context_recipe.py`, `model_context_admission.py` | Seçilen bilgi, rol, budget ve model limitleri; izin üretmez. |
| `application/scaffolding_ablation.py` | Paired evidence kapıları mevcut; yeni deney motoru yerine uyumlu genişletme. |
| `application/client_instruction_bootstrap.py`, `opencode_agent_bootstrap.py`, `client_integrations.py` | Gerçek talimat/ajan üreticisi ve dağıtım; yalnız üretilmiş kopyayı elle düzeltme. |
| `application/skill_packages.py`, `skill_runtime.py`, `src/zekam/skills/` | Paket kimliği, yaşam döngüsü ve projection. `allowed-tools` metni çalışma yetkisi değildir. |
| `opencode.json`, `.opencode/agents/`, `AGENTS.md`, `00_BASLA.md` | Oturum yükü ve rol girişleri; sadeleştirme/boundary uyumu. |
| `application/workspace_resume.py`, `docs/ZEKAM_YETKINLIK_ENVANTERI.md` | Yeni yeteneğin dürüst görünürlüğü; son committeki proje-bazlı RAG hata izolasyonu korunur. |

**Önemli implementasyon kararı:** Mevcut soyut loop/optimization kontratlarını, Zekam'ın yerel operational store ve onaylı OpenCode dispatch yoluna bağla. Gerekli SQLite repository/adapter boşluğu varsa yalnız bu yeteneğin ihtiyaç duyduğu bölümü ekle ve migration'ını test et. Legacy PostgreSQL bağlantısını açma, eski veriyi taşıma veya genel driver yasağını gevşeterek Maven'ı aradan geçirme. Bir yerel test-runner capability'si ile agent/model çağrısının ayrı effect sınırları olsun.

Zekam CLI/JSON ürünü olarak kalır. Yeni UI/dashboard/TUI, zorunlu sunucu, Redis/PostgreSQL veya Docker altyapısı eklenmez. Genel harness framework'ü vendörlemek yerine gerekli fikirler mevcut mimariye uyarlanır.

## 5. Genel Zekam talimat ve context düzenlemesi

### 5.1 Gereksiz yükü kaldır, anlamı koru

Kanonik talimatların envanterini çıkar: repository/global rules, OpenCode role dosyaları, otomatik instruction listesi, selected skill body/reference, managed lifecycle/compaction ve ilgili tool metadata. Her kural için “türü, geçerli rol/route/state, neden gerekli, asıl kaynak, mekanik karşılık, korunacak regression” kaydı yeterlidir. Yeni devasa rule-engine veya her cümle için ayrı veritabanı servisi kurma.

- Kök talimatlar amacı, proje-özel kritik gerçekleri, yetki/sahiplik sınırını ve nasıl kanıtlanacağını taşır.
- Jira, RAG source-fallback, ACL hata çözümü, benchmark ve mutation ayrıntıları ilgili route/state/role referansında kalır. Genel sohbete veya ilgisiz unit-test planına yüklenmez.
- Eski `NIHAI_UYGULAMA_PROMPTU.md` tarihsel kaynak olarak erişilebilir kalır; normal otomatik instruction paketinden çıkarılır. Yalnız onda kalan gerçekten güncel zorunlu bilgi varsa doğru kanonik kaynağa taşınır.
- “Her şeyden önce doctor/ask”, “her düzeltmede bütün belgeleri oku” gibi kapsamı belirsiz tekrarlar, güncel coordinator davranışıyla tek anlamlı hale gelir. Başlangıç sağlık kontrolü gereken olay ile genel soru ayrılır.
- Aynı sonuca giden birden fazla güvenli yöntem varsa model seçer. Örnek senaryo, tavsiye veya heuristik sert kural değildir. Güvenlik ve exact sözleşmeler ise tavsiye değildir.
- Basit sohbet ve yeterli pinned citation cevabı için yeni ajan/araç töreni ekleme. Gerçek agentic mutation/research işinde mevcut subagent ve risk bazlı bağımsız verifier şartını kaldırma.

Bu görev `AGENTS.md` / `00_BASLA.md` kapsam koşullarını sadeleştirmeye izin verir; kaynak-root, claim-before-effect, recovery ve no-secret ilkelerini silmeye değil. Değişiklikten önce mevcut akış korunarak baseline alınır. Yeni giriş ile scoped referans birlikte test edilir; “dosya kısaldı ama kurallar bulunamıyor” kabul değildir.

### 5.2 Etkin context manifesti

Mevcut manifest/diagnostic sözleşmesine geriye uyumlu olarak mümkün olan kaynak türü, logical ref, digest, rol/route, yükleme nedeni, yükleme seviyesi ve ölçüm niteliği ekle. Bunları ayrı bir authority kaydı yapma.

`known_loaded`, `discovered_only`, `estimated`, `unobservable` gibi ayrımın anlamı açık olsun. Client'ın gizli system prompt'unu veya tool serialization'ını görmüyorsan tam etkin token sayısı iddia etme. Byte/karakter/token tahmini/sağlayıcının gerçek usage sayısı birbirinden ayrılır. Token sayım yöntemi ve sürümü belirtilir. Tokenizer sırf ölçüm için izinsiz indirilmez; mevcutsa kullanılır, değilse tahmin açık etiketlenir.

Global dosya kısalıp otomatik imports toplamı aynı kaldıysa rapor bunu gösterebilsin. Required safety/work bilgisi kesilerek budget tutturulmaz; minimum zorunlu bağlam sığmıyorsa çağrı blocked-context olur. Kaynak içindeki talimat benzeri metin, tool çıktısı ve araştırma metni authority kazanmaz.

### 5.3 Managed güncelleme ve rollback

Kanonik generator değişikliği, manifest/projection üretimi ve güncelleme mekanizması birlikte teslim edilsin. Bilinen eski managed sürümler güvenilir içerik digest'iyle tanınır. Sadece marker bulunmasını sahiplik kanıtı sayma. Bilinmeyen gövde/marker çakışması veya kullanıcı düzenlemesi conflict olarak korunur.

Geçiş; dry-run, exact plan digest, güvenli backup/karantina, kullanıcı prefix/suffix metnini koruma, atomik publish, readback ve receipt içerir. Tek dosya içi managed parça ve çok dosyalı paket için crash ortası recovery/replay test edilir. Aynı apply ikinci kez yeni effect üretmez. Rollback yalnız kendi önceki değişikliğini geri alır; sonradan gelen kullanıcı edit'ini ezmez.

Çalışan gerçek kullanıcı-geneli kurulumlara apply için mevcut ayrı izin korunur. Ancak test home/fixture üzerinde temiz kurulum, bilinen eski kurulum, unknown drift ve rollback gerçek dosya işlemleriyle test edilir. Yeni kurulum yolu kadar upgrade yolu da ürün teslimidir.

### 5.4 Bütün skill'ler için küçük ortak standart

Mevcut skill authoring/validation yüzeyini kullan. Kısa ve özgül description, doğru tetiklenme, hangi sonucu ürettiği, kritik sınırlar, gerekli yerel veri, gerektiğinde yüklenen referans ve doğrulama kaynağı bulunur. Her basit skill'e agent graph/harness dayatma.

Sabit 200/500 satır veya keyfî yüzde küçültme kabul eşiği değildir; boyut bir uyarı/ölçümdür. Tekrarlı/çelişkili talimatlar, ölü link, eski otomatik import ve yanlış tetiklenme için uygulanabilir lint/regression ekle. Semantik çelişki lint'i kesin teorem değildir; reviewer bulgusu ayrı gösterilir. Yeni hatalar önce kod + regression ile çözülür; global prompt'a yeni uyarı eklemek tek başına düzeltme sayılmaz.

## 6. Unit-test istek sözleşmesi ve ilk kapsam

### 6.1 Kapsam çözümü

İstek; exact proje binding'i, hedef relative kaynak dosyaları, hedef metrik/eşik, dosya-bazlı veya toplam politika, dahil unit-test kapsamı, regression kapsamı, izin verilen test/fixture yolları, yasak production/config alanları ve bütçeye çevrilir. Aynı dosya iki modülde varsa basename ile karar verilmez. Path traversal, symlink/junction veya farklı root sessiz kabul edilmez. Dosya yoksa veya birden fazla aday varsa kanıtlı belirsizlik döner.

**Varsayılan seçim:** Kullanıcı yalnız “bu dosyalarda %90” derse her seçilmiş dosyada `LINE >= 90%`; ayrıca seçilmiş scope'un toplam covered/total oranı ve dosya-bazlı branch raporu. Varsayılan ilk planda açıkça gösterilir. Kullanıcı açıkça toplam hedef isterse aggregate kullanılır. Branch hedefi açıkça istenirse ayrı koşuldur; sessizce line90'dan branch90'a veya tüm proje90'a çevrilmez.

Unit test için integration/e2e kapsamındaki execution data ile oran şişirilmez. Aynı test adı farklı engine/modülde doğru kimlikle ayrılır. Önceden mevcut testlerden gelen kapsama ile yeni test katkısı raporda ayrıştırılır; ikisini ayırmaya yetecek per-test veri yoksa katkı yalnız aday-batch düzeyinde raporlanır.

### 6.2 İlk adaptörün teknoloji sınırı

Çekirdek normalize test/coverage/result kontratları modelden ve dilden bağımsızdır. Bu görevde **gerçek destek Java/Maven** içindir; projedeki JUnit4 veya JUnit Platform tabanlı kurulum tanınır, mevcut assertion/mock convention'ı korunur. Kullanıcının projesine zorunlu JUnit/JDK/Mockito upgrade'i uygulanmaz. Platform/engine veya Java sürümü doğrulanamıyorsa destekleniyor denmez.

Kabul fixture'larında en az legacy Java kaynak seviyesi/JUnit4, modern Java/JUnit Platform ve çok modüllü yapı temsil edilsin. Derleme `--release` ayarı ile gerçek test JVM sürümü ayrı kaydedilsin; JDK17 üzerinde eski bytecode çalıştırmak gerçek JDK8 runtime kabulü değildir. Fixture dependency sürümleri test edilerek sabitlensin; sürüm seçimi kodlama ajanına aittir, bu belgedeki “trunk” doküman URL'si SNAPSHOT kurma talimatı değildir.

Windows birinci sınıf hedeftir; Linux için de yerel execution kabulü/CI yolu bulunur. macOS veya başka JDK/istemci kombinasyonu gerçekten koşmadan “verified” gösterilmez. Gradle/pytest/Jest/Vitest için yeni tam adaptörler bu görevde zorunlu değildir; tanınmayan adapter `not-supported` verir, Java komutunu başka projeye uygulamaz.

### 6.3 Discovery ile execution'ı ayır

POM, test dizinleri, wrapper, modüller, effective ayarlar ve mevcut coverage yapılandırması incelenir. `help:effective-pom` dahil Maven komutları plugin/extension çalıştırabilir; salt metin keşfiyle aynı yetki sınıfı değildir. Böyle komutlar gerçek execution kapısından geçer.

Eksik JaCoCo/test altyapısı varsa sınırlı setup planı üret. POM/parent/property/argLine/exclusion değişikliği veya bağımlılık indirme test-yazma iznine dahil değildir. Uygun mevcut profili kullan; yoksa gerekli en küçük setup değişikliği ayrı exact onayla yapılır. Mevcut kurumsal mirror, JDK, toolchain ve repository ayarlarını rastgele değiştirme.

Başlangıçta hiç test olmaması desteklenen durumdur: `NO_TESTS` baseline olarak görünür, PASS değildir. Geçerli report ve sınıf envanteri yokken coverage'ı uydurup 0 yazma. İlk ölçüm için onaylı seed test/fixture gerekiyorsa bunun başlangıçtan sonra üretildiği belirtilir; ilk taze ölçüm orijinal pristine baseline diye geriye yazılmaz. Mevcut testlerin gerçekten bulunduğu halde seçilmediği durum ise discovery/konfigürasyon hatasıdır.

## 7. Gerçek çalıştırma, coverage ve veri bütünlüğü

### 7.1 Deterministik çekirdek neyi belirler?

Hangi komutun hangi kaynak/konfigürasyon/izin altında çalıştığı, gerçek exit code, test discovery ve test sonucu, raporların kimliği, sayaçlar ve stop koşulları deterministik kodla belirlenir. Modelin “geçti”, “yaklaşık %92”, “muhtemelen refactor şart” cümlesi bu alanlara veri olarak kabul edilmez. Ortamın kendisi nondeterministic olabilir; harness bu belirsizliği kaydeder, sihirli biçimde ortadan kaldırdığı iddiasında bulunmaz.

Builder test üretir; harness çalıştırır; bağımsız verifier kanıtı ve davranış kalitesini inceler. Verifier bir model kullanıyorsa onun yorumu da tartışmasız doğruluk kanıtı değildir. Deterministik metrikler, model değerlendirmesi ve kullanıcı onayı ayrı alanlarda kalır.

### 7.2 Maven çalıştırma sözleşmesi

Runner, doğrulanmış wrapper/kurulu Maven ve explicit argv/cwd/toolchain ile çalışır. `shell=True` veya modelden gelen serbest shell metniyle build başlatılmaz. Windows `.cmd`/wrapper desteği gerekiyorsa exact doğrulanmış wrapper için test edilmiş platform köprüsü kullan; genel shell/interpreter yasağını tüm runtime için kaldırma. Maven/JVM alt süreçleri timeout/cancel halinde beraber sonlandırılır ve receipt/cleanup sonucu yazılır.

Plan; gerekli modüller, test selection, plugin sürümleri, argLine/agent yüklemesi, unit-test ve coverage report komutlarını kaydeder. Sadece `test` goal'unun rapor ürettiği veya `verify` goal'unun yalnız unit test çalıştırdığı varsayılmaz. İlgili lifecycle'daki integration/deploy/custom exec etkileri önce tespit edilir. Coverage raporu unit-test execution verisinden üretilir; gerekiyorsa doğru goal dizisi seçilir. `forkCount=0`, agent override, atlanmış testler ve yanlış engine açık preflight/measurement tanılarıdır.

Mevcut test komutunun sonucu console'dan tek regex ile uydurulmaz; test raporları ve process sonucu birlikte kullanılır. Keşfedilen, çalıştırılan, failed/error/skipped/aborted testler ve modül bilgisi raporlanır. Aday testin hiç keşfedilmediği veya yalnız skip olduğu koşu kabul değildir. Hedef modülde sıfır test fail-closed değerlendirilir; test içermemesi doğal olan parent/utility modül otomatik bütün build hatası sayılmaz.

### 7.3 Yetki, kaynak ve build kilitleri

Agent dosya mutation'ı yalnız onaylı test/fixture alanlarında admitted write yolu ile olur. Production, POM, test/coverage ayarları, verifier varlıkları ve kullanıcı dosyaları test-only modda korumalıdır. Generated test kendi başına kod çalıştırabilir; Maven plugin ve test alt süreçlerinin dosya/ağ izinleri de execution boundary'sinin parçasıdır.

**Sonradan diff görmek tek başına önleyici sandbox değildir.** Gerçek platform desteğine göre agent tool izinleri + capability admission + process/file/network sınırları uygulanır. Desteklenen ortamda sınırın aşılamadığını negatif testle göster. Güvenli yürütme kabiliyeti olmayan platformda riskli build'i sessizce serbest çalıştırma; eksik kabiliyeti açıkça bildir. Var olan geniş CLI izinlerini evrensel sandbox kanıtı sayma. Yeni özel OS sandbox platformu kurmak yerine mevcut execution altyapısında gerekli en küçük, test edilmiş kontrolü kullan.

Aynı dosyaya tek writer yetmez: aynı reactor/module `target/`, `.exec`, report ve test output alanlarını paylaşan **build resource'ları** da kilitlenir. İlk sürümde proje/reactor build serialization kabul edilen sade çözümdür. Gerçekten ayrık output ve state kanıtı yokken iki Maven koşusunu paralel başlatma. Read-only analizler kaynak kapsamları uygunsa paralel olabilir.

Ortama erişim daraltılır; settings.xml secret'ları, environment credential değerleri ve özel kaynak içeriği prompt/log'a taşınmaz. Ağ/cache/bağımlılık ihtiyacı planda ayrılır. Offline Maven tek başına tüm build kodunun ağını engellemez; bunu “network isolated” diye raporlama.

### 7.4 Taze rapor ve source binding

Her ölçüm source revision, hedef production digest, test aday digest'i, build/config/adapter/toolchain kimliği, run/attempt ve report digest'iyle ilişkilidir. Yalnız dosya modification time'ı tazelik kanıtı değildir. Önceki JaCoCo exec veya Surefire raporunun yeni koşuya karışması engellenir. Raporu model yazamaz/değiştiremez. Çıktı alanı runner/verifier'ın denetimindedir; parser boyut ve format sınırlarıyla güvenli okur, dış XML entity/ağ çözümlemesi yapmaz.

Hedef Java dosyası → module + source root + package + sourcefile + ilgili class bytecode kimlikleri eşlenir. Aynı `Service.java` adının iki package/modülde bulunması doğru ayrılır. Kaynak/debug bilgisi yoksa veya eşleme belirsizse `measurement-incomplete/mapping-error` döner; yüzdelik 0/100 üretip devam etmez. Compiler-generated satırlar ve inner class etkisi resmî JaCoCo semantiğine göre ele alınır.

Test-only fazında production davranış snapshot'ı sabittir, test dosyaları her accepted candidate'da değişebilir. Mevcut source_revision/plan freshness kontratını gevşetmeden, değişen test adayını ayrıca bağla. Haricî kullanıcı edit'i veya hedef production değişikliği ölçümü stale yapar. Kabul edilen kendi test patch'ini haricî drift ile karıştıran sonsuz invalidation döngüsü kurma; bu ayrımı test et.

### 7.5 Sayaç matematiği

`covered`, `missed`, `total` tamsayı olarak saklanır. Eşik karşılaştırması tam hassasiyetli oranla yapılır; gösterim yuvarlaması kararı değiştirmez. Dosya oranlarının aritmetik ortalaması scope coverage değildir; scope için doğru sayaçlar toplanır. Aynı source satırı birden çok class/metotta sayılarak şişirilmez.

LINE için `sourcefile` sayaçları esas alınır; satır snippet'inde `ci > 0` olan kısmi instruction satırını bütünüyle missed sayma. BRANCH için covered/missed branch sayaçları ayrı tutulur. İstisna yolları ayrıca davranış planındadır; %100 branch'in bütün exception senaryolarını kapsadığı iddia edilmez. [E05]

`N/A` (doğrulanmış uygulanamaz ölçü), `NOT_MEASURED`, `MISSING_REPORT`, `MAPPING_ERROR` ve gerçek yüzde0 farklıdır. Örneğin branch içermeyen bir dosyada branch N/A olabilir; raporda kayıp dosya N/A değildir. Generated/excluded code durumu görünürdür; zaten excluded hedef kullanıcının onayı olmadan yok sayılıp hedefine ulaşıldı denmez. Kullanıcı hedefini aşmak için exclusion veya metrik değiştirmek yasaktır.

## 8. Davranış planı, ajanlar ve kalite

### 8.1 Rol organizasyonu

Mevcut assignment rollerini kullan; yeni rol isimleri gerektiğinde görev uzmanlığı/metadata olsun, gereksiz global enum ve agent çoğaltma yapma.

| Sorumluluk | Yetki / beklenen çıktı |
|---|---|
| Coordinator | Exact scope, bütçe, bağımlılıklar, fan-in, devam/durma. Kaynak ağacını okuyamayan mevcut coordinator kuralı korunur. |
| Analyzer / researcher | Hedef davranışlar, bağımlılıklar, mevcut testler, örtülmeyen riskler ve kaynak kanıtı. Read-only. |
| Test architect / planner | Davranış hedefi, oracle kaynağı, öncelik, yöntem seçenekleri ve gerekçeli katkı tahmini. |
| Test developer / builder | Onaylı test kaynaklarında sınırlı patch, test-case ↔ plan/risk ilişkilendirmesi. |
| Harness + independent verifier | Gerçek execution/metrik; bağımsız diff, oracle, kalite, scope ve kalan risk incelemesi. |

Analyzer ile planner küçük işlerde aynı read-only child görevinin ayrı çıktıları olabilir. Builder kendi değişikliğinin bağımsız verifier'ı olamaz; ayrı invocation ve açık kanıt zinciri gerekir. Bağımsızlık mutlaka başka marka model demek değildir. Her compile hatasında bütün analiz/plan ajanlarını yeniden çağırma; fixture/import repair kısa yoldan yapılabilir. Yeni risk, yanlış oracle veya plateau strateji değişimi ise replan nedenidir.

### 8.2 Plan bir test kotası değildir

Her önemli senaryo veya senaryo grubu: davranış sözleşmesi, risk/bozulabilecek davranış, ilgili kaynak, beklenen gözlem ve onun dayanağı, dependency/fixture ihtiyacı, kabul kriteri ve öncelik taşır. “A.java için tam 17 test” bağlayıcı hedef değildir; tahmini büyüklük bilgisi olabilir. Tahmini coverage katkısı garanti değil, ölçüm sonrası kalibre edilecek öngörüdür.

Planner kritik iş riskini küçük/kolay sınıfların coverage kazancına feda etmez. Covered satırlardaki zayıf assertion da gap'tir. Aynı eski öneriyi her tur tekrar üretmez; reddedilen yaklaşımı ve nedenini görür. Builder kapsam içinde daha iyi bir test stratejisi veya yeni kritik davranış bulduğunda gerekçeyi plan delta'sına ekleyerek ilerleyebilir; yeni yetki doğuruyorsa durur.

### 8.3 Beklenen sonuç kaynağı (oracle)

Kaynak türleri en az şu anlamları taşısın: açık kullanıcı/iş sözleşmesi, doğrulanmış dokümantasyon veya mevcut normatif test, matematiksel/domain invariant, mevcut davranışı kaydeden characterization, çözülmemiş varsayım. Her türü otomatik doğru sayma; çatışma ve güven düzeyi görünür olsun.

Production kodundan aynı algoritmayı kopyalayıp expected üretmek bağımsız oracle değildir. Aynı fonksiyonu hem expected hem actual için çağırmak, mock'a yazılan değeri geri almak ve tautology assertion kalite kanıtı değildir. Beklenti dayanağı belirsizse bunu gizleme; düşük riskli characterization ile normative doğruluğu ayrı tut. Karar gerektiren davranış belirsizliği `needs-specification` olur; unrelated güvenli senaryolar sürdürülebilir.

### 8.4 Testler ne kadar zorlayıcı olacak?

Girdileri ve sonuçları gerçek davranış riskine göre zorla. Uygun olduğunda sınır değerleri, geçersiz/eksik veri, dependency failure, exception propagation, state geçişi, yan etki, tekrar çalıştırma/idempotency, hassasiyet/yuvarlama, koleksiyon çoğulluğu, tarih/saat sınırları ve concurrency/regression düşünülür. **Bu liste örnek bir risk kataloğudur; her metoda her başlık uygulanacak checklist veya üst sınır değildir.** Uygulanmayan kategori için mekanik “N/A raporu” üretme yükü de yoktur.

Example-based, parameterized, property-based, metamorphic veya stateful yöntemlerden uygun olanı seç. Property/metamorphic ilişki gerçek sözleşmeden türemeli; örneğin indirim/limit fonksiyonuna doğrulanmamış “her yerde monoton” kuralı dayatma. Reproducible seed, fake clock, deterministic scheduler/latch gibi araçlar gerektiği yerde kullanılır; gerçekçi senaryo yerine keyfî sleep ile yeşil test üretilmez. Yeni framework otomatik kurulmaz.

Normal hedef observable davranıştır. Sırf private metoda ulaşmak için reflection, production API'yi gereksiz public yapma veya sistemin kendi kodunu mock'layarak test etme kabul edilmez. Mockito/static mocking gibi bir yöntemi yalnız adı nedeniyle toptan yasaklama; testin anlamlı sözleşmeyi doğrulayıp doğrulamadığını değerlendir. İstisnai bir erişim gereksinimi mevcut açık proje standardında gerçekten varsa bunu ayrı gerekçe/review konusu yap; gizli testability shortcut'ı üretme.

### 8.5 Kalite kapısı

Anlamlı assertion yalnız `assert*` kelimesi sayılarak belirlenmez: beklenen exception, state, side effect ve uygun interaction doğrulamaları da geçerli olabilir. Aynı şekilde assertion içeren her test kaliteli değildir. Static smell kuralları aday bulur; semantik review ve gerektiğinde mutation evidence sonuca katkı verir.

Eklenen testin yeni bir davranış/risk/assertion veya regression değeri bulunur. Duplicate test kümeleri sade tutulur; farklı önemli sınır örnekleri “aynı branch” diye silinmez. Coverage artışı0 olan güçlü test otomatik reddedilmez; neden değerli olduğu kaydedilir. Tam tersi, coverage arttığı için anlamsız test otomatik kabul edilmez.

Onaylı plandaki materyal risklerin durumu `verified / unresolved / accepted-exception` anlamlarıyla izlenir. Verifier plan dışında yeni materyal risk bulabilir. Hedef yüzde tutsa bile kritik unresolved oracle/behavior riski varsa kalite geçildi denmez. Kullanıcı açık risk kabul ederse bunun exact kaydı sonuçta görünür; model kendine istisna veremez.

### 8.6 Failing test repair sınırı

Hata şu sınıflardan kanıtla ayrılır: compile/syntax, yanlış import/framework, fixture/dependency setup, test expectation hatası, gerçek production defect, environment/toolchain, flaky, test discovery ve ölçüm/parser hatası. Sınıflandırma kanıtsız kesin teşhis gibi sunulmaz; `unknown` ve eskalasyon desteklenir.

**Gerçek bug bulan testi, hatalı production çıktısını expected yaparak “düzeltme”.** Reproducer ve oracle kanıtını koru; production defect proposal üret. Test-only koşuda kusuru düzeltemiyorsan başarı yerine bu blokajı göster. Başarısız testin yeri kontrollüdür: kullanıcının varsayılan suite'ine onaysız kırmızı değişiklik bırakmak zorunda değilsin; minimal patch/reproducer'ı artifact olarak saklayıp kendi adayını güvenli geri alabilirsin. Fakat kusur gizlenmez, success yazılmaz, flaky/skip adıyla yutulmaz.

Flaky testte tekrar koşu tanı içindir; tesadüfen yeşil gelene kadar retry kabul değildir. Kaç tekrar ve hangi sırada çalıştığı kaydedilir. Başlangıçtan gelen flaky/hatalı test ile yeni adayın regresyonu ayrılır; kritik acceptance kapısındaki kararsızlık varken tam doğrulandı denmez.

## 9. Iteratif döngü, durma ve production refactor

### 9.1 İlerleme akışı

Akışın mantıksal omurgası:

`scope → readiness/baseline → behavior/gap plan → onaylı test-only attempt → gerçek run/ölçüm → bağımsız kabul → kalan gap / uygun repair / replan → final verification`

Bu bir her tur aynı sayıda agent çağırma reçetesi değildir. Basit repair kısa olabilir, davranış değişimi daha derin değerlendirme gerektirir. Her accepted batch kanonik attempt ve kanıtla ilişkilidir. Aday patch'ler atomik/sınırlı uygulanır; reddedilen adayda yalnız görevin kendi değişikliği preimage/ownership kontrolüyle geri alınır, user dirty edit'ine dokunulmaz.

İlerleme yalnız yüzde delta değildir: yeni covered line/branch, anlamlı yeni davranış/assertion, çözülen oracle belirsizliği, verified mutation veya daha derin test için işe yarayan fixture hazırlığı olabilir. Aynı risk açıklamasını tekrar yazmak ilerleme değildir. Her ilerleme türünün kanıtı vardır; modelin yeni cümlesi novelty sayılmaz.

### 9.2 Bütçe ve plateau

Başlamadan önce scope'a uygun üretim/repair attempt, process timeout, toplam elapsed, output/context ve varsa remote call/cost bütçesi planda görünür olsun. Varsayılanlar konfigüre edilebilir ve “ilk kalibrasyon değerleri” olarak belgelenir; bilimsel optimum veya tüm projelerin üst sınırı diye sunulmaz. Kullanıcı onaylı aralık içinde gereksiz onay tekrarları olmaz; bütçe artışı mevcut izin kuralıyla yeniden planlanır.

Plateau için yalnız yuvarlanmış yüzdeye bakma. Aynı gap/risk kümesi, tekrarlanan başarısız yaklaşım ve doğrulanmış kazanımın durması birlikte değerlendirilir. Önce stale report/yanlış scope/eksik context ihtimali giderilir. Ardından kalan bütçede gerçekten farklı fixture/input/mocking/observable davranış stratejisi denenebilir. Gerekli setup adımı anında coverage getirmeyebilir; plan ona sınırlı keşif bütçesi verebilir. Final test suite bağımsız, assertion'lı ve okunabilir olmalıdır; sadece print üreten keşif script'leri unit test kabul edilmez.

**Plateau, “production koduna dokunmadan bu hedef imkânsızdır” kanıtı değildir.** Sistem “bu scope, yöntemler ve bütçeyle ek ilerleme bulamadım” diyebilir. Kaynak/bağımlılık yapısıyla desteklenen somut testability problemi ayrıca raporlanır. Bütçe bitmesini refactor zorunluluğu diye yeniden adlandırma.

### 9.3 Terminal durum ve recovery

Mevcut loop/state enum'larını tekrar kullan; ek reason/status gerekiyorsa sürümle ve mevcut okuyucuların uyumunu test et. Aşağıdaki anlamlar birbirinden ayrılmalıdır:

- Hedef ve zorunlu kalite/final doğrulama geçti.
- Başlangıçta zaten hedefteydi; yine kalite/ölçüm doğrulanmış veya yalnız oran gözlenmiş olabilir.
- Bütçe bitti / stagnation review gerekiyor.
- Ortam/araç/ölçüm/izin eksik, teknoloji desteklenmiyor veya specification belirsiz.
- Gerçek production defect / ayrı testability refactor onayı gerekli.
- Kullanıcı pause/cancel istedi veya kesinti nedeniyle recovery gerekiyor.

Bunlar aynı `success=true` içine gömülmez. CLI'da makine-okunur status/reason ve uygun exit davranışı dokümante edilir. Terminal receipt yoksa başarı yoktur. Cancel/timeout sonrasında yeni attempt başlayamaz; eldeki report ve patch sahipliği readback ile korunur. Resume aynı işi ikinci kez enqueue/execute etmez. Kaynak/config/plan değiştiyse eski başarı veya yetki yeni koşuya taşınmaz.

### 9.4 Ayrı testability refactor kapısı

Öneri; hangi observable davranışın test edilemediği, denenmiş test-only yolları, sorunlu dependency/state, en küçük refactor, etkilenebilecek API/yan etkiler, risk, rollback ve doğrulama planını içerir. Kullanıcı exact proposal'ı onaylamadan production yazı izni verilmez.

Onay sonrası örneğin dependency seam, clock injection veya küçük adapter ayrıştırması yapılabilir; hangi yöntem seçileceği kodun sorunudur. İş kuralı değişikliği, performans/transaction/concurrency semantiği değişikliği veya yeni özellik bu onayın otomatik parçası değildir. Bunlardan biri gerekiyorsa ayrıca kapsam kararı istenir.

Mevcut testler, characterization ve gerekli regression yeniden çalıştırılır; API/config/exception/side-effect değişiklikleri bağımsız incelenir. “Testler geçti, davranışın matematiksel olarak aynı kaldığı kanıtlandı” denmez. Refactor sonrası line/branch paydası değişebileceği için bağlı yeni source/ölçüm baseline'ı oluşturulur; eski yüzdeyle doğrudan kazanım hesabı yapılmaz. Eski kanıt korunur. Aynı target politikasında test döngüsü yeniden sürer.

## 10. Artifact sözleşmeleri ve kullanıcı yüzeyi

### 10.1 Veri akışı

Ajanların birbirine yalnız serbest sohbet veya dev prompt devretmesi yerine mevcut result envelope'a bağlı sürümlü kayıtlar kullan. Ayrı JSON dosyası şart değil; kanonik object/artifact store ve mevcut şema yaklaşımı uygundur. Required alanlar kodla doğrulanır, okunabilir açıklama ek alan olabilir.

| Mantıksal kayıt | İçermek zorunda olduğu bilgi |
|---|---|
| İstek/scope | Project/source binding, hedefler, metrik/eşik, ölçüm/regression scope'u, izinler, budget, revision. |
| Project/readiness | Build/test/coverage araçları, modüller, kaynak-test eşlemesi, destek/eksik/yan etki bulguları. |
| Baseline | Run/rapor kimlikleri, gerçek test sonuçları, sayaçlar veya neden ölçülemediği; config/source binding. |
| Davranış planı | Senaryo/risk ID, oracle dayanağı, öncelik, izinli test yolları; yöntem seçenekleri ve belirsiz tahmin. |
| Attempt/patch | Parent/ordinal, hangi plana bağlı, değişen dosya digest'leri, agent invocation, sahiplik ve execution referansları. |
| Execution/coverage | Gerçek process/test discovery/result, scoped counters, fresh report, production/test/config/toolchain digest'i. |
| Verification/gap | Bağımsız invocation, kalite bulguları, materyal risk durumu, kabul/red ve eksiklerin kanıtı. |
| Refactor proposal | Exact scope/risk/rollback/oracle, gereken ayrı yetki ve yeni source baseline bağı. |
| Final/continuity | Nereden nereye, hangi testler/önemli davranışlar, kalan risk/stop reason, receipt'ler ve sonraki güvenli adım. |

Minimum model çıktısı ile doğrulama yeterli olsun; ajana tüm internal UUID/manifest alanlarını uydurtma. Run/claim/receipt, gerçek metric, digest ve sıra kimliğini harness/store üretir. Geniş JSON sözleşmesini her agent promptuna bütünüyle basma; ilgili zarf ve örnek gerekirse yüklenir. Schema/version/source uyuşmazlığı sessizce düzeltilmez; kontrollü format repair veya açık rejection olur.

Ham zincirleme düşünce, transcript, secret, sınırsız terminal çıktısı tutulmaz. Gerekli sanitize failure özeti ve local evidence artifact'ı ayrı erişim/retention kuralıyla saklanır. Günlük/status komutları başarı yetkisi üretmez.

### 10.2 CLI ve doğal dil entegrasyonu

Mevcut komut ağacını incele; çakışma yoksa **yeni** `zekam test` altında `plan`, `run`, `status`, `report`, `pause`, `resume`, `cancel` yüzeylerini geliştir. Bunlar bu belge hazırlanırken mevcut oldukları iddia edilen komutlar değildir. Eşdeğer mevcut bir yüzey bulunduğunda ikinci alias ailesi ekleme; seçilen canonical isimleri usage/test/skill'de tutarlı kullan.

`plan` varsayılan salt-okunur/provider-free plan üretir. Source dosyasını metin olarak incelemekle gerçek test çalıştırmak ayrı aşamadır. Gerçek baseline için gereken yerel execution yetkisi mevcutsa kullanıcı isteğine uygun ölçüm yapılır; yoksa readiness ve exact execution planı gösterilir. Modelden davranış analizi almak gerekiyorsa onaylı mevcut agent route'u kullanılır; “provider-free plan” içinde gizli model çağrısı olmaz.

`run` exact plan ve yetkilerle çalışır; task/plan/budget değiştiren retry yetkisiz çalışmaz. Status/report sadece gerçek kaydı okur. Türkçe doğal dil routing'i skill'i bulup bu yüzeye bağlar; “unit test nedir?” genel açıklaması test yazma veya tool execution tetiklemez. Hedef dosyasız “testlerini yaz” mevcut görevde güvenilir exact hedef yoksa önce belirsizliği çözer.

### 10.3 Skill paketi

Canonical paket adı **`unit-test-engineering`** olsun; mevcut `src/zekam/skills/` düzenine uyarla. Root `SKILL.md` kısa amaç/tetikleme, temel sınır, entrypoint ve ihtiyaç halinde açılacak referansları içersin. Maven/JaCoCo ve hata teşhisi, oracle/kalite, refactor, devam/sonuç rehberleri küçük ilgili referanslarda kalabilir. Gerçek ölçüm kodu normal uygulama/adapter/test alanındadır; büyük iş mantığını Markdown veya aynı işi tekrarlayan skill script'lerine taşıma.

Paket gerçek skill lifecycle üzerinden inspect/evaluate/export ve yönetilen projection'a bağlansın. OpenCode varsayılan; Codex/Claude Code mevcut opt-in politikasına uyar. Aynı skill'in farklı client tarafından görünmesi o client'ta doğrulandığı anlamına gelmez. Paket içinde yeni bağımsız provider credential/model router saklanmaz. Kaynak dosyadaki veya skill referansındaki `allowed-tools` ifadesi, Zekam'ın gerçek işlem admission'ının yerine geçmez. [E04,R09]

## 11. Sınırlı ileri kalite: PIT ve model yardım profili

### 11.1 PIT pilotu

PIT opsiyonel capability/adaptör olarak implement edilsin. Onaylı sınıf/test/mutator ve timeout bütçesiyle çalışsın; gerekli plugin/engine uyumluluğu tanılansın, otomatik bağımlılık upgrade'i yapılmasın. `killed / survived / no-coverage / non-viable / timeout / memory-error / run-error` ayrımı ve ham sayaçlar korunsun; aracın hesapladığı skor ve bizim gösterdiğimiz skor varsa tanımları açık olsun. [E08]

Surviving mutant otomatik olarak test açığı değildir; equivalent veya scope dışı etki olasılığı review konusudur. Mutation, expected oracle'ı icat etmez. Pilotun en az bir kontrollü fixture'ında aynı line coverage korunurken daha güçlü assertion'ın yeni bir mutantı yakaladığı gerçek çalışmayla gösterilsin. Kaynak Java dosyasına sahte hatalar yazıp kullanıcının production'ını geçici değiştiren gizli yol kullanılmaz; PIT'in kendi bytecode işlemesi ayrı effect olarak sınırlandırılır.

PIT kullanılmadığında temel skill çalışabilsin ve `mutation_not_run` desin. Her görevde mutation zorunlu değildir; kullanıcı bunu acceptance koşulu seçerse başarısız/çalıştırılmayan PIT o koşulu geçirmiş sayılmaz.

### 11.2 Model/istemci yardım pilotu

Bir model–istemci kombinasyonu belirli şema/araç/adımda gerçekten takılıyorsa küçük örnek veya hata rehberi koşullu açılabilir. Bunu marka veya “küçük model anlamaz” varsayımıyla her oturuma basma. Yardımcı referansın işe yaradığı aynı görev/bütçe/sürümde ölçülsün. İzin ve kalite eşiği modele göre düşürülemez.

Gerekli `model_id` katalogdan exact alınır; provider prefix kesilmez, yetkisiz fallback yapılmaz. Mevcut MiniMax gibi kayıtların kimliği tahmin edilmez. Kullanılabilir modellerin yeni görevdeki yeterliliği geçmiş başka benchmark sonucundan türetilmez.

## 12. Uygulama iş paketleri ve bağımlılıklar

Aşağıdaki paketler **aynı görevin teslim dilimleridir**, başka konuşmaya bırakılmış fazlar değildir. Implementasyon ayrıntısı ve güvenli sıranın iyileştirilmesi ajana aittir; hiçbir zorunlu çıktı “sonraki faz” denerek eksiltilmez. Kaynak çakışması olmayan read-only analizler paralelleşebilir; aynı yazılabilir/build resource tek sahibindir.

| Paket | Bağımlılık | Teslim ve çıkış kanıtı |
|---|---|---|
| W00: Devir ve baseline | Yok | Eski görev/kanıt korunmuş, exact yerel root/HEAD/operational durum ve gerçek kalite baseline'ı; yeni kapsam Work/plan'a bağlanmış. |
| W01: Ortak talimat semantiği | W00 | Otomatik tarihsel yük kaldırılmış; global/coordinator/referans kapsamı uzlaştırılmış; G01–G08 regression'ları. |
| W02: Managed migration ve context ölçümü | W01 | Generator + projection birlikte güncel; bilinen eski/unknown drift/rollback/crash testleri; known/unknown etkin yük raporu. |
| W03: Unit-test domain ve yerel persistence | W00 | İstek, scope, observation ve attempt kontratları; mevcut operational port üzerinden gereken SQLite adaptasyonu; no-PostgreSQL regression. |
| W04: Maven/JaCoCo çalıştırıcısı | W03 | Gerçek süreç/test discovery/rapor parser'ı; permission ve build kilidi; taze dosya/modül sayaçları ve negatif koşular. |
| W05: Agentic test döngüsü | W03,W04 | Analyzer/plan/builder/verifier mevcut canonical dispatch ile bağlı; hata ayrımı, coverage/behavior gap, repair/replan, bounded continue. |
| W06: Refactor, kalite ve PIT pilotu | W05 | Production izni sınırı, oracle/bug koruması, opt-in gerçek mutation fixture'ı; bütçe ve denominator değişimi kanıtı. |
| W07: CLI, skill ve süreklilik | W02,W05 | Natural-language routing → gerçek skill/runtime; plan/run/status/report/control; managed export; resume/cancel/recovery kabulü. |
| W08: Bağımsız uçtan uca kabul | W06,W07 | Gerçek Maven fixture koşuları, adversarial negatif testler, regression ve bağımsız verifier; destek matrisi. |
| W09: Eşleştirilmiş değerlendirme ve teslim | W08 | Mevcut ablation altyapısına bağlı corpus/plan/report; izinli canlı koşular veya exact açık blocker; usage/runbook/DoD/receipt fan-in. |

W03 sırasında önce `application/operational_store.py` portunu ve gerçek yerel adaptörleri incele. Application servislerini doğrudan PostgreSQL/SQLite SQL çağrılarına bağlamak yerine mevcut port/unit-of-work sınırını koru. Gerekli schema değişikliği Zekam migration düzeni, migration testi ve rollback/backup politikasıyla yapılır; SQL adlandırma/alan/audit standardı mevcut projeye uyar. Ayrı authority database'i veya “geçici JSON state dosyası” ile kalıcı loop state'i ikame etme. [R10]

W05/W07'de yalnız fake agent stub'u yeterli değildir: gerçek canonical dispatch'e bağlanan production yolunun kodu bulunmalıdır. Test doubles offline kabulde kullanılabilir ve **replay/mock** diye işaretlenir. Gerçek modellerin davranış yeterliliği ancak ayrıca çalıştırılmış kabulden gelir.

## 13. Kabul senaryoları

Bu corpus **harness/skill ürününü test eder**; ileride her kullanıcı dosyasına bütün bu testleri yazdırma listesi değildir. Mevcut testleri genişlet; semantik olarak eşdeğer örneklerle daha iyi kapsama mümkünse uyarlamayı kaydet. Test isimleri, adetleri ve dosya dağılımı bağlayıcı değildir; aşağıdaki kabul davranışları bağlayıcıdır.

### 13.1 Genel Zekam kabulü

| ID | Durum | Beklenen doğrulanabilir sonuç |
|---|---|---|
| G01 | Selam/teşekkür | Route/doctor/RAG/subagent töreni yok; kısa sohbet. |
| G02 | Proje içermeyen kavram sorusu | Proje context'i veya unit-test execution açılmaz; güncel dış bilgi ihtiyacı ayrı route. |
| G03 | Yeterli pinned citation | Cevap aynı kanıttan; gereksiz source taraması/ikinci ask yok. |
| G04 | Stale veya kanıt yetersizliği | Snapshot sınırı belirtilir; yeni source/authority/başarı uydurulmaz. |
| G05 | Belirsiz Jira/proje | Key veya project_ref uydurulmaz; mevcut resolver korunur. |
| G06 | Küçük yetkili değişiklik | İlgili bağlam ve gerekli test kullanılır; bütün tarihsel belgeler zorunlu okunmaz. |
| G07 | Gerçek riskli/çok kaynaklı mutation | Gerçek child, bağımsız verifier ve gerekli regression atlanmaz. |
| G08 | Deterministik ACL/reparse problemi | Alakasız teşhis/fallback veya aynı başarısız çağrının döngüsü yok. |
| G09 | Tek projenin büyük/bozuk RAG state'i | O proje attention-required, diğerleri ve resume sağlam; `4042ad5` regresyonu korunur. |
| G10 | Temiz/eski/unknown managed kurulum | Bilinen eski geçer, kullanıcı metni korunur, unknown conflict olur, rollback güvenlidir. |
| G11 | Kaynak/ref içerisine yerleştirilmiş talimat | Yeni yetki veya system talimatına dönüşmez; yetkisiz effect reddedilir. |
| G12 | Compaction / model devri | Açık iş ve receipt/checkpoint'ten devam; aday araştırma uygulanmış veya tamamlanmış sayılmaz. |

### 13.2 Unit-test mühendisliği kabulü

| ID | Durum | Beklenen doğrulanabilir sonuç |
|---|---|---|
| U01 | Hesap/eşik/yuvarlama | Sınır bozulmasını yakalayan doğru dayanaklı test; expected kopyalanmış algoritma değil. |
| U02 | State ve yan etki | Doğru önce/sonra, interaction veya idempotency sözleşmesi; yalnız satır çağrısı değil. |
| U03 | Coverage zaten yüksek, assertion zayıf | Güçlü ve değerli test line artmasa da korunur; delta0 otomatik red değildir. |
| U04 | Bilinen gerçek production bug | Kusur/reproducer korunur; expected bozuk sonuca uydurulmaz, hedef geçti denmez. |
| U05 | Derin branch'e erişim için fixture | Bütçeli alternatif setup denenir; ilk plateau'da sahte refactor zorunluluğu yok. |
| U06 | Aday keşfedilmedi / tümü skipped | Exit0 başarı sayılmaz; no-tests başlangıç durumu ile discovery defect ayrılır. |
| U07 | Eski, kayıp, bozuk veya yanlış dosya raporu | Taze ölçüm yerine kullanılamaz; mapping/error sebebi açık. |
| U08 | Flaky test / nondeterministic dependency | Yeşile kadar retry yok; sınırlı tanı, deterministik çözüm veya açık kararsızlık. |
| U09 | Eksik araç/bağımlılık/coverage | Sessiz POM/network/global değişiklik yok; setup planı ve ayrı izin. |
| U10 | Planda olmayan değerli kapsam-içi davranış | Builder keşfi kabul/review akışına girer; sabit test sayısı sınırı engellemez. |
| U11 | Production refactor | Onaysız write engellenir; onaylı exact kapsam, yeni baseline, regression ve rollback kanıtı. |
| U12 | Kesinti/source drift | Aynı attempt tekrar effect üretmez; stale sonuç onay olmaz, doğru recovery. |

### 13.3 Ölçüm ve işlem sınırı için zorunlu karşı örnekler

| ID | Karşı örnek | Kabul şartı |
|---|---|---|
| M01 | İki module/package altında aynı Java basename | Exact hedef doğru rapora bağlanır; ilk eşleşme seçilmez. |
| M02 | Inner class ve aynı kaynak satırını paylaşan metotlar | Sourcefile LINE sayacı çift sayılmaz. |
| M03 | `ci>0`, `mi>0` kısmi instruction satırı | JaCoCo LINE anlamı korunur; fully-covered instruction ile karıştırılmaz. |
| M04 | Yüzdelerin ortalaması ile ağırlıklı toplam farklı | Doğru sayaç matematiği ve her-dosya/aggregate politikası ayrı sonuç üretir. |
| M05 | Gösterimde %90'a yuvarlanan ama hedef altı oran | Kabul başarısız kalır. |
| M06 | Branch yok / source mapping yok / debug yok | Uygulanamaz, eksik ve desteklenmeyen durumlar ayrı; sahte0/100 yok. |
| M07 | Eski `.exec` + yeni XML veya başka koşunun test raporu | Kaynak/run/config bağı doğrulanmadan ölçüm kabul edilmez. |
| M08 | Integration test coverage'ının unit scope'a karışması | Karışım reddedilir/ayrıştırılır; unit hedefine gizli katkı yok. |
| M09 | Aday test veya plugin production/verifier/report'a yazmayı dener | Gerçek işlem sınırı engeller; yalnız sonradan tespit etmek yeterli değildir. |
| M10 | İki ayrı test writer aynı Maven output alanını kullanır | Build kilidi/serialization korur; report yarışı olmaz. |
| M11 | Shell metni, path traversal, wrapper drift, secret'lı environment | Yetkisiz çalıştırma/erişim engellenir; secret çıktıya girmez. |
| M12 | Target reached raporundan sonra haricî source değişir | Eski başarı yeni source için geçerli sayılmaz; yeniden doğrulama gerekir. |
| M13 | Aynı accepted test patch'i sonraki attempt'ın girdisi | Kendi aday değişimi doğru bağlanır, sonsuz source-drift/replan oluşmaz. |
| M14 | SQLite yeniden açma / yarım effect / duplicate replay | Durable state ve claim/receipt tutarlı; aynı işlem ikinci kez uygulanmaz. |
| M15 | PostgreSQL yok, native-only eski driver yok | Yeni yerel kontrat ve fixture yolu legacy bağımlılık başlatmadan çalışır. |
| M16 | Malformed/oversized XML, dış entity referansı | Bounded parser ve ağsız işleme; sahte ölçüm/sonsuz okuma yok. |
| M17 | Managed apply ortasında crash, ardından kullanıcı edit'i | Recovery/rollback kullanıcı edit'ini ezmez; conflict görünür. |
| M18 | Düşük context bütçesi ve zorunlu güvenlik bilgisi | Required bilgi kesilerek çağrı yapılmaz; ölçüm niteliği ve blokaj dürüst. |

## 14. Gerçek kabul, benchmark ve sonuç iddiaları

### 14.1 Yerel entegrasyon kabulü

Zekam'ın kendi yeni unit/integration/architecture testleri, mevcut ilgili regression'lar, schema/paket doğrulaması, lint/type-check ve gerçek Maven fixture koşuları çalıştırılır. Varsayılan engine ve test selection kontrol edilir. Sentetik model replay sonucu ile gerçek Maven execution iki ayrı kanıttır: replay agent orchestration'ı sınayabilir ama model kalitesini kanıtlamaz; parser fixture'ı da gerçek Maven testinin yerine geçmez.

Java fixture kampanyasında en az şu yollar gerçek runner üzerinden kanıtlansın: düşük başlangıçtan ilerleyen test-only döngü, başlangıçta test bulunmaması, birden çok dosyada farklı hedef sonuçları, no-discovery/skip reddi, geçerli oracle'ın ürün hatası bulması, yetkisiz refactor reddi, izinli fixture refactor sonrası yeni ölçüm, durdurma/kesinti ve aynı run'dan güvenli devam. PIT pilotu ayrı opt-in koşu olur.

Windows native çalıştırma desteğini unit mock'larıyla “kanıtlandı” sayma. Kullanılabilir yerel/CI ortamı yoksa uygulanmış kodu ve eksik gerçek platform koşusunu ayrı göster. Gerekli araç kurulu değilse onaylı setup planını hazırla; çıktıları elle doldurma. Kullanıcının gerçek iş projesi kabul için kendiliğinden açılmış değildir.

### 14.2 Model–istemci değerlendirmesi

Mevcut `ScaffoldingAblationService` ve benchmark/campaign sözleşmeleri temel alınır. Aynı hedef, target source, araç/izin, bütçe ve bağımsız gold oracle altında karşılaştır:

- **A:** Mevcut ilgili talimat içeriğiyle referans çalışma.
- **B:** Hedef ve güvenlik korunarak sade/scoped talimat.
- **C:** B + yeni unit-test skill/runtime bağlantısı (yalnız uygun görevlerde).
- **D:** C + yalnız gözlenen ihtiyaca göre sınırlı yardımcı referans (pilot).

A→B ortak talimat etkisini, B→C unit-test yeteneğini, C→D yardım katkısını ayırır. A kolunda eski tehlikeli izin açığını yeniden üretme; bütün kollarda aynı güncel zorunlu güvenlik tabanı vardır. Varyantlar aynı kullanıcı hedefini taşır; birine rapor biçimi diğerine hata bulma hedefi vererek “kısa prompt daha iyi” sonucu çıkarılmaz.

Ölç: scope/authority ihlali, artifact validity, gerçek test pass/discovery, hedef coverage, oracle/risk doğruluğu, mümkünse mutation ve seeded-defect yakalama, yanlış completion/erken refactor, loop sayısı/repair yükü, token/elapsed/call/cost, ilgili skill/reference'ın gerçekten yüklenmesi. Tek toplam skor kaliteyi gizlemesin. Başarısız görevler metrik paydasından çıkarılmaz. Küçük örneklemde kesin genelleme veya bütün modellerde başarı iddiası yoktur.

Önce offline corpus ve plan/replay entegrasyonu; ardından mevcut model kataloğundaki uygun en az iki model–OpenCode kombinasyonu için izinli qualification hedeflenir. Model yoksa veya açık kampanya izni yoksa o kabul `BLOCKED_AUTHORIZATION/NOT_AVAILABLE` olarak kalır. Plan modele ait exact ID, toplam çağrı bütçesi, veri export kapsamı ve replay farkını gösterir; ayrıca onay olmadan gerçek provider çağrılmaz. Bir modelin başarısı tüm-model qualification veya `ZEKAM-DOD-025` kabulü değildir.

### 14.3 DoD ve dürüst teslim statüsü

**Kod/yerel entegrasyon tamam** demek için W00–W08'in zorunlu çıktıları implement edilmiş, ilgili otomatik testler ve gerekli gerçek yerel fixture akışları geçmiş, bağımsız verifier sonucuyla aynı kaynak revision'a bağlanmış olmalıdır. Gerekli bir platform/tool kabulü koşulamadıysa o madde açık kalır. Tek seferde dosyalar üretildi diye kapanış yapılamaz.

**Model/istemci için doğrulanmış** demek için W09'daki o exact model–client–config kombinasyonunun gerçek kabul sonucu gerekir. **Kullanıcı kurulumuna dağıtılmış** demek için managed apply/readback receipt gerekir. Bu üç statü ayrı gösterilir. Kod hazır ama canlı model veya global deployment kapısı eksikse bunu “her şey tamam” diye sunma; aynı zamanda güvenli yerel kod geliştirmesini baştan durdurma.

Gerekli authorization veya dış ortam bulunmuyorsa yalnız bağımlı effect durur, bağımsız yerel iş devam eder. Son durumda exact blocker, tamamlanan kapsam, geçilen testler ve tek sonraki güvenli işlem kaydedilir. Kullanıcıdan daha önce verdiği hedef/karar tekrar istenmez. Yeni zorunlu bilgi kaynaktan çözülebiliyorsa önce oradan çözülür.

## 15. Teslim dosyaları, bakım ve son yanıt

Bu görevin sonunda repository'de normal proje düzenine uygun olarak çalışan source/adapter/CLI, gerekli schema/migration'lar, ince skill paketi, managed generator ve migration, kabul fixture/testleri, destek matrisi ve kısa kullanım/recovery/runbook bulunur. Ayrı bir devasa belge seti sırf task maddesi doldurmak için üretilmez; mevcut dokümanlar genişletilebilir.

Kullanıcı çalışma artifact alanında ise machine-readable final verification ve okunabilir kısa rapor bulunur. En az: source/config/plan revision, eski/yeni scope sayaçları, eklenen önemli davranışlar/testler, production değişikliği varsa exact onay, real/replay/not-run ayrımı, tool/platform/model desteği, quality riskleri, attempt/claim/receipt bağı ve rollback/next safe action.

Kaynak referansları ve D01–D34 seçimleri mevcut karar/knowledge düzenine kısa biçimde taşınabilir. Araştırma metnini otomatik system prompt veya Work authority yapma. Kullanıcının notları/projeleri silinmez. Kod aktarımı düşünülürse kaynak commit ve lisans uygunluğu ayrıca incelenir; bu görev dış projelerin kodunu veya datasetini sınırsız kopyalama izni değildir.

Final kullanıcı yanıtı kısa ve Türkçe olsun: gerçekten ne değişti, hangi gerçek testler geçti, ne uygulanmadı/izin bekliyor, dosya/kanıt yolu. Uzun kod önizlemesi, ham terminal veya gizli düşünce gösterme. Kanıt yoksa sayı/başarı icat etme. Görev yalnızca bu belgedeki kapsamı içerir; “fırsat buldum” diyerek başka Zekam projeleri/özellikleri ekleme.

## 16. Kaynak ve hazırlık izleri

### 16.1 Bu tur tekrar kontrol edilen başlıca dış kaynaklar

Bu listedeki yayınlar dayanak ve karşılaştırmadır, bağımlılık kurulum listesi değildir. Dokümanların güncel sürümleri değişebilir; uygulama sırasında kullanılan gerçek tool sürümü sabitlenip kendi dokümanıyla eşleştirilir. Aşağıdaki özetler kaynakların bütün sonuçlarının yeniden üretildiği anlamına gelmez.

- **E01:** OpenAI, *Rethinking skills and prompts for GPT-6 Astra*, 11 Eylül 2026. Göreve özgü description, progressive disclosure ve aşırı reçeteleri yeniden değerlendirme. https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra
- **E02:** Anthropic, *Skill authoring best practices*. Özgürlük düzeyini görevin kırılganlığına göre ayırma, gerektiğinde workflow/validator, farklı modellerde kabul. https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices
- **E03:** *SkillsBench*, incelenen v4. Skill faydası ve ağır/uygunsuz workflow zararları; çoklu harness ve kontrollü değerlendirme sınırlamaları. https://arxiv.org/html/2602.12670v4
- **E04:** OpenCode resmî *Rules* ve *Agent Skills*. Global/project instruction yükleri, skill metadata/discovery/permission ve format koşulları. https://opencode.ai/docs/rules/ ; https://opencode.ai/docs/skills/
- **E05:** JaCoCo, *Coverage Counter*. LINE/BRANCH ve sourcefile semantiği; exception kapsamının sınırı. https://www.jacoco.org/jacoco/trunk/doc/counters.html
- **E06:** JaCoCo, *Maven Plug-in* ve *report goal*. Agent/rapor yapılandırması ve varsayılan verify fazı. https://www.jacoco.org/jacoco/trunk/doc/maven.html ; https://www.jacoco.org/jacoco/trunk/doc/report-mojo.html
- **E07:** Apache Maven Surefire, *test mojo*. Discovery, failIfNoTests, skip/engine/fork davranışları. https://maven.apache.org/surefire/maven-surefire-plugin/test-mojo.html
- **E08:** PIT, *Basic concepts*. Mutator, equivalent mutation ve farklı sonuç sınıfları. https://pitest.org/quickstart/basic_concepts/
- **E09:** Jain ve Le Goues, *TestForge: Feedback-Driven, Agentic Test Suite Generation*, 2025. Execution/coverage feedback ile iteratif test üretimi. Python benchmark sonuçları Zekam/Java başarısı olarak taşınmadı. https://arxiv.org/abs/2503.14713
- **E10:** *Evaluating AGENTS.md: Are Repository-Level Context Files Helpful for Coding Agents?* Abstract tekrar erişildi; önceki rapordaki v3 HTML ayrıntısı bu tur yeniden açılamadı. Önceki sayılar bu görevin kabul eşiği değildir. https://arxiv.org/abs/2602.11988

### 16.2 Zekam kaynak kanıtları

Bütün R referansları `4042ad598814a5d12bb1e9493d03ec169050427b` kaynak snapshot'ına bağlıdır. Kaynak okundu, bu hazırlıkta runtime'ı çalıştırılmadı.

- **R01:** `opencode.json`: otomatik instructions listesi. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/opencode.json
- **R02:** `NIHAI_UYGULAMA_PROMPTU.md`: superseded statüsü. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/NIHAI_UYGULAMA_PROMPTU.md
- **R03:** `src/zekam/application/client_instruction_bootstrap.py`: exact managed-body ownership ve update. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/src/zekam/application/client_instruction_bootstrap.py
- **R04:** `src/zekam/application/loop_service.py`: canonical loop admission/dispatch/validation. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/src/zekam/application/loop_service.py
- **R05:** `src/zekam/application/loop_orchestrator.py`: one-job-per-attempt, digest ve progress binding. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/src/zekam/application/loop_orchestrator.py
- **R06:** `src/zekam/application/measured_loop_runtime.py`: somut PostgreSQL contract loader ve native-only pinned driver. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/src/zekam/application/measured_loop_runtime.py
- **R07:** `src/zekam/application/context_recipe.py`: rol/typed context ve authority-free packet. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/src/zekam/application/context_recipe.py
- **R08:** `src/zekam/application/scaffolding_ablation.py`: paired gates, review-required; otomatik deletion yok. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/src/zekam/application/scaffolding_ablation.py
- **R09:** `src/zekam/application/skill_packages.py`: instruction-only projection, metadata, containment, authority-free plan. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/src/zekam/application/skill_packages.py
- **R10:** `src/zekam/application/operational_store.py`: transport-neutral records ve explicit unit-of-work portu. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/src/zekam/application/operational_store.py
- **R11:** `src/zekam/application/active_task_contract.py`: bu belgenin uyduğu header sözleşmesi. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/src/zekam/application/active_task_contract.py
- **R12:** `.opencode/agents/zekam-coordinator.md`: genel soru/route/RAG/dispatch sınırları. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/.opencode/agents/zekam-coordinator.md
- **R13:** Yaşayan eski görev ve son commit: açık işler yeni dosyayla completed olmaz; RAG state izolasyonu korunur. https://github.com/mehmet-karacan/zekam/blob/4042ad598814a5d12bb1e9493d03ec169050427b/AKTIF_GOREV.md ; https://github.com/mehmet-karacan/zekam/commit/4042ad598814a5d12bb1e9493d03ec169050427b

### 16.3 Önceki araştırmayı bağlayan dosya kimlikleri

Bu üç dosya hazırlanırken gerçekten okunmuştur. Ekli olmamaları uygulamayı engellemez; bu aktif görev temel karar ve kabul kapsamını kendi içinde taşır. Aynı adlı fakat farklı içerikli dosya, aşağıdaki snapshot'ın aynısı sayılmaz.

| Dosya | SHA-256 |
|---|---|
| `ZEKAM_UNIT_TEST_ARASTIRMA_RAPORU.md` | `4976ed7321059c89fe997e94c3d08882fb11f0e14c7be68951f5981c075014a1` |
| `ZEKAM_BAGLAM_SKILL_ARASTIRMA_EKI.md` | `7d257abc2f81a2471719a4a37fc997030be9a5cf1fdde59325d342101ce2523c` |
| `KARAR_ADAYLARI_GUNCEL.md` | `2a792c3631fd6aef75a29db74b9e7cfd3798d3b0d687a0c4dffcd3c183c18e02` |

## 17. Uygulayıcıya ilk eylem

Bu dosyayı geliştirme görevi olarak aldığında yeni bir araştırma veya kapsam onayı istemeden **W00'ı başlat**: mevcut görev ve gerçek kaynak durumunu koruyarak uzlaştır, kanonik iş/plan bağını kur, ilgili baseline'ı al ve W01/W03 için gerçek alt ajan görevlerini hazırla. İlk güvenli kaynak değişikliğine geç. Bütünü tekrar yorumlayan uzun bir planla yetinme; bu sözleşmenin çıktıları çalışan kod ve test kanıtıdır.
