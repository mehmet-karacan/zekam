---
schema: zekam-active-task/v2
task_id: ZEKAM-NATIVE-CLI-WORKSPACE-001
status: APPROVED_ACTIVE_TASK
title: Zekam Native CLI ve Yalin Proje Baglami Gecisi
created_at: 2026-10-05T02:20:49+03:00
baseline_repository: mehmet-karacan/zekam
baseline_branch: main
baseline_head: f37ab301510bbe0045dab265de36ea1475775f33
baseline_commit_subject: "config: gecersiz reranker hedeflerini ertele"
baseline_is_fixed_revision: true
legacy_postgresql_data_import: FORBIDDEN
postgresql_runtime_dependency: FORBIDDEN
docker_required_for_zekam_core: false
ui_surface: FORBIDDEN
push_authorized: false
runtime_test_evidence_at_task_creation: NOT_EXECUTED
---

# AKTIF_GOREV.md
## Native CLI, yalnız Zekam çalışma alanı ve amaca odaklı bağlam

## 1. Uygulanacak karar

**Bu dosya araştırma istemi değil, araştırmaya dayalı uygulama görevidir.** Kullanıcı bu dosyayı uygulamak üzere verdiğinde güncel kaynakla ilgili farkları doğrula; aşağıdaki değişiklikleri gerçek Zekam repository'sinde geliştir, test et ve kanıtlarıyla teslim et. Aynı araştırmayı baştan yapıp yalnız öneri raporuyla dönme. Teknik ayrıntıları mevcut mimariye göre seç; belirtilen sonuçları ve güvenlik sınırlarını değiştirme.

**Zekam, coding CLI'ın önüne geçen zorunlu bir ajan yöneticisi olmayacak. Kullanıcının seçtiği CLI doğrudan çalışacak; Zekam kendi çalışma alanında amaç, gerekli yerel bilgi, isteğe bağlı yardımcı araçlar ve doğrulanabilir süreklilik sağlayacak.**

Kullanıcı Zekam kökünde doğrudan `opencode`, `codex`, `claude` veya `gemini` açabilmeli. Başka bir projede aynı executable'ı açtığında Zekam otomatik olarak devreye girmemeli. CLI'ın kendi model seçimi, planlama, kaynak okuma/düzenleme, araçları, MCP bağlantıları, skill keşfi, subagent'ları, oturum yönetimi ve güvenlik mekanizmaları korunmalı. Zekam yeni native özellikleri yeniden uygulamaya veya kendi araç listesine sığdırmaya çalışmamalı.

Bu geçiş Zekam'ın bilgi, görev, kanıt, yedekleme veya test mühendisliği yeteneklerini silmek değildir. **Yürütme yöntemini host'a bırak, Zekam'a özgü veriyi ve deterministik kontrolleri gerektiğinde kullanılabilir tut.**

### 1.1 Birlikte teslim edilecek sonuçlar

- Dört CLI için ince, doğru keşfedilen, yalnız Zekam çalışma alanına ait giriş; global müdahale ve zorunlu koordinatör olmadan kullanım.
- Mevcut global ve başka projelere dağıtılmış Zekam parçalarından, kullanıcı içeriğini koruyan planlı geçiş; tekrar dağıtımı engelleyen üretici değişiklikleri ve geri alma.
- Amaca, gerekli yerel gerçeklere ve kabul kanıtına odaklanan bağlam/skill düzeni; istemciden bağımsız yardımcı araçlar, süreklilik ve mevcut unit-test ölçümünün kullanılabilirliği.

Bu belgenin ayrıntıları geliştirme sözleşmesidir. **Belgenin tamamını her yeni oturumun system prompt'una veya her skill'e yerleştirme.** Geliştirme sırasında ilgili bölüm okunur; ürünün sürekli yüklediği giriş çok daha küçük kalır.

### 1.2 Yetki ve kanıt sınırı

`APPROVED_ACTIVE_TASK`, mevcut parserın görev statüsüdür; kendiliğinden operasyonel yetki, claim, lease, provider çağrısı veya tamamlanma kanıtı değildir. Kullanıcının bu dosyayı uygulama için vermesi, bu repository'deki kapsam içi geliştirmeyi seçer. Gerçek kullanıcı-geneli ayarları değiştirmek, başka projelere yazmak, yeni araç/bağımlılık kurmak veya canlı model kampanyası başlatmak kendi açık izinlerini gerektirir. Push yetkisi yoktur.

Araştırma sırasında güncel GitHub kaynakları hedefli olarak salt okunur incelendi ve resmî belgeler karşılaştırıldı. Kullanıcının makinesindeki CLI sürümleri, kurulum dosyaları, model hesapları ve runtime davranışı gözlemlenmedi. Repository testleri, gerçek CLI kabul koşuları ve Maven/JaCoCo bu hazırlıkta çalıştırılmadı. Dokümandaki kaynak bulguları ile uygulamada üretilecek runtime kanıtını karıştırma.

## 2. Görevi kayıpsız devral

Önce mevcut `AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md`, yaşayan görev/projection ve ilgili durum kayıtlarını oku. Bu yeni görevde açıkça değiştirilen eski koordinatör/dağıtım/talimat politikalarını geçiş hedefi olarak ele al; eski kuralın kendisini değiştirmeyi yasaklayan döngüye girme. Güvenlik sınırlarını veya operasyonel yetki kontrollerini bu gerekçeyle atlama.

Önceki `ZEKAM-UNIT-TEST-AND-CONTEXT-001` görevinin ve YAML projeksiyonunun exact byte/digest kaydını mevcut arşiv yaklaşımıyla koru. Açık işlerini, canlı model qualification eksiklerini ve bağlantılı önceki backlog'u yeni görevle tamamlandı sayma. Örtüşen sadeleştirme işlerini ilişkilendir; aynı işi ikinci kez üretme. Yeni dosya kullanıcı tarafından köke önceden konmuşsa eski committed sürümü Git'ten ayır; kaybolmuş yerel değişikliği geri getirmiş gibi sunma.

Yeni görev için mevcut `ActiveTaskContract.load(Path('AKTIF_GOREV.md'))` doğrulamasını ve repository'deki deterministik projection üreticisini kullan. Elle yeni bir authority YAML'ı veya uydurma source digest yazma. Mevcut şemanın izin vermediği header alanları ekleme. [R10]

Yerel baseline olarak gerçek kaynak kökü, HEAD/branch, `git status --short`, yüklenen `zekam` paketinin konumu, Python ve kurulu CLI sürümleri, görev digest'i ve ilgili aktif iş/lease durumunu kaydet. Araştırma HEAD'ine reset etme; ilerlemiş kaynakta ilgili diff'i esas al. Kullanıcı değişikliklerini koru; toplu stash, `reset --hard`, kopya proje, mirror veya detached worktree oluşturma. Geçici kabul fixture'ları kullanıcı projesi kopyası değil, ayrı Zekam-owned sentetik girdiler olmalı.

Önceden var olan test/ortam arızalarını yeni regresyonlardan ayır. Kapsam dışı bütün Global DoD/backlog'un kapanmasını bu işin önkoşulu yapma; ilgili güvenlik ve kalite kapıları korunur. Gerekli yetki yoksa yalnız o etkili adımı beklet, güvenli bağımsız geliştirmeyi sürdür.

## 3. Araştırmanın karara etkisi

### 3.1 Resmî kaynak karşılaştırması

| Bulgular | Bu görev için karar |
|---|---|
| OpenAI'nin 11 Eylül 2026 tarihli GPT-6 Astra rehberi, uzun/çakışan skill açıklamalarının ve gereksiz yöntem reçetelerinin yeniden değerlendirilmesini öneriyor. [E01] | Skill'leri topluca silme. Hedef, gerekli bilgi ve sınırları koru; gereksiz yönlendirmeyi azalt. Evrensel “uzun skill modeli kötüleştirir” yasası veya ölçülmemiş başarı yüzdesi ileri sürme. |
| Agent Skills standardı metadata, etkinleşen gövde ve gerektiğinde açılan kaynakları ayırıyor. [E11] | Başlangıçta küçük keşif bilgisi; gerçek ihtiyaçta ilgili prosedür/kod/referans. Format limitini ideal talimat uzunluğu sanma. |
| Codex kullanıcı-geneli talimatlarla Git kökü–çalışma dizini zincirini birlikte kullanıyor; instruction keşfi oturum başında yapılıyor. [E02] | Global kalıntıları ve override dosyalarını denetle; yalnız `cd` ile eski oturum bağlamının temizlendiğini varsayma. |
| Claude Code'un doğrudan `AGENTS.md` okuması v2.1.277+ ve discovery koşullarına bağlı; yerel `CLAUDE.md` içindeki `@AGENTS.md` uyumluluk yolu belgelenmiş. Eager import dosyaları yine başlangıç bağlamına giriyor. [E05] | İnce ortak giriş ve gerektiğinde ince compatibility import. Belge bölmek tek başına token tasarrufu değildir. Global ayarla zorla yeni modu açma. |
| Gemini workspace/ancestor bağlamını ve dosya erişiminde JIT keşfi kullanıyor. [E07] | Dosyanın görülmesini, Zekam otomasyonunun yetkili olarak etkinleşmesinden ayır. Klasör adı kontrolünü güvenlik sınırı yapma. |
| OpenCode `AGENTS.md` ile config `instructions` içeriğini birleştiriyor; AGENTS içindeki `@file` referansını otomatik import etmiyor. [E09] | Dört CLI'a aynı import sözdizimini körlemesine yazma. Ortak giriş gerçekten anlamlı metin içersin; config aynı metni tekrar eklemesin. |
| Codex `.agents/skills`; Gemini `.agents/skills` ve `.gemini/skills`; OpenCode `.agents`, `.claude`, `.opencode` skill konumlarını keşfedebiliyor. [E03,E08,E10] | Her istemci dizinine bağımsız kopya koymak yerine mevcut generator üzerinde kontrollü projection; çapraz keşif/duplicate testi zorunlu. |
| Claude rehberi sürekli geçerli kısa proje bilgisini görev-özel skill rehberinden ayırıyor. [E04,E06] | Jira, benchmark, RAG hata reçeteleri ve test mühendisliği ayrıntıları her açılışa yüklenmez. |

Bunlar kaynak bulgularıdır. Aşağıdaki proje sınırı, geçiş ve kabul tasarımı Zekam için seçilmiş mühendislik kararlarıdır; kaynakların Zekam üzerinde ölçülmüş performans iddiası değildir.

### 3.2 Gerçek kaynakta değişmesi gereken yerler

| Doğrulanan durum | Sonuç |
|---|---|
| `opencode.json`, `zekam-coordinator` default agent'ını ve geniş `allow` izinlerini tanımlıyor; `00_BASLA.md` ve `DEVAM_PROTOKOLU.md` dosyalarını ayrıca yüklüyor. [R02] | Default agent ve Zekam'ın dağıttığı genel izin override'ları kaldırılacak; talimat yükü tekilleştirilecek. |
| Koordinatör doğrudan kaynak okumayı/düzenlemeyi yasaklıyor, child başarısızsa ana ajanın işi yapmasını engelliyor ve OpenCode'a özel router akışını dayatıyor. [R03] | Native ana ajanın yetkili çalışması engellenmeyecek; zorunlu rol ve model yönlendirme katmanı kaldırılacak. |
| OpenCode bootstrap global agent/plugin konumlarına göre çalışıyor; lifecycle plugin başlangıcında global spool dizini oluşturuyor. [R04] | Yalnız prompt'u koşula bağlamak yetmez. Otomatik yan etki üreten kurulum ve lifecycle girişleri de değişecek. |
| Instruction ve hook bootstrap'ları global dosyaları yönetiyor. Hook CLI, hata durumunda `PreCompact` için `continue: false` döndürebiliyor. [R05,R06,R07] | Yeni varsayılanda global hook yok; gözlem hatası native yaşam döngüsünü yönetmeyecek. |
| Typed entegrasyon kimliklerinde Gemini yok; eski varsayılan OpenCode açık, Codex/Claude kapalı. [R08] | Dört istemcili proje-yerel destek, schema/policy/consumer katmanlarıyla birlikte gelecek. Eski boolean'ları topluca true yapmak çözüm değil. |
| `.ai/repository-context.json`, eski ana prompt'u, YAML active_work'ü ve sabit subagent sayısını içeriyor. [R09] | Metadata ve tüketicileri yeni authority ve native politika ile uzlaştırılacak. |
| Unit test runtime binding'i gerçek OpenCode executable'ı istiyor ve composition `opencode_adapter` kuruyor. [R16] | Diğer CLI'lardan kullanılacak ölçüm/kanıt yolu bu bağımlılıktan ayrılacak; mevcut explicit batch yolu korunacak. |
| Mevcut continuity CLI, yerel session/source ve admission bileşenlerini zaten kullanıyor. [R15] | İkinci bir süreklilik veritabanı kurma; var olan akışın native istemcilerden kullanılabilirliğini tamamla. |
| Paired scaffolding evaluator zaten kalite/güvenilirlik/token/gecikme/maliyet kapıları ve rollback bağı kuruyor. [R13] | Yeni benchmark platformu yerine mevcut değerlendiriciyi kullan. |

**Önceki bulguyu tekrarlama:** Güncel `opencode.json` içinde `NIHAI_UYGULAMA_PROMPTU.md` artık yoktur. Tarihsel prompt referansı hâlâ bulunan `.ai` metadata'sını ve diğer gerçek tüketicileri düzelt; zaten yapılmış değişikliği yeniden yapılacak iş gibi sunma. [R02,R09,R11]

Bu inceleme bir PATH wrapper'ının kullanıcı bilgisayarında kurulu olduğunu kanıtlamaz. Kanıtlı sorunlar config, talimat, global dağıtım ve runtime bağımlılıklarıdır. Alias/shim varlığını yerelde ayrı envanterle belirle.

## 4. Hedef mimari ve davranış sözleşmesi

### 4.1 Sorumluluk sınırları

| Alan | Sahibi |
|---|---|
| İnteraktif oturum, model seçimi, native araçlar, planning, MCP, alt ajan çalıştırma | Kullanıcının açtığı CLI ve kendi güvenlik politikası |
| Proje amacı, yerel geliştirme bilgisi, görev kapsamı ve seçili skill referansları | İnce Zekam çalışma alanı bağlamı |
| Zekam veri/işlemleri, ölçüm, yetkili Work geçişi, source binding, claim/receipt ve recovery | Mevcut Zekam servisleri ve deterministik kontroller |
| Sonucun doğruluğu ve tamamlanma kanıtı | Gerçek kaynak/test sonuçları ve riskin gerektirdiği bağımsız doğrulama |

Zekam yeni bir TUI, browser/dashboard, zorunlu daemon, genel model gateway'i, CLI proxy'si veya ikinci ajan framework'ü olmayacak. `zekam` kendi yardımcı CLI'ı olarak kalabilir; vendor executable'larını gölgeleyemez.

**Native-first, “bütün native araç çağrıları Zekam tarafından teknik olarak denetleniyor” anlamına gelmez.** Araya girmeyen Zekam, bağımsız CLI'ın her filesystem/shell etkisini engelleyemez. Native sandbox/approval host'un, Zekam servislerinin admission kontrolleri Zekam'ın sorumluluğudur. Markdown bir güvenlik duvarı değildir. [E05]

Zekam kontrollü bir Work için gerekli ön yetki ve claim gerçekten kurulmuşsa ilgili native çalışmanın kapsamını ve sonucu bağlanabilir. Sadece sonradan bir rapor içeri alındı diye daha önceki bütün araç çağrılarına pre-effect yetki veya receipt uydurma. Kanıt kaydı, gözlemlenen source/test sonucunu ve gözlemlenemeyen yürütme bölümünü ayırsın. Yetkisiz veya eksik kanıtlı Work doğrulanmış olarak kapanmasın.

### 4.2 “Kendi klasöründe çalışmak” için kesin kapsam

Üç kökü birbirine karıştırma: Zekam'ın gerçek çalışma alanı/kaynak kökü, kalıcı veri kökü `ZEKAM_HOME`, üzerinde iş yapılan hedef projenin kaynak kökü. Bunlar farklı amaçlardır. Mevcut manifest, project identity ve exact binding yapılarını kullan; yalnız bu iş için ikinci registry veya `.zekam/state` authority'si oluşturma.

| Senaryo | Beklenen davranış |
|---|---|
| Güvenilen Zekam kökünde yeni native CLI oturumu | İnce proje girişi keşfedilir. Klasör açılması tek başına görev, provider çağrısı, migration veya tarama başlatmaz. |
| Aynı Zekam projesinin alt dizininde yeni oturum | Doğru köke ulaşılır, aynı giriş tekrar tekrar yüklenmez. |
| Başka proje, home, üst/yan dizin veya benzer isimli `zekam-old` | Zekam otomatik araç, spool, job, hook veya config mutation'ı üretmez. |
| Registry'de kayıtlı GPU/SKY/Akış gibi ayrı proje | Registry kaydı o projeye otomatik Zekam instruction/skill dağıtma izni değildir. |
| Zekam oturumundan yetkili başka proje üzerinde çalışma | Hedef exact root ve host'un izinleriyle çözülür. Hedefe Zekam bootstrap kopyalanmaz, proje klonlanmaz. |
| Kullanıcı başka dizinden açıkça `zekam` komutu çalıştırır | Yalnız istenen yardımcı işlem kendi yetkileriyle çalışır; bu genel otomatik etkinleşme sayılmaz. |
| Zekam runtime/DB henüz hazır değil | Native CLI ve salt kaynak inceleme çalışabilir. Zekam işlemleri eksikliği dürüstçe bildirir; sahte state veya başarı üretmez. |

Kök eşleşmesini basename veya string prefix ile yapma. Yol normalizasyonu, filesystem kimliği, proje sınırı, nested repository, symbolic link/junction/reparse ve belirsiz binding durumlarını mevcut güvenlik yaklaşımıyla ele al. Belirsiz otomatik kapsam pasif kalır. Zekam-owned etkili işlemde belirsizlik fail-closed'dur.

Bir shell alt sürecinin `cwd`'sini tüm host oturumunun başlangıç kimliği sanma. Güvenilir session-origin bilgisi yoksa kapsamı bildiğini iddia etme. Proje içi talimatın okunması ya da bir arama sonucunda Zekam'ın bulunması otomatik operasyon yetkisi üretmez. Gemini gibi JIT/ancestor discovery kullanan bir host dosyayı başka bir bağlamda da görebilir; vaat edilen sınır “dosya hiçbir zaman görülmez” değil, **Zekam'ın kapsam dışına kendiliğinden kurulup çalışmamasıdır**. [E02,E07]

Eski bir oturumda yüklenmiş bağlamı klasör değiştirerek silemezsin. Geçiş kabulü temiz yeni oturumda yapılır; native history veya session dosyaları otomatik silinmez. Önceden ayrı yetkilendirilmiş scheduler/evolution işleri ise CLI açılışından ayrıdır; bu görev onları topluca kapatmaz, global dağıtımı yeniden üretmelerini engeller.

### 4.3 İnce ve gerçekten native giriş

`AGENTS.md`, yalnız sürekli gerekli amaç, önemli yerel farklılıklar, yetki/kanıt ayrımı ve görev halinde açılacak kısa yönlendirmeyi içersin. `00_BASLA.md` ve `DEVAM_PROTOKOLU.md` de uyumlulaştırılsın: küçük girişten bütün tarihsel talimatlara koşulsuz zincir kurmak sadeleştirme değildir.

Giriş; selamlama, genel soru ve basit kaynak incelemesinde doctor/RAG/router/subagent töreni istemesin. Kullanıcının açıkça seçtiği görev için ilgili kabul ve yetki bağlamı yüklenir; klasörü açan her kullanıcıya eski aktif görev kendiliğinden uygulanmaz. `AKTIF_GOREV.md` kapsam authority'si, YAML salt okunur projection, operational store gerçek çalışma durumudur. Bunlar birbirinin yerine geçmez.

Dört istemci için bu teslimin yerel giriş yaklaşımı:

- **Codex/OpenCode:** ortak `AGENTS.md`. OpenCode config aynı başlangıcı ayrıca yeniden yüklemez; `@file` metninin OpenCode'da otomatik çalışacağı varsayılmaz. [E02,E09]
- **Claude Code:** ortak içeriğe giden ince proje-yerel `CLAUDE.md` compatibility import'u kullanılabilir; `@AGENTS.md` resmî uyumluluk yoludur. Mevcut kullanıcı dosyası varsa üzerine yazma. Yeni doğrudan AGENTS desteğini gerçek sürüm ve discovery koşullarıyla raporla, sürüm yükseltmeyi zorunlu kurulum yapma. [E05]
- **Gemini CLI:** ince proje-yerel `GEMINI.md`, ortak girişe native import veya aynı kanonik kaynaktan üretilen eşdeğer kısa içerik. Kullanıcının `context.fileName` ayarlarını global değiştirme; mevcut AGENTS+GEMINI keşfi varsa çift yükü ayrıca sınama. [E07]

Gerekli hedef dosyalar mevcut generator/sahiplik düzeninden türesin. Bağımsız dört sistem prompt'u oluşturma. Kullanıcı-geneli `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` veya vendor config'ine yeni yönlendirme ekleme. Managed-only/override gibi kullanıcının seçtiği host politikası Zekam bağlamını kapatıyorsa bunu teşhis et; politikayı sessizce aşma.

### 4.4 Skill ve agent düzeni

Skill metni işin ne olduğunu, ne zaman kullanılacağını, gerekli girdileri, sonucun nasıl doğrulanacağını ve gerçekten zorunlu sınırları anlatsın. Modelin zaten bildiği genel kod yazma dersleri, her görevde aynı sıra/ajan sayısı, marka bazlı düşünme reçeteleri ve geçmiş hataların bütün detayları sürekli yüke dönüşmesin. Kırılgan migration, exact komut formatı, finansal/hesaplama kuralı ve güvenlik prosedürü gerektiği referansta korunur.

Mevcut skill paket/revision/activation yapısını kullan. Skill açıklaması “her görevde kullan” türü geniş bir tetikleyici olmasın. İlgili olmayan isteklerde etkinleşmeme de test edilsin. Gövde yalnız native aktivasyonda, referans yalnız ihtiyaçta açılır. Tarihsel hata öğrenimi önce regression testi veya yerel açıklama adayıdır; otomatik global prompt maddesi değildir.

Projede `.agents/skills` ortak yüzey, gerektiğinde `.claude/skills` ince managed projection için başlangıç adayıdır; `.gemini/skills` veya `.opencode/skills` kopyasını yalnız gerçek ihtiyaç varsa üret. OpenCode'un birden fazla dizini taradığı gerçeği nedeniyle **eşit metin = tek keşif** varsayımı yapma. Gerçek sürüm aynı kanonik skill'i tek etkin girişe indirmiyorsa mevcut generator içinde fiziksel projection adlarını ve yalnız Zekam-owned alias'lara uygulanan dar, proje-yerel native filtreyi düzenle. Kullanıcının bütün Claude/Agent skill keşfini kapatan genel ayar kullanma. Seçilen düzenin dört istemcideki discovery sonucunu kaydet. [E03,E06,E08,E10]

Kanonik skill kimliği ile gerekirse farklılaşan fiziksel projection adını açıkça ilişkilendir; kullanıcı skill'iyle ad çakışmasında conflict üret. Symlink ile hızlı çözüm adına mevcut reparse/sahiplik sınırlarını gevşetme. Üretilmiş kopyalar elle bağımsız geliştirilmesin. Yeni dördüncü istemci eski üç-target receipt'lerin yorumunu değiştirmesin. [R12]

Her agentic işte en az bir child kuralı, zorunlu koordinatör ve ana ajanın kaynak okumaması kuralı yeni interactive modun varsayılanı olmayacak. Native ana ajan kapsam içi inceleme ve değişikliği yapabilir. Alt ajanlar ihtiyaç ve gerçek host yeteneğine göre kullanılır; sırf sayaç doldurmak için ajan açılmaz. Riskli/yıkıcı değişiklikte gerekli bağımsız review korunur. Aynı modelin başka başlıkla yazdığı paragrafı bağımsız verifier sayma. Bu ilkeyle çelişen manifest/domain doğrulayıcılarını ve testleri açık kapsamlı politika değişikliğiyle güncelle; testleri yalnız susturmak için kaldırma.

### 4.5 Süreklilik ve yardımcı araçlar

`resume`, `capabilities`, proje çözümleme/bilgi/citation, knowledge, backup ve mevcut Work servisleri kullanılabilir kalsın. Aktif görev, tamamlanan iş, bekleyen iş ve next-safe-action yeni bir CLI/model tarafından kanonik kayıt ve kaynak revision'ından anlaşılabilsin. Bütün transcript'in kopyalanmasına veya OpenCode lifecycle plugin'inin yüklü olmasına bağlı kalmasın.

`continuity.py`, mevcut yerel continuity servisleri ve admission yolunu incele. Yerel start/checkpoint/close işlemlerinin native oturumdan erişilen gerçek bir ortak yolu varsa onu kullan; eksikse yalnız gerekli küçük CLI/JSON girişini mevcut servis üzerine ekle. İkinci session store veya tam transcript adapter framework'ü kurma. Yeni komut gerekiyorsa uygulama, `--help`, test ve runbook birlikte teslim edilir; varmış gibi komut çağrısı yazılmaz. [R14,R15]

Checkpoint, en az çalışma/proje/görev bağı, kaynak revision'ı, kanıt referansları, tamamlanan/bekleyen ve sonraki güvenli aksiyonu ayırsın. Varsa gerçek native session ve client sürümü metadata olur; yoksa uydurma OpenCode session kimliği üretme. Dışarıdan gelen doğal dil özetleri doğrulanmış Work geçişi değildir. Eksik test veya receipt sahte başarıya dönüşmez.

Girişte sürekli bütün projelerin RAG indekslerini tarama ve her soruya zorunlu `ask` bariyeri koyma. Soru açıkça yerel kaynak doğrulaması istiyorsa yetkili native arama/okuma kullanılabilir. Zekam RAG/citation seçildiğinde freshness, kaynak revision'ı, redaction ve kanıt kuralları korunur. Jira mapping ve kurum-özel araç bilgisi ilgili skill/referansta kalır; başka host'ta OpenCode MCP adı varmış gibi çağrı uydurulmaz.

### 4.6 Unit Test Engineering: küçük ama gerçek native yol

Mevcut unit-test emeği korunacak. İlk hedef yeni dört headless model backend'i yazmak değil, **kullanıcının zaten açtığı native ajanla test üretirken Zekam'ın ölçüm, kapsam ve kanıt servislerini kullanabilmektir**.

`unit_test_runtime.py` içindeki agent gateway ile Maven ölçüm/environment/ledger bileşenlerini ayır. Native yardımcı yolda OpenCode executable bulunması önkoşul olmasın. Mevcut `test` yüzeyine uyumlu biçimde onaylı ölçüm, kanıt okuma/kaydetme ve kontrollü tekrar ölçüm erişimini sağla. Bu yol gerçek süreç/test raporu üzerinden çalışsın; yalnız “native destekli” yazan bir adapter sınıfı olmasın. [R16,R17]

Native ajan planlar/üretir, mevcut runner ölçer; yalnız tool çıktısındaki başarı yazısına güvenilmez. Test keşfi, taze Surefire/JaCoCo raporu, hedef dosya/sayaç anlamı, kaynak ve test içerik digest'leri, exit status, timeout ve source drift doğrulanır. Eski rapor, test bulunamaması veya sadece `exit 0` hedefe ulaşıldığı anlamına gelmez. Coverage için assertionsız/tekrarlı test, exclusion manipülasyonu, private metoda reflection veya production API'yi gereksiz açma yapılmaz.

Kullanıcının verdiği coverage hedefi ve test-only sınırı korunur; ölçüm→eksik analizi→üretim→yeniden ölçüm, onaylı bütçe içinde ilerler. Production davranışı veya testability refactor ayrı onay ister. Anlamlı davranış/regression testi, salt coverage artırmadığı için değersiz sayılmaz. Bunlar önceki test görevinin gereksinimleridir; bu görev onları yeniden sıfırdan kurmaz.

Açıkça seçilen eski otomatik OpenCode batch yürütücüsü korunabilir; yetkisi, bağımlılığı ve qualification durumu açıkça görünür. Codex/Claude/Gemini oturumunda yardımcı ölçüm istendi diye sessizce OpenCode veya başka model başlatılamaz. Otomatik batch özelliği ile native interactive workflow desteği aynı yetkinlik satırında yanıltıcı biçimde birleştirilmez.

## 5. Güvenli geçiş ve yeniden globalleşmeyi engelleme

### 5.1 Tek seferlik temizlikten fazlası

Mevcut `client_integrations.py` plan/apply, sahiplik, karantina ve receipt yaklaşımını genişlet. [R05,R06,R12] Bütün gerçek üreticileri çağrı grafiğinden bul: integration sync, OpenCode install, init/setup, doctor hazırlığı, skill prepare/export, template/release üretimi ve varsa evolution/worker üzerinden dağıtım. Yalnız bugünkü dosyaları silip bir sonraki güncellemede yeniden oluşturan sistem teslim etme.

Yeni default politika dört istemciye eşit **proje-yerel giriş desteği** verir; global otomatik kurulum vermez. Destekleniyor, seçili, kurulu, keşfedildi, çalıştırılarak doğrulandı farklı alanlardır. Eski `cli.integrations` boolean'larını yeni kapsamla sürümlü ve deterministik uzlaştır. Eski config veya eksik policy yeni kurulumda OpenCode global bootstrap'ını açmasın. Gemini'nin typed identity, parser, status, generator, projection ve ilgili validasyonları birlikte tamamlanmalı. Önceden üretilen digest/receipt kayıtları yeni alan eklenerek geriye dönük değiştirilmez. [R08,R12]

### 5.2 Salt okunur envanter ve exact plan

Gerçek effective home/config köklerini ve mevcut environment yönlendirmelerini salt okunur çöz; yalnız hardcoded `~/.config` varsayımına güvenme. Zekam'ın yazdığı instruction bölümlerini, default agent/plugin referanslarını, hook girdilerini, skill projection'larını ve bilinen diğer-proje dağıtımlarını envanterle. Değerleri, credential'ları veya ham kişisel ayarları rapora dökme; kaynak yolu sınıfı, sahiplik kanıtı, digest, planlanan işlem ve conflict yeterlidir.

Dry-run config, DB, lock, spool, karantina veya dizin oluşturmasın. Plan; kapsamı, exact dosya/bölüm snapshot'ını, önce/sonra değişikliğini, bağımlılığı, backup/rollback'ı ve etkilenmeyen kullanıcı içeriğini belirlesin. Gerçek kullanıcı-geneli apply için plan gösterildikten sonra ayrı exact yetki gerekir. Bu geliştirme görevi o onay verilmeden real home temizleme yetkisi sayılmaz. Fixture home'larında geçiş testleri geliştirme kapsamında yapılır.

### 5.3 Neler kaldırılır, neler korunur?

Yalnız Zekam tarafından üretildiği içerik/sürüm/receipt zinciriyle doğrulanan global müdahaleler kaldırılır veya native discovery dışında güvenli karantinaya alınır. Mevcut marker tek başına sahiplik kanıtı sayılmaz. Bilinen legacy içerik digest'leri ve skill revision/export bağı korunur. Bilinmeyen veya kullanıcı tarafından düzenlenmiş içerik conflict'tir. [R05,R06,R12]

Kullanıcının özel AGENTS/CLAUDE/GEMINI içeriği, skill'leri, MCP ayarları, vendor executable'ı, provider/model bağlantısı, kimlik bilgileri, native memory/session geçmişi ve bütün kullanıcı projeleri korunur. Shared config'te yalnız sahipliği ispatlanmış Zekam girdilerini çıkar; kullanıcı alanlarının semantiğini, mümkün olan yerde exact byte düzenini ve satır sonlarını koru. Bilinmeyen JSON/JSONC/TOML biçimini güvenilir parse edemiyorsan yeniden yazma.

Başka registry projelerindeki Zekam kalıntıları ayrı project-root planlarına ayrılır; user-scope apply gizlice bütün projeleri değiştirmez. Root'taki bütün `.agents`/`.claude` dizinini ya da bütün `AGENTS.md` dosyasını silme. Alias, shell profile veya PATH shim tespit edilirse executable çözümünü gözlemle; sahipliği belirsiz kullanıcı fonksiyonunu silme.

### 5.4 Kesinti, eski oturum ve lifecycle

Eski hook/plugin/outbox kaydını yok sayarak terminal başarı üretme. Aktif owner/lease ve pending olay varsa ilgili scope için güvenli geçiş sırası kur. Gerekiyorsa o scope'u beklet; kullanıcı oturumunu zorla kapatma. Mevcut governed drain/recovery yolunu kullan; `client drain --uygula` gibi public olarak reddedilen bir yoldan iç worker yetkisini taklit etme. [R07]

Yeni varsayılanda lifecycle hook kurma. Açık ihtiyaçla opsiyonel proje-yerel gözlem eklentisi korunursa exact workspace guard her mkdir/spool/subprocess/DB yazısından önce çalışmalı; startup, stop, compaction veya native araç sonucunu kontrol eden katman olmamalı. Gözlem arızası host'u durdurmaz; fakat Zekam kendi eksik kanıtlı Work'ünü başarıyla kapatmaz. Gözlem eksikliği görünür ve telafi edilebilir kalır.

Plan sonrası drift varsa apply reddedilir. Her atomik kapsam için backup, readback, idempotence, kesinti sonrası devam ve rollback testi bulunur. Kullanıcı değişmiş dosyaya rollback ile üzerine yazma. Rollback global eski davranışı geri getiriyorsa bunu açıkça raporla; hem eski entegrasyon geri yüklenmiş hem native-only garanti korunmuş gibi sonuç üretme. Başarılı temizlikten sonra yeni native oturumla doğrulama gerekir; history silmek çözüm değildir.

## 6. Kaynak üzerinde uygulama haritası

Aşağıdaki liste başlangıç haritasıdır; çağrı grafiğini ve ilgili testleri yerelde izle. Kaynakta zaten yapılmış işi tekrar üretme, yalnız görülen dosyaları değiştirip diğer generator'ları unutma.

| Alan | Kapsam |
|---|---|
| `AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md`, `.ai/repository-context.json`, ilgili manifest/DoD kuralları | İnce giriş, doğru authority, task-specific yükleme, risk bazlı subagent/review politikası. |
| `opencode.json`, `.opencode/agents/`, `config/client-templates/` | Zorunlu coordinator/default izinlerden çıkış; ince yerel adapter; tarihsel metin keşif dışı. |
| `src/zekam/domain/client_integration.py` | Dört istemci, kapsam/destek/kurulum/qualification ayrımı ve geriye uyumlu politika. |
| `application/client_integrations.py`, `client_instruction_bootstrap.py`, `client_hook_bootstrap.py`, `opencode_agent_bootstrap.py` | Tüm üreticiler, sahiplik, migration, scope ve rollback. |
| `interfaces/cli/integration.py`, `opencode.py`, `client.py` ve bunları çağıran kurulum/hazırlık akışları | Yeni varsayılan, eski komutların açık deprecation/cleanup yolu; yeniden globalleşmenin engellenmesi. |
| `application/skill_packages.py`, skill yaşam döngüsü ve mevcut paket kaynakları | Tek kanonik paket, Gemini projection, çapraz discovery ve historical receipt uyumluluğu. |
| Mevcut context compiler/recipe ve `application/scaffolding_ablation.py` | Gerçek effective-context tanısı, ilgili/yinelenen yük ayrımı, paired değerlendirme. |
| `interfaces/cli/continuity.py`, `application/local_continuity_*`, mevcut resume/store bileşenleri | Native istemciden gerçek checkpoint/close/continuation yolu; ek authority yok. |
| `application/unit_test_runtime.py`, `interfaces/cli/unit_test.py`, `infrastructure/unit_test_runner/` | Ölçüm–agent gateway ayrımı, native yardımcı yol, mevcut Maven/JaCoCo kanıtı. |
| `docs/CLI_ENTEGRASYON_POLITIKASI.md`, `docs/ZEKAM_YETKINLIK_ENVANTERI.md`, README ve ilgili testler | Gerçek kullanım, destek sınırı, geçiş/runbook, deprecation ve dürüst kabul durumu. |

Zekam kimliği, kalıcı veri kökü, mevcut SQLite/CAS/knowledge kayıtları ve kullanıcı içerikleri korunur. Yeni PostgreSQL bağımlılığı veya eski PostgreSQL veri importu yoktur. Docker, Redis, zorunlu sunucu veya UI eklenmez. Bu geçişi geniş çaplı dizin taşıma/yeniden adlandırma projesine dönüştürme.

## 7. İş paketleri ve ilerleme

| Paket | Teslim | Bağımlılık |
|---|---|---|
| W01: Devralma ve baseline | Önceki görev arşivi, mevcut durum, gerçek producer/discovery haritası, hedefli baseline test sonucu. | Yok |
| W02: Kapsam ve politika | Native host sınırı, exact workspace çözümü, yeni typed policy, global kurulum üretimini kapatan çekirdek. | W01 |
| W03: Dört ince giriş | Root bootstrap ve metadata, yerel compatibility dosyaları, doğru skill projection/discovery, çelişen zorunlu rol kurallarının uzlaşması. | W02 |
| W04: Güvenli migration | Dry-run, exact apply/receipt, kullanıcı verisi koruması, legacy projection/hook/agent cleanup, crash/rollback/regrowth testleri. | W02; W03 ile koordine |
| W05: Kullanılabilir yardımcı yol | Native continuity ve test ölçümü; OpenCode zorunluluğu olmadan gerçek helper akışı, mevcut explicit batch regresyonu. | W02 |
| W06: Bağlam ve kabul | Görev-özel skill/reference dönüşümü, effective-context kontrolü, negative tests ve paired değerlendirme girişi. | W03–W05 |
| W07: Son doğrulama ve devir | İlgili CI/kalite kapıları, güncel kullanım/transition runbook'u, capability matrisi ve açık deployment/qualification durumu. | W06 |

Paketler tam bir yeni orkestrasyon reçetesi değildir. Bağımsız dosya/kaynaklarda uygun paralellik kullan; aynı yazılabilir kaynağa birden fazla kontrolsüz writer koyma. Küçük bir düzeltme için bütün rollerin törenini tekrarlama. Her pakette gerçekten biten değişikliği, kanıtı ve kalan engeli kaydet. Yalnız README veya config değiştirip runtime/migration boşluklarını sonraya bırakmak teslim değildir.

## 8. Kabul testleri

Mevcut test araçlarını ve sentetik fixture'ları genişlet. Negatif senaryolar, doğru metnin dosyada bulunmasını değil mümkün olduğunda gerçek davranışı test etsin. Aşağıdaki satırlar kabul sonuçlarıdır; her satır için test veya gözlem yolu ve sonucu kaydedilir.

| ID | Senaryo | Kabul |
|---|---|---|
| A01 | Dört vendor executable'ın normal açılışı | Zekam shim/alias/wrapper zorunluluğu yok; native executable kimliği değişmiyor. |
| A02 | Zekam kökü ve aynı projenin alt dizini | Doğru ince giriş keşfediliyor, duplicate bootstrap yok. |
| A03 | Sibling/parent/home ve ayrı kayıtlı proje | Zekam kaynaklı otomatik process, config, spool, job ve provider etkisi yok. |
| A04 | `zekam-old`, benzer prefix, nested repo, farklı filesystem kökü | Yanlış otomatik kapsam kabul edilmiyor. |
| A05 | Symlink/junction/reparse veya drift | Mevcut exact-root sınırı aşılamıyor; belirsizlik aktif yetkiye dönüşmüyor. |
| A06 | Sadece `ZEKAM_HOME` tanımlı veya Zekam kurulu | Bu durum başka projede otomatik entegrasyonu açmıyor. |
| A07 | Zekam oturumundan açıkça yetkili dış hedef | Doğru source root kullanılıyor; hedefe bootstrap veya proje kopyası üretilmiyor. |
| A08 | Runtime/DB eksikliği ve zararsız kaynak inceleme | Native CLI kilitlenmiyor; Zekam Work başarı kaydı uydurulmuyor. |
| A09 | Native model/tool/plan/MCP/subagent seçimi | Zekam default coordinator, model-bound router veya genel `allow` ile bunları zorlamıyor. |
| A10 | Stop/compaction ve opsiyonel gözlem hatası | Yeni default hook'suz; varsa local gözlem host kararını bloklamıyor, eksik kanıt görünür. |
| A11 | Eski config veya policy alanı yok | Upgrade/init/install/doctor/sync tekrar global bootstrap üretmiyor. |
| A12 | Migration dry-run | Dosya/DB/lock/spool/dizin yan etkisi yok; exact plan mevcut. |
| A13 | Known-owned global artifact apply | Yalnız onaylı Zekam bölüm/girdileri taşınıyor; kullanıcı verisi korunuyor. |
| A14 | Marker var, içerik drift/unknown | Sahiplik varsayılmıyor; güvenli conflict ve açıklama var. |
| A15 | Shared config ve özel instruction prefix/suffix | Kullanıcı semantiği ve korunan metin aynı; credential/PII rapora düşmüyor. |
| A16 | Plan sonrası drift, crash, retry ve rollback | Stale plan reddediliyor; idempotent recovery var; kullanıcı değişikliğine overwrite yok. |
| A17 | Diğer proje projection cleanup | Ayrı root planı/onayı olmadan user-scope apply oraya yazmıyor. |
| A18 | Bütün dağıtım üreticileri sonrası ikinci sync/upgrade | Temizlenen global parçalar tekrar oluşmuyor; ikinci plan no-op. |
| A19 | Gemini kimliği ve legacy receipts | Parser/status/generator uyumlu; eski digest/receipt değişmiyor. |
| A20 | Claude sürüm/fallback/import ve host override | Gerçek koşula uygun giriş; global policy override yok; destek durumu dürüst. |
| A21 | Skill'in çoklu keşif dizinleri | Aynı kanonik skill için çakışan etkin giriş yok; kullanıcı skill'i kaybolmuyor. |
| A22 | İlgisiz soru, selamlama, küçük doküman düzeltmesi | Gereksiz doctor/router/RAG/agent zinciri veya bütün aktif görev yükü yok. |
| A23 | İlgili test/Jira/recovery isteği | İhtiyaç duyulan skill/reference bulunuyor; gerekli exact prosedür kaybolmuyor. |
| A24 | Bir host'ta checkpoint, diğerinde resume | Aynı doğrulanmış iş ve source revision anlaşılabiliyor; hayalî session/review yok. |
| A25 | OpenCode executable yokken native test helper | Mevcut runner ile gerçek ölçüm ve sonuç kaydı çalışıyor; başka model sessizce açılmıyor. |
| A26 | Eski explicit OpenCode batch yolu | Açık seçildiğinde mevcut izinler ve runtime davranışı korunuyor. |
| A27 | Eksik/eski JaCoCo, test yok, yanlış hedef veya source drift | Başarı/coverage üretmiyor; nedeni ve next-safe-action doğru. |
| A28 | Geçersiz yetki/claim, target dışı yazma, eksik receipt | Zekam-owned işlem ve doğrulanmış Work kapanışı reddediliyor. |
| A29 | Native gözlem ile kanonik kanıt ayrımı | Sonradan ingest, geçmişe dönük pre-effect yetki/bağımsız verifier sayılmıyor. |
| A30 | RAG/knowledge/backup ve kullanıcı kayıtları | Mevcut kapsam içi regresyonlar geçiyor; veri veya kaynak sahipliği kaybı yok. |
| A31 | Kurulu ama test edilmemiş CLI/model | `ready` veya qualified denmiyor; installed/discovered/verified ayrı. |
| A32 | Temiz yeni oturum ve eski session | Yeni davranış kanıtlanıyor; eski bağlamın history silinerek temizlendiği iddia edilmiyor. |

Windows yol/izin/komut davranışları hedefli olarak test edilsin. Desteklenen diğer işletim sistemleri için var olan CI matrix'i korunur; yalnız bir işletim sisteminde koşmuş testi çapraz platform kanıtı sayma. `os.O_NOFOLLOW` benzeri platforma özel mevcut kod yolunu native continuity'de kullanırken gerçek platform desteğini doğrula. [R15]

### 8.1 Sadeleşmenin gerçekten işe yaradığını ölç

Mevcut `ScaffoldingAblationService` ve uyumlu evidence yapılarını kullan. [R13] Karşılaştırma: mevcut ağır akış ile yeni native/ince akış; gerekliyse üçüncü bir minimal referans. Aynı görev, kaynak snapshot'ı, model/CLI sürümü, araç erişimi, güvenlik sınırı, bütçe ve kabul ölçütü altında karşılaştır. Eski global ayarları gerçek kullanıcı home'una geri kurarak benchmark yapma; izole sentetik ortam kullan.

Kapsamı küçük ama ayırt edici tut: genel soru, küçük source düzeltmesi, çok dosyalı inceleme, devam/recovery ve gerçek unit-test ölçüm görevi. Sonuç kalitesi ve güvenlik ihlalini birincil; gereksiz tool/subagent sayısı, effective context yükü, süre ve maliyeti ikincil ölçüt yap. Exact tokenizer yoksa byte/karakter ölçüsünü token diye sunma. Ölçülemeyen latency/maliyet için sıfır yazma.

Yalnız AGENTS satır sayısını ölçme: global talimat, root/ancestor dosyası, eager import, config instruction, agent prompt'u, skill açıklamaları ve gerektiğinde yüklü gövdenin katkılarını ayır. Kullanıcı metnini dökmeden kaynak sınıfı/digest/yüklenme nedeni ve ölçüm niteliği raporlanmalı. Host introspection gerçek context'i göstermiyorsa bunun tahmin/modelleme olduğunu belirt.

Başarı eşikleri ve tekrar sayısı sonuçları gördükten sonra seçilmez. Mevcut değerlendirme politikasına uygun, bütçeli ve tekrarlanabilir plan üret; tek başarılı örnekten evrensel üstünlük çıkarma. Token azalırken doğru görevin/skill'in bulunamaması veya kritik güvenlik bilgisinin kaybı başarı değildir. Eski rol kısıtını kaldırmak kullanıcı mimari kararıdır; ölçüm yapılmadıysa kalite artışı iddiası yine kurulamaz.

Canlı CLI/model çağrısı, kurulum veya ağ gerektiren kabul yalnız mevcut açık yetki kapsamında çalıştırılır. Mevcut oturumu kullanmak, bütün hesap ve modellerle sınırsız benchmark izni değildir. İzin/kurulum yoksa provider-free testleri tamamla ve ilgili qualification satırını `not-run`/`not-installed`/`authorization-required` olarak ayır; sessiz skip ile passed üretme.

## 9. Tamamlanma ve kullanıcıya teslim

Üç sonuç ayrı raporlanacak:

1. **Kaynak implementasyonu:** uygulanan kod, test, şema/policy uyumu, CI ve regression sonucu.
2. **Yerel geçiş:** gerçek cihazdaki exact plan, onay, apply/readback/rollback sonucu. Onay yoksa geliştirme hazır olsa da deployment uygulanmış sayılmaz.
3. **CLI/model qualification:** hangi istemci/sürüm/işletim sistemi/modelde hangi senaryonun gerçekten çalıştığı; henüz koşulmayan kombinasyonlar.

Repository'de yeni politika/runbook, ince bootstrap'lar, generator/migration değişiklikleri, davranış testleri, native yardımcı yol ve güncellenmiş capability envanteri birlikte bulunmalı. Araştırma kararlarını mevcut belge düzeninde kısa bir ADR ile sakla; bu aktif görev için yeni ve bağımsız bir doküman platformu kurma. Geçici çalışma raporları gerçek kaynak köküne yığılmasın.

Kullanıcıya son raporda değişen davranışı, gerçek test sonuçlarını, korunan veriyi, yapılmamış deployment/qualification adımlarını ve gerekiyorsa sıradaki tek güvenli aksiyonu anlat. “Dört CLI tam destekli” iddiasını yalnız dört ayrı gerçek kabul kanıtıyla kur. Önceki unit-test görevinin açık qualification/activation işleri ve kapsam dışı Global DoD kayıtları açık kalır.

**Teslimin özü:** Kullanıcı native CLI'ını seçer ve doğrudan açar. Zekam yalnız kendi çalışma alanında gerekli bağlamı ve yardımcı yetenekleri sunar. Daha az zorunlu yönlendirme, korunmuş yerel bilgi, gerçek ölçüm ve kayıpsız süreklilik birlikte sağlanır.

## Ek A. Resmî araştırma kaynakları

Kontrol tarihi: 5 Ekim 2026. Aşağıdakiler teknik iddiaların kaynaklarıdır; talimat veya otomatik işlem yetkisi değildir. Hareketli belgeler uygulama sırasında yalnız ilgili sürüm farkı için yeniden kontrol edilir. Blog önerisi, format zorunluluğu ve bu görevdeki tasarım kararı aynı şey değildir.

| ID | Kaynak | Adres |
|---|---|---|
| E01 | OpenAI, Rethinking skills and prompts for GPT-6 Astra; 11 Eylül 2026 | `https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra` |
| E02 | OpenAI/Codex, AGENTS.md discovery | `https://developers.openai.com/codex/guides/agents-md` (resmî yönlendirme: `https://learn.chatgpt.com/docs/agent-configuration/agents-md`) |
| E03 | OpenAI/Codex, Agent skills | `https://developers.openai.com/codex/skills` (resmî yönlendirme: `https://learn.chatgpt.com/docs/build-skills`) |
| E04 | Anthropic, Claude Code best practices | `https://code.claude.com/docs/en/best-practices` |
| E05 | Anthropic, Project memory/instructions ve AGENTS.md | `https://code.claude.com/docs/en/memory` |
| E06 | Anthropic, Claude Code skills | `https://code.claude.com/docs/en/skills` |
| E07 | Google, Gemini CLI GEMINI.md context | `https://geminicli.com/docs/cli/gemini-md/` |
| E08 | Google, Gemini CLI skills | `https://geminicli.com/docs/cli/skills/` |
| E09 | OpenCode, Rules | `https://opencode.ai/docs/rules/` |
| E10 | OpenCode, Agent Skills | `https://opencode.ai/docs/skills/` |
| E11 | Agent Skills specification | `https://agentskills.io/specification` |

## Ek B. Pinned repository kanıtı

Repository: `mehmet-karacan/zekam`. Revision: `f37ab301510bbe0045dab265de36ea1475775f33`. Commit: `config: gecersiz reranker hedeflerini ertele`, 4 Ekim 2026 21:53:19 UTC. Aşağıdaki aralıklar araştırmada okunan kaynak kapsamını belirtir; tablo, dosyanın bütününün veya bütün çağrı grafiğinin test edildiği anlamına gelmez.

Kanonik kaynak adresi şablonu: `https://github.com/mehmet-karacan/zekam/blob/f37ab301510bbe0045dab265de36ea1475775f33/<dosya-yolu>`.

| ID | Dosya yolu | İncelenen kapsam / kanıt |
|---|---|---|
| R01 | `AGENTS.md` | Kök talimatlar; zorunlu child ve görev-özel yük. |
| R02 | `opencode.json` | Tam config; default coordinator, broad allow ve eager instructions. |
| R03 | `.opencode/agents/zekam-coordinator.md` | Koordinatör gövdesi; kaynak erişimi yasağı, router/RAG reçeteleri, child bağımlılığı. |
| R04 | `src/zekam/application/opencode_agent_bootstrap.py` | Satır 1–260; global konumlar ve lifecycle başlangıç/spool. |
| R05 | `src/zekam/application/client_instruction_bootstrap.py` | Satır 1–260; managed body, legacy digest, exact metin/sahiplik koruması. |
| R06 | `src/zekam/application/client_hook_bootstrap.py` | Satır 1–250; global hook hedefleri, reviewed sürümler ve owned-entry temizliği. |
| R07 | `src/zekam/interfaces/cli/client.py` | Satır 1–240; hook/pending/drain ve hata/admission semantiği. |
| R08 | `src/zekam/domain/client_integration.py` | Typed client enum, eski defaults, policy digest ve status ayrımı. |
| R09 | `.ai/repository-context.json` | Tam belge; tarihsel main_task, projection ve min-subagent metadata. |
| R10 | `src/zekam/application/active_task_contract.py` | Satır 1–230; görev header/authority/projection sözleşmesi. |
| R11 | `00_BASLA.md` | Başlangıç/devam yükleme ve kanıt protokolü. |
| R12 | `src/zekam/application/client_integrations.py` | Satır 1–260; projection/receipt doğrulama, üç-target sınırı ve kanıtlı sahiplik. |
| R13 | `src/zekam/application/scaffolding_ablation.py` | Paired evaluator, kapılar ve rollback bağı; otomatik silme yok. |
| R14 | `docs/ZEKAM_YETKINLIK_ENVANTERI.md` | Mevcut feature durumları; özellikle unit-test/Work/continuity açıkları. Belge beyanı, bu araştırmanın runtime kanıtı değildir. |
| R15 | `src/zekam/interfaces/cli/continuity.py` | Satır 1–210; ortak local continuity/store/admission bileşenleri. |
| R16 | `src/zekam/application/unit_test_runtime.py` | Runtime binding/composition; OpenCode zorunluluğu ve ayrılabilecek ölçüm bileşenleri. |
| R17 | `src/zekam/interfaces/cli/unit_test.py` | Satır 1–180; mevcut `test` CLI, request/binding/runner entegrasyon girişleri. |

Ek olarak pinned repository tree ve CLI dizin envanteri okundu. Yeni uygulama, yerelde bütün ilgili tüketici/test/producer bağlantılarını doğrulayarak bu haritayı tamamlamalıdır.
