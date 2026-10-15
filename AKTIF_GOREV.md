---
schema: zekam-active-task/v2
task_id: ZEKAM-NATURAL-LANGUAGE-CAPABILITY-COMPLETION-001
status: APPROVED_ACTIVE_TASK
title: Zekam Dogal Dil ve Tum Yetkinliklerin Tamamlanmasi
created_at: 2026-10-12T21:09:52+03:00
baseline_repository: mehmet-karacan/zekam
baseline_branch: main
baseline_head: b29e2c9e89a400176d722a430af4fcebc7a270cc
baseline_is_fixed_revision: true
legacy_postgresql_data_import: FORBIDDEN
postgresql_runtime_dependency: FORBIDDEN
docker_required_for_zekam_core: false
ui_surface: FORBIDDEN
push_authorized: false
runtime_test_evidence_at_task_creation: NOT_EXECUTED
---

# Zekam — Doğal Dil, Hızlı Proje Bilgisi Ve Yetkinlik Tamamlama Görevi

**Dosya:** `AKTIF_GOREV.md`  
**Belge sürümü:** `1.0` — v0.5 talep defterinden tek ana göreve geçiş  
**Tarih:** 12 Ekim 2026  
**Mevcut yürütme aşaması:** `IMPLEMENTATION_APPROVED`
**Bu dosya hazırlanırken:** Kaynak değişikliği, ürün testi, canlı embedding veya deployment yapılmadı.

> **Görev özeti:** Önce Zekam'ın gerçek durumunu ve uygun açık kaynak yöntemleri araştır;
> yedi kullanıcı talebini ve 31 yetkinliği kapsayan, uygulanabilir ve ölçülebilir onarım
> planını tamamla. Uygulama onayından sonra aynı görev kapsamında kontrollü dilimlerle
> geliştir, gerçek doğal dil senaryolarıyla doğrula ve kanıtlarıyla kapat. Yalnız rapor,
> yeni komut adları veya `ready` etiketleri üretmek, ürünü tamamlamak değildir.

## 0. Yetki, Aşama Ve Devir Sözleşmesi

### 0.1 Güncel Kullanıcı Kararı

Kullanıcı 2026-10-12 tarihli bu sohbette tüm ilgili onayları verdi ve tekrar onay
istenmemesini istedi. WP-02..12 kaynak uygulaması, ilgili schema migration,
provider/araç kullanımı ve her tamamlanan aşamanın commit+push işlemi bu kapsamda
onaylıdır. İşin exact kapsamı, veri koruma ve test/kanıt koşulları devam eder.

| Aşama | Şimdiki Yetki | Çıkış |
|---|---|---|
| Araştırma, kaynak okuma, ekli dosyaları güvenli inceleme, yerel kanıt/plan hazırlama | Onaylı | Güncel gerçek durum, karşılaştırma, tasarım kararları, modül ve test bazlı plan |
| Mevcut kapsam için izole, provider-free tanı/fixture testleri | İncelenmiş komut, uygun güvenli ortam ve araç yetkisi varsa; kullanıcı verisine yazmadan | Baseline/test kanıtı, ortam yoksa açık blocker |
| Zekam kaynak koduna, ajan ayarlarına veya schema'ya uygulama | Exact WP-02..12 planı için onaylı | Kod, test, migration ve belge teslimi |
| Canlı model/embedding kampanyası, kurumsal içerik aktarımı, yeni ücretli işlem | İlgili görev kapsamı için onaylı; veri minimizasyonu ve bütçe sınırı korunur | Gerçek kabul ve ölçülmüş kanıt |
| Git commit | Her tamamlanan aşama için onaylı | Türkçe ASCII başlık ve gövde |
| Git push veya eşdeğer uzaktaki Git referansı güncellemesi | Her tamamlanan aşamanın `origin/main` push işlemi onaylı | Commit ve remote readback |
| Gerçek global istemci ayarı ve kullanıcı verisi taşıma | İlgili görev kapsamı için onaylı; exact sahiplik ve geri alma kanıtı gerekir | Kapsamlı, doğrulanmış değişiklik |

`APPROVED_ACTIVE_TASK`, mevcut task parser'ıyla uyumlu kapsam kimliğidir; bir işletim
sistemi izni, execution grant, başarılı test, Work kapanışı veya push yetkisi değildir.
**Araştırma onayı ile uygulama onayı, ajan Shell hedef politikası ile bu hazırlığın
izinleri birbirine karıştırılmayacak.** Bir izin eksikse yalnız ilgili etkili adımı
beklet; bağımsız ve güvenli işleri durdurma.

### 0.2 Mevcut Repository Görevini Kayıpsız Devral

1. Gerçek checkout'taki `AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md`, mevcut
   `AKTIF_GOREV.md`, generated YAML ve operational Work/lease durumunu oku.
2. Bu hazırlıkta okunan eski repo görevi `ZEKAM-NATIVE-CLI-WORKSPACE-001` idi.
   Kullanıcının koyduğu yeni dosyayı eski committed görevle karıştırma. Eski task,
   projection, açık işler ve kullanıcı değişikliklerini mevcut arşiv yöntemiyle koru.
3. Yeni göreve geçiş önceki işleri tamamlanmış saymaz. Örtüşen işleri bu görevdeki
   REQ/CAP/WP kayıtlarına bağla; ikinci kez aynı işi açma veya eski işi silme.
4. Header'ın alanları mevcut `ActiveTaskContract` sözleşmesine göre seçildi. Sürüm,
   aşama ve yetki açıklamaları gövdededir; parser'ın bilmediği alanları frontmatter'a ekleme.
   Repo üzerinde kabul edildiğinde gerçek parser ve mevcut projection üreticisini kullan;
   YAML'a elle onay, digest veya ilerleme yazma.
5. `baseline_head` araştırma karşılaştırma sabitidir; kaynak ilerlediyse diff'i incele.
   Eski commit'e reset etme. Gerçek HEAD/branch, dirty durum, paket import konumu,
   Python/OS/CLI sürümleri ve scope digest'ini yürütme başlangıcında tekrar kaydet.
6. Gerçek kaynak kökü dışında kopya proje, mirror veya detached worktree oluşturma.
   Araştırma raporları, indirilen dış kaynak ve fixture'lar Zekam kaynak köküne atılmaz.
   Kaynakta yalnız ayrıca onaylı tracked değişiklikler yapılır.
7. Ek A'daki eski metin salt tarihsel kanıttır. Eski durdurma ifadeleri, örnek onaylar
   ve geçmiş durumlar bu bölümün yerine geçmez; otomatik başlangıç bağlamına yüklenmez.

## 1. Amaç Ve Kullanıcının Beklediği Deneyim

Zekam, kendi çalışma klasöründe açılan destekli coding istemcisinde, kullanıcıya komut
öğretmeden projeleri tanıyan, doğru bilgiye hızlı ulaşan, belgeleri güncelleyebilen,
araştıran, görevleri yöneten, testleri geliştiren ve kesintiden devam edebilen bir
mühendislik yardımcısı olmalıdır. Hedef tek bir model veya CLI'a bağımlı yeni bir ürün
kurmak değil; mevcut Zekam'ın değerli yeteneklerini **gerçekten çalışan bütünleşik
akışlar** hâline getirmektir.

Kullanıcı örnekleri: “GPU'da bunu açıkla”, “Bu GTD'yi GPU bilgisine ekle”, “SKY'nin şu
tercihini hatırla”, “Bu defectte ne değişmiş?”, “Şu sınıfın testlerini iyileştir”,
“Kaldığın yerden devam et”, “Yaptıklarını commit et ama push yapma.”

CLI/MCP/API teknik araç olarak kalabilir; `zekam ask` veya `project index` çalıştırmak
**insanın zorunlu görevi değildir**. Ajan gerekli araçları kendisi çağırır. Sadece
prompt'ta “otomatik yap” yazılması doğal dil entegrasyonunun çalıştığını kanıtlamaz.

### 1.1 Üç Ayrı Kabul Ekseni

- **Davranış:** Kullanıcının doğal dil isteği doğru proje, işlem ve sonuçla karşılanıyor mu?
- **Doğruluk:** Sonuç gerçek kaynak, taze test/ölçüm ve geçerli kanıtla doğrulanıyor mu?
- **Süreklilik:** Kesinti, model/istemci değişimi ve hata sonrası kayıtlar korunarak devam ediyor mu?

Bir eksenin başarısı diğerlerini otomatik geçirmez. `declared-ready`, kodda bulunma,
komuta bağlanma, fixture testi, canlı çalışma ve kullanıcı cihazı kabulü ayrı alanlardır.

## 2. Girdiler Ve Kaynakların Güven Sınırı

### 2.1 Mevcut Girdiler

Aşağıdaki hash'ler bu görev dosyasının hazırlığında mevcut gerçek byte'lardan hesaplandı.
Dosya adı aynı fakat içerik farklıysa yeni girdiyi ayrı revision olarak kaydet.
- **AKTIF_GOREV.md (önceki v0.5)** — 42,954 byte; SHA-256 `cde119a8b4ee1d9d8ece297e0981588b48802a3cf0b6515db6ae4b4eb05b7967`.
- **ZEKAM_MIMARI_PAKETI_v2.zip** — 1,559,448 byte; SHA-256 `d84446d48fd41adac2072314b47d5cf1751a854efa563e4b9ebdeca953d3bb4e`.
- **ZEKAM_MIMARI_REFERANSI.md** — 465,497 byte; SHA-256 `a44205c086003c9b553bb7fc79bc1c910cd9e1981dde6caf65429cb66d04beb1`.
- **gpu-db-ddl-sync-master@87c867aa5ba.zip** — 40,314,652 byte; SHA-256 `35f098fa6025e0d5328e62d5a768a93bf99c7f49970821eb869e19d6f245e704`.

- Mimari pakette `kataloglar/yetkinlikler.json`, `aciklar_ve_kararlar.json`,
  `arayuz_sozlesmeleri.json`, `kabul_senaryolari.json`, `kaynak_haritasi.json`,
  `yeniden_insa_kapsami.json` ve üretim/güncelleme bölümleri öncelikli referanstır.
- v2 metadata'sındaki `full_repository_content_audit=false`,
  `runtime_tests_executed=false`, `full_reconstruction_proven=false` korunur.
  Paketin yapısal doğrulama sayıları Zekam ürün test sonucu değildir.
- GPU/SKY gerçek proje kökleri, ODD/GTD örnekleri, gerçek Jira/Confluence kayıtları ve
  ODI exportları yalnız gerçekten erişilen/gösterilen girdilerden çözülür. Bu dosyada
  bunların erişilebilirliği veya belirli bir örnek kaydın varlığı varsayılmıyor.
- Graft karşılaştırma adayı: `https://github.com/trailhq/graft`. Bu dosyanın hazırlanması,
  Graft'ın daha iyi veya Zekam'la uyumlu olduğunu kanıtlamaz.
- Ekli Jenkins ZIP'i salt-okunur referanstır. İçindeki script, pipeline, DDL, build,
  hook veya bağlantı kodu kendiliğinden çalıştırılmaz; hazır embedding çözümü içerdiği
  varsayılmaz. Uygulanabilir örüntü yoksa bu da araştırma sonucudur.

### 2.2 Bu Hazırlıkta Canlı Okunan Dar Repo Kesiti

`main` ref'i, `AGENTS.md`, eski aktif görevin ilk bölümü ve `active_task_contract.py`
parser kesiti okundu. Okunan ref, mimari paketin sabit commit'iyle aynıydı. Bu,
repo'nun tamamının yeniden denetlendiği anlamına gelmez. Bölüm 15 kaynak bağlarını verir.
Geniş araştırma ve uygulama aşağıdaki iş paketlerinin **yapılacak işidir**.

## 3. Kapsam Ve Değişmezler

### 3.1 Kapsam

Yedi kullanıcı talebi eksiksiz korunur. Tüm 31 CAP kaydı taranır; `partial`,
`documented-partial`, `scaffold` ve gerçek akışı eksik teknik yetenekler tamamlanacak
planlara bağlanır. `ready` beyanlı özellikler doğal dil, entegrasyon ve regresyon
bakımından tekrar sınanır. Tarihsel özellikte değerli davranış korunur; eski dosyalar
veya PostgreSQL dönemi motoru sırf var diye yeniden etkinleştirilmez.

### 3.2 Kapsam Dışı

- Yeni Zekam UI/dashboard/web ürünü, zorunlu bulut servisi veya ayrı operational DB motoru.
- Kullanıcının diğer repository'lerini bu görev bahanesiyle yeniden geliştirmek.
  GPU/SKY kaynakları araştırma/fixture/kabul girdisi olabilir; source mutation ayrıca kapsam ister.
- Bağımsız `ai-test-engineering` ürününü bu göreve taşımak veya deposunu değiştirmek.
  Zekam ile yetki kontrollü entegrasyon gerekiyorsa ayrı karar kaydı; ikinci ürün kopyası yok.
- Harici eklentilerin hepsini kalıcı runtime bağımlılığı yapmak.
- Eski veriyi temizlemek, kullanıcı global ayarlarını değiştirmek veya uzaktaki içeriği
  yayımlamak için araştırma izninden yetki türetmek.

### 3.3 Zorunlu Koruma Kuralları

- Çalışan kullanıcı kaynakları, belgeler, görev/geçmiş kayıtları ve dosya sahipliği korunur.
- Core local-first kalır; SQLite operational authority ve mevcut CAS/derived index/
  analytics ayrımı korunur. PostgreSQL/Docker core önkoşulu yapılmaz; legacy DB importu yoktur.
- “Kendi klasöründe çalışsın” öncelikle **etkinleşme sınırıdır**. Mevcut `ZEKAM_HOME`'u
  kaynak köküne taşımak ayrıca fiziksel yerleşim ve yedek/rollback kararı gerektirir.
  Bu ayrımı görünür kıl; kullanıcı verisini sessizce taşıma ve kullanıcı beklentisini unutma.
- Windows gerçek kullanıcı kabulünün öncelikli platformudur. Mevcut diğer platformların
  kodu/taahhüdü sessizce silinmez; doğrulanmayan platform için başarı iddia edilmez.
- Model adı, sağlayıcı öneki ve erişim kimliği tahmin edilmez. Konfigürasyon esas alınır;
  tek modele bağlayan sabit tercih veya eski tarihli katalog canlı gerçeklik sayılmaz.
- Kimlik/digest ve immutable migration sözleşmeleri korunur. Gerekli additive değişiklik
  kaynak/projection/consumer/test ile birlikte ele alınır; eski migration byte'ları değiştirilmez.
- Bilgi/RAG/model çıktısı onay veya Work state değildir. `unknown` etki sessiz retry
  edilmez; kesinti receipt/readback ile uzlaştırılır. Harici sistemlerde mutlak
  exactly-once garantisi sunulmaz.
- Kullanıcıya CLI komutu, digest, UUID, token veya klasör çözümleme işi yüklenmez.
  Bilinen alanlar mevcut kayıttan çözülür; yalnız gerçekten eksik karar sorulur.
- Eksik özellik “opsiyonel”, “gelecek faz” veya “unsupported” yapılarak gizlice kapsamdan
  çıkarılmaz. Teknik engel ve erteleme etkisi raporlanır; kapsam değişikliği kullanıcı kararıdır.

## 4. Korunan Kullanıcı Gereksinimleri

Ek A'da v0.5'in tam metni korunmuştur. Aşağıdaki kayıtlar onun güncel uygulanabilir
özetidir. Yedi gereksinimin durumu `research-approved`; kaynak uygulama durumu
`implementation-approval-pending` olarak ayrı izlenir.

### ZKM-REQ-001 — Zekam Klasöründe İstemci Bağımsız Başlangıç

OpenCode, Codex, Claude Code, Gemini ve destek kanıtı bulunan diğer istemciler Zekam
kökünde açıldığında ince proje girişini, yetenek keşfini, aktif iş ve son güvenilir
checkpoint'i tanısın. Grok adı tek başına test edilmiş bir coding CLI değildir;
client/model ayrımını koru. Repo kökü, alt dizin, farklı dizin ve global home test edilsin.
Aynı klasör adını taşıyan ilgisiz proje, kopya veya path alias'ı güvenlik sınırı sayılmasın.
Geçerli mevcut iş/onay varsa devam edilebilir; klasör açılması yeni işlem onayı üretmez.
Eski native-first yaklaşımın iyi tarafını koru; gerektiğinde değişen politikayı açık ADR ile uzlaştır.

### ZKM-REQ-002 — Kaynak Odaklı Hızlı Ve Artımlı Embedding

Önce tarama, parse, chunk, provider qualification, embedding, yazım ve sorgu maliyetini
ayrı ölç. Her dosyanın envanter/metadata kaydı ile embedding seçimini ayır. `.java`,
`.js`, `.jsx`, `.ts`, `.tsx`, `.sql`, `.pks`, `.pkb`, `.py` gibi kaynak türleri ve güvenli
seçilmiş belgeler teknoloji politikasına göre işlensin. Uzantısı kaynak gibi görünen
üretilmiş/minify/bundle/vendor dosyası otomatik uygun sayılmasın. Değişmeyen içerik
ve chunk'lar yeniden embed edilmesin; delete/rename, model/profil değişimi, resume ve
generation atomikliği çözülsün. Kaynak kod profili, açıkça verilmiş GTD/ODD veya ODI XML'i
engelleyen tek bir küresel uzantı filtresine dönüşmesin.

`.env`/`.properties` yalnız güvenli local-only yapı/anahtar analizi için değerlendirilsin.
Shell ile `source`, import veya expression evaluation yapılmasın; bağlantı bilgilerinden
kendiliğinden DB bağlantısı veya ağ çağrısı çıkarılmasın. Şifre/token/private URL veya
hassas anahtar adı model, log, embedding, rapor, cache ve dış araştırma sorgusuna girmesin.
Dosya envanteri secret değerinin kalıcı hash'ini veya içeriğini ifşa etmeyi gerektirmez.

### ZKM-REQ-003 — Önce Proje, Sonra Yalnız İlgili RAG

Registry/alias/family, açık proje, Jira anahtarı ve doğrulanmış son görev bağlamını çöz.
Çelişkiyi tahminle bastırma. Zekam checkout'u, kullanıcının iş projesi diye otomatik
seçilmesin. GPU/SKY çözümlemesinde tüm projelerin chunk içeriği taranmasın. Açık çoklu
proje sorusu ayrı scoped fan-out olabilir. Tanımsız proje, eski indeks, kanıt yokluğu,
provider erişimsizliği ve gerçekten belirsiz istek ayrı sonuçlardır. Tek proje sorgusunda
başka projeye sessiz fallback veya kullanıcı yetkisi dışı retrieval yapılmasın.

### ZKM-REQ-004 — Shell Allow, Push Onayı Ve Türkçe ASCII Commit

Destekli ajan/alt ajan tanımlarında hedef shell/terminal politikası **`allow`** olacak;
onaylı proje işinde her zararsız komutta yeniden izin istenmeyecek. Native izin anahtarları
her hostun gerçek şemasından üretilsin; bütün istemcilere aynı JSON kopyalanmasın.
Commit, yapılan işin ne/neden/nasıl değiştiğini, testleri ve kalan riski anlatan **Türkçe
anlamlı, sadece ASCII** başlık ve gövde içersin. Push ayrı açık onay istesin.

**Uygulanabilirlik kapısı:** Sınırsız shell ve doğrudan credential erişimi varken yalnız
bir prompt veya `git push` metin eşlemesiyle bütün uzak yazmaların engellendiği iddia
edilemez. Native deny/ask istisnası, aracılı yayın aracı, credential/transport sınırı ve
eşdeğer API/alt süreç yayın yollarını araştır ve test et. Güvenilir sınır desteklenmiyorsa
“push kesin kontrollü” denmesin; dar mimari karar gereksinimi raporlansın. Bu teknik
sınır, kullanıcının `allow` talebini sessizce `deny` yapma gerekçesi değildir.

Bağımsız verifier'ın kimliği/evidence bağımsızlığı shell erişimiyle yok olmaz; yaptığı
öz değişikliği kendi kendine doğrulaması bağımsız inceleme sayılmaz. Global izin
gevşetme, sırları dışarı gönderme ve güvenlik kontrollerini kaldırma bu tercihten çıkmaz.

### ZKM-REQ-005 — Dosya/Klasör/Belgeyi Doğal Dille Proje Bilgisine Al

“Bu GTD'yi GPU için güncelle” denince varsayılan hedef proje bilgi kaynağı ve indeksidir;
belgenin orijinalini, Jira kaydını veya üretim kodunu değiştirmek değildir. Dosya/klasör/
arşiv, ODD/GTD, talep/defect ve ODI 11g export XML/ZIP ayrı provenance ve parser taşır.
ODD/GTD açılımları/şablonları tahmin edilmez; gerçek format üzerinden analiz edilir.
Başlık, tablo, sayfa veya satır/simge locator'ı, kaynak revision ve içerik kimliği korunur.
Desteklenmeyen/şifreli/okunamayan içerik metadata olarak görünür; “indekslendi” denmez.
Yeni generation tamamlanmadan eski çalışan indeks bırakılır; rename ve sürüm geçmişi
kaybolmaz. Projeler arasında kaynak, cache, permission veya rapor sızıntısı olmaz.

### ZKM-REQ-006 — Graft Ve Uygun Açık Kaynak Alternatifleri

Graft'ın kaynak, lisans, güncel commit, algoritma, test ve gerçek davranışını incele.
Graft + en az iki işlevsel alternatifin aynı iş yükünde neyi çözdüğünü karşılaştır;
uygun alternatif bulunamıyorsa arama kanıtını ve kapsam boşluğunu yaz, isim uydurma.
Graph çözümünü sırf adı dolayısıyla embedding motoru sayma. HF model/dataset araştırması,
GitBook doküman yaklaşımı, Context7 sürüm belgeleri ve gerektiğinde Exa/Firecrawl ile
kanıtı tamamla. En iyi kararı `uyarla / bileşen kullan / referansla yetin / reddet` diye
kaydet. Star sayısı, demo veya farklı donanımdaki pazarlama ölçümü üstünlük kanıtı değildir.

### ZKM-REQ-007 — Kullanıcıya Komut Ezberletmeyen Doğal Dil Akışı

İstemci isteği anlayabilir; proje/işlem/izin doğrulaması deterministik katmanda yapılır.
`intent → project/scope → capability → plan → effect admission → execution → evidence
→ açıklama/checkpoint` bağlantısı gerçek araç çağrılarıyla çalışsın. Büyük monolitik
ikinci bir orchestrator zorunlu tutulmasın. Host desteği yoksa dürüstçe bildirilsin.
Kullanıcıya normal durumda sonucu anlat; hata varsa neden ve gerekli kullanıcı kararını
söyle. Retrieval kanıtını üretilmiş açıklama diye, öneriyi uygulanmış değişiklik diye sunma.

### ZKM-REQ-008 — Kısmi Yetkinliklerin Tamamlanması Ve Tüm Kataloğun Kabulü

Kullanıcının “kısmi olanları da tamamlayalım” isteği bu yeni kapsam kaydıdır.
CAP-01..CAP-31'in her biri mevcut beyan, runtime erişimi, doğal dil akışı, veri sözleşmesi,
güvenlik, test ve canlı kabul açısından incelensin. Her eksik için bir WP, somut
uygulama adımı, acceptance ve blocker kaydı olsun. Kısmi özellikleri sadece etiket
ve doküman değiştirerek bitirme. Tarihsel mekanizmanın yerine davranışsal eşdeğer
mevcut yol varsa kanıtla; ürünü sırf geçmişte vardı diye eski mimariye döndürme.

## 5. Araştırma Yöntemi Ve Eklenti Kullanımı

### 5.1 Ortak Yöntem

1. Önce pinned Zekam kaynak gerçeği ve mevcut testlerden başlangıç davranışını çıkar.
2. Açık kaynak adaylarını sınırlı ve bağımsız araştırma kollarında bul; teknik iddiaları
   resmi belge/kaynak/testte doğrula. Her dosya/URL için revision ve okunan kapsamı kaydet.
3. Aynı corpus, makine ve ölçüm yönteminde karşılaştır. Lisans, kurulum, model/host
   bağımsızlığı, veri ifşası, Windows desteği ve rollback uygunluğu birlikte değerlendir.
4. Seçilen yaklaşımı mevcut modüllere uyarlayan ADR ve exact uygulama planı üret.
   Dış projenin tümünü vendörleme veya mevcut sistemi kanıtsız yeniden yazma.
5. Araştırma kapısı tamamlanınca aynı araştırmayı tekrar tekrar yapmak yerine eksik
   kararları ve uygulama onayı kapsamını tek pakette sun.

| Araç/Eklenti | Gerçek Kullanım Amacı | Zorunlu Sınır |
|---|---|---|
| GitHub | Zekam kaynak/test/geçmiş, Graft ve alternatiflerin kod kanıtı | Repo kimliği ve ref doğrula; özel dosyayı public arama motoruna taşıma |
| Exa | Kod indeksleme, araştırma, bellek/ajan yaklaşımı için aday keşfi | Sonuç keşiftir, doğrulama değildir; yalnız gerekli public sorgular |
| Firecrawl | Seçilmiş public teknik sayfanın yapılandırılmış okunması | Kredi/bağlantı eksikliğini kaydet; başarılı scrape uydurma, bütçesiz crawl yok |
| Context7 | Seçilen kütüphanenin gerçek sürüm/API sözleşmesi | Önce library ID çöz; örnek snippet performans kanıtı değildir |
| Hugging Face | Embedding/reranker model kartı, lisans, revision, code/çok dil veri seti | Modeli indirmek/çalıştırmak ayrı kaynak ve bütçe kararı; yetenek adı tahmin etme |
| GitBook | Erişilebilir referans içerik ve yerel bölümlü dokümantasyon tasarımı | Metadata rehberi Zekam'a entegrasyon kanıtı değil; remote write/publish ayrı |
| Miro | Gerekiyorsa teknik çizim yapısı ve yerel export modeli | Mevcut yerel Mermaid diyagramları korunur; pano yazma ayrı onay |

Araçların kurulu/çağrılabilir/bağlı/yetkili/doğrulanmış olması ayrı kaydedilir. ChatGPT'deki
bir eklenti, Zekam/OpenCode/Codex runtime'ında otomatik bulunmaz. Kullanılacak ortamda
şema/discovery ile gerçek fonksiyonlar öğrenilir; geçmişteki araç adları körlemesine
çağrılmaz. Kredi veya bağlantı eksikliği tek başına tüm işi durdurmaz; uygun mevcut okuma
yolu kullanılır ve provenance'da hangi aracın sonuç verdiği belirtilir.

### 5.2 Araştırma Kayıt Sözleşmesi

Her bulgu: `finding_id, requirement_ids, capability_ids, source_kind, repository/url,
commit/version, path/section/line-range, retrieved_at, tool, content/slice_digest,
observation, inference, evidence_level, limitations, decision_ref`.
Her araç kaydı: `tool_name, operation, purpose, input_classification, outcome,
source_refs, observed_usage, auth_or_quota_blocker`. Secret/payload kopyası yok.
Aynı kaynağı farklı araçla okumak bağımsız doğrulama veya iki ayrı kanıt sayılmaz.

## 6. 31 Yetkinliğin Kapanış Matrisi

Aşağıdaki **eski beyanlar**, doğrudan v2 `yetkinlikler.json` girdisinden korunmuştur.
Bu yeni görevde henüz hiçbiri yeniden çalıştırılarak kabul edilmedi. Her kayıt için
sonraki durum, test run/evidence ve ilgili model×client×OS kapsamıyla güncellenir.

| CAP | Yetkinlik | V2 Beyanı | Ana İş Paketi | Tamamlanma Ölçütü |
|---|---|---|---|---|
| CAP-01 | Native CLI Girişi | `partial` | WP-02 | Yeni oturumda Zekam bağlamı; dış kökte yan etki yok; gerçek client kabulü |
| CAP-02 | Native Test Ve Süreklilik Yardımcıları | `partial` | WP-07 / WP-09 | Başka client ile aynı işi ve gerçek ölçüm kanıtını devralma |
| CAP-03 | Proje RAG Ve Alıntılar | `ready` | WP-05 | Doğru scope, taze citation ve doğal dil açıklaması; kalite/latency kanıtı |
| CAP-04 | İstek, Proje Ailesi Ve Jira Yönlendirmesi | `ready` | WP-03 | Alias/family/role/Jira çelişkilerinde doğru ve izole yönlendirme |
| CAP-05 | OpenCode Sürekliliği | `ready` | WP-07 | Kapanma/compaction/reopen sonrası gerçek checkpoint ve no-duplicate effect |
| CAP-06 | İş Ve Proje Kayıtları | `partial` | WP-07 | Oluştur/plan/geçiş/geçmiş/bağımlılık/kapanış uçtan uca bağlı |
| CAP-07 | Markdown Bilgi Yaşam Döngüsü | `ready` | WP-04 / WP-05 | İçeri al/güncelle/ara/arşivle/geri al; source locator ve history |
| CAP-08 | ODI 11g Ön Kontrol Ve Bağlama | `partial` | WP-06 | 11g gerçek export nesne/ilişki doğrulaması; bozuk XML ve secret redaksiyonu |
| CAP-09 | Kanıtlı Proje Araştırması | `ready` | WP-08 | Host-neutral araştırma, kaynak doğrulaması, bağımsız verifier, kayıtlı rapor |
| CAP-10 | Fikir Üretimi Ve Terfi | `scaffold` | WP-10 | Aday üret/değerlendir/kaydet/öneriden işe geçir; scope ve tekrar kontrolü |
| CAP-11 | Analitik Raporlar | `partial` | WP-11 | Gerçek çalışma/usage/error kayıtlarından okunabilir doğal dil raporu |
| CAP-12 | Model Benchmark Ve Yönlendirme Kanıtı | `partial` | WP-08 | Gerçek model qualification ile mock sonuçlarının kesin ayrımı |
| CAP-13 | Unit Test Mühendisliği | `partial` | WP-09 | Test-only üretim + Maven/JaCoCo + kalıcı resume + kalite/plateau kanıtı |
| CAP-14 | Semantik Bellek | `partial` | WP-10 | Karar/deneyim arama, doğrulama, güncellik, çelişki ve güvenli promotion |
| CAP-15 | Jira Anahtarı Çözümü | `partial` | WP-03 / WP-06 | Key çözümünden gerçek issue içeriğine bağ; hayali issue üretme yok |
| CAP-16 | Yedek Ve Kurtarma | `ready` | WP-11 | Yeni hedefe restore, logical/source/receipt bütünlüğü ve kurtarma deneyi |
| CAP-17 | Ölçülü Otonom Gelişim | `ready` | WP-10 / WP-11 | Aday→ölçüm→shadow/canary→kontrollü activation/rollback; gerçek durum |
| CAP-18 | Kişisel Skill Yaşam Döngüsü | `documented-partial` | WP-10 | Skill propose/evaluate/approve/activate/invoke/deprecate/rollback bağlı |
| CAP-19 | Yapısal Kod Grafiği | `documented-partial` | WP-06 | Sembol/reference/impact sorgusunda gerçek statik kanıt ve belirsizlik ayrımı |
| CAP-20 | Proje Keşfi Ve Teknoloji Profili | `technical` | WP-04 | Tam metadata envanteri; doğru teknoloji/modül ve seçici içerik profili |
| CAP-21 | Yetki, Onay Ve Kanıt Kapıları | `technical` | WP-02 | İzin tercihi, task scope, claim/receipt ve yayın onayı birlikte korunur |
| CAP-22 | Kuyruk, Lease, Fencing Ve Outbox | `technical` | WP-07 | Race, lease expiry, fencing, outbox, crash, replay ve recovery kabulü |
| CAP-23 | Ortak MCP Registry | `technical` | WP-02 | Tek kanonik MCP tanımı; native projection drift/rollback ve config koruma |
| CAP-24 | Jira Ve Confluence Okuma | `technical` | WP-06 | Jira/Confluence yetkili okuma, pagination/version/citation ve hata sınırları |
| CAP-25 | Model Kimliği Ve Provider Taşıması | `technical` | WP-08 | Config-driven exact model kimliği; host/provider bağımsız taşıma |
| CAP-26 | Oracle Metadata Ve PL/SQL Bağlamı | `technical` | WP-06 | Oracle metadata/PLSQL ilişkisi; varsayılan row data ve DDL çalıştırma yok |
| CAP-27 | CAS, Ham Olay Ve Analitik | `technical` | WP-11 | CAS/olay/analytics ayrımı, digest ve yeniden üretim; türetilmiş state yetkisiz |
| CAP-28 | Kurulum, Paketleme Ve Doctor | `technical` | WP-12 | Temiz paket kurulumunda tüm public yollar ve doctor gerçek gap raporu |
| CAP-29 | Kaynak Sahipliği, Sandbox Ve Git | `technical` | WP-02 / WP-12 | Kullanıcı diff/sahiplik korunumu, kapsamlı ASCII commit, push gate |
| CAP-30 | Ajan Rolleri Ve Bağımsız Doğrulama | `historical-and-retained` | WP-08 | Gerçek child identity ve bağımsız review; aynı kaynağa tek writer |
| CAP-31 | Ölçülü Döngü, Radar Ve Tournament Sözleşmeleri | `historical-or-contract` | WP-10 | Radar/tournament/döngü kanıta bağlı bütçe/plateau ve aday çıktısı |

Her CAP için kayıt alanları: `declared_status`, `implementation_status`,
`natural_language_status`, `test_status`, `live_acceptance_status`, `source_revision`,
`work_packages`, `evidence_refs`, `remaining_gaps`, `blocked_reason`. Gerekli mevcut
alanları koruyarak contract sürümü belirle. En az `not-run`, `passed`, `failed`,
`blocked`, `not-supported` ayrımı olsun. Kaynakta eski beyanların güncellenmesi,
kanıt üretildikten sonra yapılır.

## 7. Önceki 27 Maddelik Planın İş Paketlerine İzlenebilirliği

| Plan Maddesi | İçerik | Yeni Paket |
|---|---|---|
| P01 | Repository ve tarihsel kaynak analizi | WP-00 |
| P02 | Tüm yetkinliklerin gerçek durumunu çıkarma | WP-00, WP-12 |
| P03 | Graft ve açık kaynak mimari araştırması | WP-01 |
| P04 | Teknoloji, parser, embedding/model ve sürüm karşılaştırma | WP-01 |
| P05 | Zekam klasöründe otomatik etkinleşme | WP-02 |
| P06 | Doğal dil ve komutsuz kullanım | WP-03, tüm dikey dilimler |
| P07 | Doğru proje ve scope çözümü | WP-03 |
| P08 | Shell allow, commit/push politikası | WP-02 |
| P09 | Embedding darboğazının ölçümü | WP-00, WP-04, WP-05 |
| P10 | Seçici ve artımlı indeksleme | WP-04, WP-05 |
| P11 | .env/.properties local-only yapı analizi | WP-04 |
| P12 | ODD/GTD, talep/defect, ODI XML ve ZIP girdileri | WP-04, WP-06 |
| P13 | Graph + RAG + kaynak ilişkileri | WP-06 |
| P14 | Jira/Confluence gerçek okuma | WP-06 |
| P15 | Work Graph yaşam döngüsü | WP-07 |
| P16 | Kesintiden, model ve istemci değişiminden devam | WP-07 |
| P17 | Gerçek çok ajanlı mühendislik | WP-08 |
| P18 | Model benchmark, seçim ve fallback | WP-08 |
| P19 | Unit test mühendisliği | WP-09 |
| P20 | Bellek ve öğrenme | WP-10 |
| P21 | Skill yaşam döngüsü | WP-10 |
| P22 | Fikir/radar/otonom gelişim | WP-10, WP-11 |
| P23 | Rapor, sağlık, backup/recovery | WP-11, WP-12 |
| P24 | Ortak MCP yönetimi | WP-02 |
| P25 | Tek kaynaklı mimari ve yerel exportlar | WP-12 |
| P26 | Regresyon, doğal dil ve gerçek kabul | Tüm paketler, WP-12 |
| P27 | Kayıpsız geçiş ve kullanıcı içeriği koruma | WP-00, WP-02, WP-07, WP-11, WP-12 |

## 8. Araştırma Ve Uygulama İş Paketleri

**Çalıştırma kuralı:** Şimdi WP-00 ve WP-01'in araştırma/plan kısmını tamamla;
WP-02..WP-12 için exact uygulama değişikliklerini ve testleri planla. Uygulama onayı
alınınca bu dosyanın tek görev kimliği altında devam et; her paket için yeniden
ayrı görev dosyası üretme. Aşağıdaki yeni dosya/test adları **önerilen uygulama
konumlarıdır**; mevcut kaynakta var oldukları iddia edilmez. WP-00/01 sonunda
mevcut eşdeğerler varsa onları kullanarak `implementation-map.json` içinde kesinleştir.

**Ortak paket teslim döngüsü (uygulama onayından sonra):** Hedef davranışı failing
testle sabitle → hatayı doğrula → minimal kapsamlı düzeltme → hedef test + komşu
regresyon → bağımsız inceleme gereken sınırı doğrula → evidence/checkpoint → scoped
Türkçe ASCII commit. Veri migration'ında ayrıca backup/restore; harici etkide ayrıca
claim/receipt gerekir. İzole ölçüm ve test doğrulaması, kullanıcıya CLI işi devretmek değildir.

### WP-00 — Gerçek Başlangıç Durumu Ve Kapsam Envanteri

**Bağımlılık/onay:** Yok; araştırma onaylı.  
**Girdi/kapsam:** Tüm REQ/CAP/GAP kayıtları; gerçek repo ve önceki görev/projection; v2 kaynak haritası.

**İncelenecek/değişecek konumlar:** `AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md`, `pyproject.toml`, `src/zekam/application/active_task_contract.py`, `capability_inventory.py`, `interfaces/cli/main.py`, `scripts/audit_runtime_reachability.py`, `tests/`, `legacy-preserved/` ve güncel metadata.

- [x] **WP-00.01** — Repo dosyalarının tam metadata envanterini, aktif import/entrypoint graphını ve wheel dışı/tarihsel modülleri ayır. Dosya sayısı test sayısı değildir; unreachable modülü çalışan yetenek sayma.
- [x] **WP-00.02** — 31 CAP ve 23 GAP için mevcut davranışı, kaynağı, test varlığını, gerçek koşu sonucunu ve doğal dil yolunu ayrı kaydet. Önceden var olan arızaları yeni regresyonlardan ayır.
- [x] **WP-00.03** — Kullanıcı verisini değiştirmeyen küçük baseline test grubunu komutları inceleyerek uygun izole ortamda çalıştır. Model/Jira/Oracle bağlantısı veya tam provider kampanyası kendiliğinden başlamasın.
- [x] **WP-00.04** — Cold, unchanged, single-change indeksleme ve sorgu aşamalarını ölçebilecek mevcut araçları belirle; gerçek cihaz yoksa “ölçülemedi” yaz. Ölçülmeyen yavaşlık için kök neden uydurma.
- [x] **WP-00.05** — Görev/projection schema, tarihsel hash biçimi, eski agent/non-agent ayrımı, source/wheel drift ve kullanıcı veri yerleşimi için migration risk haritası çıkar.

**Üretilen sözleşme/çıktı:** `baseline.json`, `capability-assessment.json`, `source-runtime-map.json`, `gap-ledger.json`, test/env/provenance kayıtları; hiçbir `passed` kaydı kanıtsız olmaz.

**Kabul ve negatif testler:** Kontrol: 7 eski REQ + yeni REQ-008, CAP-01..31 ve GAP-01..23 kayıpsız eşlenir; dirty dosyalar korunur; eski görev kendi kendine kapanmaz.

### WP-01 — Açık Kaynak Karşılaştırması Ve Exact Uygulama Planı

**Bağımlılık/onay:** WP-00; bağımsız public arama kolları paralel olabilir.  
**Girdi/kapsam:** Baseline, kullanıcı girdileri, Graft referansı, yerel kaynak/host sınırı.

**İncelenecek/değişecek konumlar:** GitHub kaynakları; seçilmiş resmi kütüphane/model belgeleri; v2 araştırma/üretim katalogları. Mevcut modül dosyaları başlangıç haritasından seçilecek.

- [x] **WP-01.01** — Graft ve uygun alternatifleri tarama/AST/graph/chunk/cache/index/query/resume açısından kaynak/test seviyesinde karşılaştır. Mevcut Zekam referans süresiyle aynı protokolü kullan.
- [x] **WP-01.02** — Embedding model değişikliği gerekip gerekmediğini mevcut model baseline'ına karşı değerlendir. Türkçe teknik soru ve kod sembolü corpus'u, lisans, batch/token limiti, cihaz uygunluğu ve maliyet kaydı olsun.
- [x] **WP-01.03** — Native startup, host-neutral tool/agent execution, `.env` safe parsing ve shell/push politikası için resmi host sözleşmelerini doğrula. Desteği isimsiz “tüm modeller” vaadine dönüştürme.
- [x] **WP-01.04** — Her WP için exact mevcut/yeni dosya, imza/şema, migration, bağımlılık, test, ölçüm hedefi ve rollback seçimini `implementation-map.json` içinde tamamla. Placeholder ile uygulama kapısını geçme.
- [x] **WP-01.05** — Davranışsal ve performans kabul hedeflerini uygulamadan önce sabitle. Scope/host/güvenlik değişikliği kararlarını D-01..D-08'e bağla; kararsızlık olmayan rutin iç tercihler için sürekli soru sorma.
- [x] **WP-01.06** — Araştırma ve uygulanabilir planı tek seferde teslim et; açık uygulama onayını bekle. Onay gelince aynı araştırmayı baştan yapma; yalnız source/version drift farkını doğrula.

WP-01 ölçüm sınırı: Zekam'ın sentetik grafik, kaynak keşfi ve chunk planı süreleri
ölçüldü. Graft/Aider/Serena aynı korpusta çalıştırılmadı; yeni araç kurulumu
onayı yok. Tam RAG/provider süresi ve model kalitesi ölçülmüş baseline değildir.
Seçim ölçülmemiş hız üstünlüğüne dayanmıyor; ayrıntı repo dışı kanıt paketinde.

**Üretilen sözleşme/çıktı:** `research-findings.md/json`, `comparison-matrix.json`, `design-decisions.md/json`, `implementation-map.json`, `acceptance-targets.json`, bu görev dosyasının kanıta dayalı revizyonu.

**Kabul ve negatif testler:** Kontrol: Her kısmi CAP için owner WP + exact test + veri/arayüz + kapanış kanıtı; seçilmeyen adayların gerekçesi; ölçülmemiş üstünlük iddiası yok.

### WP-02 — Proje-Yerel Başlangıç, İzinler Ve Ortak MCP

**Bağımlılık/onay:** WP-01 ve uygulama onayı.  
**Girdi/kapsam:** REQ-001/004; CAP-01/21/23/29; native host capability matrisi.

**İncelenecek/değişecek konumlar:** `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `opencode.json`, `config/zekam.default.yaml`, `client_integrations`, `client_instruction_bootstrap`, `mcp_integrations`, native adapter ve izin üreticileri; eski global parçalar yalnız migration kanıtıdır.

- [ ] **WP-02.01** — Kök ve alt dizin için ince giriş üret; tekrar eden instruction/skill kopyalarını mevcut kanonik generator üzerinden tekilleştir. Zekam dışı yeni oturumda inert davranışı gerçek hostta sınayarak göster.
- [x] **WP-02.02** — İstemci metadata/instruction/hook/MCP/shell/subagent desteğini sürüm bazında ayrı kaydet. Destek yoksa manual komut ezberi yerine açık destek sınırı ve kurulabilir adapter planı ver.
- [ ] **WP-02.03** — Onaylı görevde shell allow hedefini host-native policy ile uygula. Push/yayın exception'ını model prompt'una değil uygulanabilir araç/transport sınırına bağla; başarısız guard'ı başarı sayma.
- [x] **WP-02.04** — Tek MCP kaydını hosta göre projekte et; kullanıcının credential'ını veya yabancı girdisini kopyalama/silme. Plan→sahiplik→apply→readback→rollback ve drift/conflict testleri olsun.
- [x] **WP-02.05** — Global legacy temizliği gerekiyorsa exact sahipliği kanıtlı parçalar için ayrı migration planı üret; yeni sistem tekrar global kalıntı oluşturmasın. Kullanıcı HOME/data taşıma onayı yoksa durumu koru.

**Üretilen sözleşme/çıktı:** `ClientCapabilityProfile` ve scoped startup/MCP/permission planı; mevcut record tipleri korunur. Yeni öneri `tests/e2e/test_natural_language_workspace_entry.py` ve `tests/security/test_publish_authorization_boundary.py`.

**Kabul ve negatif testler:** Testler: kök/alt/başka proje/fresh session, duplicate entry, global conflict, rollback, shell allow fiili davranışı, onaysız direct/indirect publication ve onaylı scope dışı yayın reddi.

**14 Ekim 2026 ara durum — WP-02 kismi kapali:** `integration entry` exact kayitli Zekam
kokunde ve alt dizinde kanonik `AGENTS.md`'ye baglanan ince girisi planlar ve
claim/receipt ile uygular; Zekam disi kaynakta inerttir. Conflict, drift,
idempotent tekrar ve global istemci ayarlarina yazmama fixture testleri vardir.
Istenci yetenek matrisi dosya tespiti ile native kabul kanitini ayri tutar; Claude
Code ve Gemini bu makinede bulunmadigindan gercek oturum kabulu henuz yoktur.
OpenCode v2 yerel `shell *` izni ve dogrudan `git push *` reddi gercek hostta
sinandi; dolayli shell yayini engellenmis sayilmaz. Bu nedenle WP-02.01 ve
WP-02.03 kutulari kapanmadi. Ortak MCP icin mevcut plan/apply/readback/rollback
testleri genisletildi. Global sahiplik envanterinde tasinabilir kanitli artifact
bulunmadi; uc proje skill conflict'i korunarak repo disinda salt okunur plan
kaydedildi. WP-02.02 capability matrisi, WP-02.04 ortak MCP ve WP-02.05 legacy plan kutulari
yerel kanitla kapatildi; WP-02.01 ve WP-02.03 native host/transport kosullari nedeniyle acik
kaldı. WP-02 ve bagimli nihai kabul tamamlanmis sayilmaz.

### WP-03 — Doğal Dil Ve Kesin Proje Çözümleme

**Bağımlılık/onay:** WP-02; WP-07'nin iş bağlarıyla daha sonra bütünleşir.  
**Girdi/kapsam:** REQ-003/007; CAP-04/15 ve bütün kullanıcı yüzeyleri.

**İncelenecek/değişecek konumlar:** `src/zekam/application/request_routing.py`, `jira_issue_routing.py`, `project_rag_runtime.py`; family/Jira config; mevcut skill/host tool dağıtımı. Gerekirse öneri `natural_language_dispatch.py`.

- [x] **WP-03.01** — Modelin yorumladığı niyeti allowlisted capability ve deterministic project/scope doğrulamasına çevir; intent'i execution izni sayma. Genel soru Zekam/gpu diye yanlış hedeflenmesin.
- [x] **WP-03.02** — Açık proje, Jira prefix'i, mevcut Work ve oturum referansı için öncelik/çatışma politikasını testlerle sabitle. “Bunu da GPU için ekle” çok turlu örneğinde gerçek dosya ve önceki scope bağını doğrula.
- [x] **WP-03.03** — Tek projenin sorgusunu yalnız o projeye yönlendir; açık karşılaştırmada ayrı scoped results birleştir. Kayıtsız/belirsiz proje için en az gerekli açıklamayı sor, tüm repo embedding başlatma.
- [x] **WP-03.04** — Native ajanın discovery'de doğru tool/skill'i seçmesini ve iç komutları kendisinin çalıştırmasını bağla. Kullanıcıya programatik isim, digest veya JSON yazdıran yol kabulden kalsın.
- [x] **WP-03.05** — Açıklama üretimi retrieval'dan ayrı kaydedilsin; locator'lı kaynaklar olmadan kesin teknik iddia veya sahte işlem başarısı çıkmasın. Araç kesintisi doğal dille doğru raporlansın.

**Üretilen sözleşme/çıktı:** Öneri `NaturalRequestContext → ValidatedIntent/ProjectScope → CapabilityInvocation` sözleşmesi; `tests/unit/test_natural_language_dispatch.py`, `tests/e2e/test_natural_language_project_workflow.py`.

**Kabul ve negatif testler:** Testler: Türkçe Unicode/typo/paraphrase, alias çakışması, GPU/SKY UI-backend, Jira sayı-vs-port, genel soru, çok turlu dosya, missing index, no-remote-permission; E2E native session ölçümü.

**13 Ekim 2026 ara durum — WP-03.01 tamam, WP-03 acik:**
`natural_language_dispatch.py` mevcut `route_request` kararini, guvensiz
`ModelIntentProposal` girdisini ve exact allowlist'i salt okunur planda birlestirir.
Niyet/proje celiskisi aciklama durumuna gecer; `ready` ve `grants_authority`
her zaman `false`, gercek arac cagrisı yoktur. Genel GPU sorusunda model genel
niyet bildirdiginde proje secimi yapilmaz. `test_natural_language_dispatch.py`
ve mevcut router testleri birlikte 92/92 gecti; Ruff ve hedefli mypy temiz.
Model ciktisinin native istemciden beslenmesi, cok turlu baglam, gercek scoped
retrieval ve cevap kaniti WP-03.02..05 kapsaminda aciktir. Daha genis fixture
testindeki uc onceden var olan hata, dis `gpu-fusion` kaynaginda artik bulunmayan
`AuditDDLService.java` dosyasina baglidir; o kullanici kaynagina dokunulmadi.

**13 Ekim 2026 ara durum — WP-03.02 salt okunur kapsam kaniti:**
`natural_request_scope.py` acik proje, route, Jira, Work ve oturum sinyallerinin
onceligini/celiskisini yan etkisiz bir karar olarak cozer. `natural_request_evidence.py`
mevcut operasyonel SQLite kaydindan acik Work/proje bagini ve kayitli proje kaynak
kokundeki tam dosyanin SHA-256 degerini gercekten yeniden okur; icerik dondurmez.
Kaynak disina cikis, link, gizli ayar dosyasi, ikili icerik, bayat digest, arsivli
proje ve kilitli Work okuma negatif testlerle kapatildi. Bagimsiz OpenCode
incelemesinin hardlink, SQLite kilidi ve arsivli proje bulgulari giderildi;
27 hedefli test gecti, Windows sembolik baglanti yetkisi nedeniyle bir test atlandi.
WP-03.02 henuz tamamlanmadi: native cok turlu oturumun dogrulanmis sinyali
uretmesi ve gercek son kullanici kabul akisinda bagin olculmesi aciktir.

**13 Ekim 2026 ara durum — WP-03.03 kapsamli sorgu adapteri:**
`natural_request_retrieval.py` secilmemis/belirsiz kapsamda sorgu yapmaz;
tek proje icin yalniz o kayitli projenin RAG sorgusunu acar. Coklu aile
eslesmesi yalniz acik karsilastirma ifadesiyle proje basina ayri sonuc verir.
Kayitsiz proje, eksik indeks ve engellenmis kapsam yeniden indeksleme
baslatmadan raporlanir. Gercek kayitli projenin eksik indeksinde dosya agaci
degismedi; 18 yeni test ve komsu regresyon testleri gecti. Alt katmandaki
`query_registered_project` sorgu dogrulama gozlemi yazabildiginden cikti bunu
acikca bildirir. WP-03.03 native dispatcher baglantisi ve gercek coklu indeks
kabul testi tamamlanana kadar aciktir.

**13 Ekim 2026 ara durum — WP-03.04 native kesif talimati:**
OpenCode GLM salt okunur incelemesi, `AGENTS.md` girisini, mevcut `zekam ask`
komutunu ve proje-yerel skill taramasini dogruladi. `AGENTS.md` ajanlara acik,
yetkili dogal dil isteginde eslesen Zekam komutunu kendisinin calistirip kaniti
dogal dille ozetlemesini soyler; kullanicidan komut/digest/UUID/JSON yazmasini
istemez. Native istemcide taze oturum, gercek skill secimi ve komut cagrisinin
olculmesi henuz yapilmadigi icin WP-03.04 aciktir.

**13 Ekim 2026 ara durum — WP-03.05 yapisal kanit kapisi:**
OpenCode Qwen tarafindan ayri `natural_request_answer.py` aciklama katmani
eklendi; koordinator incelemesinde gercek RAG `project_id` UUID'sinin proje
slug'undan farkli oldugu ve ilk taslagin sahte islem basarisi metnini
gecirdigi saptandi. Kayitli proje status readback UUID'si retrieval entry'sine
eklendi, RAG sonucu ve citation `project_scope` ile eslestirildi. Kismi coklu
proje sonucu, eksik locator, abstain/evidence uyusmazligi ve bazi islem basarisi
ifadeleri fail-closed yanita doner. Ilgili 123 test gecti, bir Windows symlink
testi yetki nedeniyle atlandi; Ruff ve hedefli mypy temiz. Bu yalniz yapisal
kanittir: citation'in onerilen her cumleyi anlamsal olarak destekledigi
dogrulanmadi, islem basarisi dil filtresi tum dogal dil varyantlarini kapsamaz,
aciklama kalici kayda ve native cevap akisine baglanmadi. Bu yuzden WP-03.05
ve WP-03'un nihai kullanici kabulu aciktir.

**13 Ekim 2026 ara durum — WP-03.03 route/scope dispatch adapteri (ek katman):**
`natural_request_rag.py` deterministic `RequestRoute` veya dogrulanmis
`NaturalRequestScope` girdisinden (exact biri) injected salt-okunur sorgu
seam'ine baglanir: tek proje tam olarak bir indeks sorgusu, acik coklu
fan-out proje basina ayri cagri ve ayri etiketli sonuc uretir; sonuclar
projeler arasinda birlestirilmez. Cross-project fallback dokumani, projesiz
sonuc ve sahte `grants_authority` bildirimi fail-closed reddedilir; soru
route `query_digest` ile exact baglanir. Belirsiz/declined istek sorgu
seam'ini hic cagirmaz; eksik indeks `unavailable`, politika reddi `refused`
outcome ile raporlanir, indeksleme/document embedding baslatilmaz. 26 hedefli
test gecti; Ruff, ruff format ve hedefli mypy temiz. Commitli
`natural_request_retrieval.py` degistirilmedi; iki katmanin birlestirilmesi
koordinator incelemesine birakildi. WP-03.03 native dispatcher baglantisi ve
gercek coklu indeks kabul testi tamamlanana kadar aciktir.

**13 Ekim 2026 devam — WP-03.03 kanonik CLI baglantisi:** `zekam ask`,
`--project` verilmeyen acik coklu proje karsilastirmasinda route/scope kararini
tek kez alip mevcut `natural_request_retrieval.py` kanonik yolunu calistirir.
Tek proje cikisi korunur; coklu sonuc ayri `zekam-ask-multi-result/v1`
semasini tasir. Gercek proje kaydi ve katalogu kullanan hedefli testlerde
15 yeni senaryo gecti; cevap veren iki indeksin sonucunda sorgu seam'i mock
oldugu icin gercek iki okunabilir indeks kabul kaniti henuz yoktur.
`natural_request_rag.py` alternatif prototip olarak kalmistir, kanonik
dispatch veya paket kabulu olarak sayilmaz. WP-03.03 aciktir.

**13 Ekim 2026 devam — WP-03.03 gercek indeks kaniti:**
`test_ask_real_dual_index.py`, iki kayitli proje icin gercek
`SQLiteKnowledgeIndex` generation'i ve ayri kaynak dosyalari kurdu. CLI
acik karsilastirmada iki ayri `project_id` ve citation verdi; indeksler
`ready/index_readable`, sorgu sonuclari ise yetkisiz uzak embedding rotasinda
acikca `lexical-only-degraded`/stale olarak etiketlendi. Saglayici/ag cagrisi
olmadi, sorgu sirasinda indeks ve generation degismedi. Gercek indeksli dort
kabul testi ve yedi komsu fan-out testi gecti. Eski CLI RAG testleri izole
`HomeLayout`/operasyonel schema ile duzeltildi; birlesik grup 32/32 gecti.
Belirsiz/kayitsiz proje durumunun tum CLI varyantlarinda en az gerekli dogal
dil aciklamasi olarak sunulmasi henuz kanitlanmadigi icin WP-03.03 aciktir.

**13 Ekim 2026 devam — WP-03.03 CLI aciklama siniri tamam:**
`zekam ask`, proje sinyali olup tek kayitli projeye cozulmeyen soruda
`zekam-ask-clarification/v1` ve kisa Turkce soru dondurur; bu yolda sorgu,
indeksleme veya provider cagrisi yapmaz. Kayitsiz acik `--project` girdisi
fail-fast kalir ve kayitli slug onerisi verir. Tek proje sonucu ile acik
karsilastirma semalari korunur. Yeni 12 test ve komsu CLI/gercek cift indeks
testleri birlikte 46/46 gecti; Ruff ve diff kontrolu temiz. Bu kanit
WP-03.03'un yerel CLI kabulunu kapatir. Native istemcinin gercek oturumda
tool/skill secimi WP-03.04, cevap aciklamasinin tam kaniti WP-03.05 ve
WP-12 kabul matrisi kapsaminda aciktir.

**14 Ekim 2026 — WP-03 kapanis kaniti:**
`natural_request_scope.py`, `natural_request_evidence.py` ve route testleri
explicit proje, Jira prefix'i, acik Work ve dogrulanmis oturum sinyallerini
celiskide netlestirir; gercek SQLite Work/proje readback'i ve gercek kaynak dosyasi
SHA-256 readback'iyle “Bunu da GPU icin ekle” baginin baska projeye sizmasi reddedilir.
Kanit dosya icerigini donmeden, salt okunur ve authority'siz kalir.

`zekam ask` artik tek proje ve acik fan-out yollarinda retrieval govdesini ayri
`zekam-natural-request-answer/v1` envelope'ina baglar. Human output yalniz cevap
metnini gosterir; `--json` istenmedikce UUID, digest veya programatik JSON yazdirmaz.
Locator, kaynak digest'i ve proje UUID'si zinciri gecersizse sabit belirsizlik;
indeks/sorgu kesintisinde sabit basarisizlik; clarification'da sabit netlestirme
mesaji doner. Gecerli iki gercek SQLite indeksiyle yapilan fan-out'ta iki ayri
proje citation'i korunur, indeks ve generation degismez.

OpenCode v2.0.29 native taze oturum probu `zekam ask` yolunu, Jira sorusunda
`jira-is-kaydi` skill'ini ve diger proje sorusunda skill'siz ask yolunu secti;
ikinci taze oturum ic komut calistirdi ve sonucu dogal dille raporladi. Native
Python ortaminda paket kurulumu olmadigi icin bu probe cevap yerine bounded
“modul bulunamadi, sorgu islenmedi” raporu verdi; kurulum veya baska komut
calistirilmadi. Yerel `PYTHONPATH=src` test yolu ayni istegi provider'siz
calistirabilir. Bu sinir, native provider/kurulum qualification'ini iddia etmez.

WP-03.02, WP-03.03, WP-03.04 ve WP-03.05 kutulari bu kanit zinciriyle kapatildi;
WP-02 native host aciklari ve WP-06 Jira issue icerik baglantisi kendi paketlerinde
ayri kalir.

### WP-04 — Tam Envanter, Güvenli Parser Ve Proje Bilgi Girdileri

**Bağımlılık/onay:** WP-01/03; yeni generation WP-05 ile.  
**Girdi/kapsam:** REQ-002/005; CAP-07/20; kaynak kod + ODD/GTD + issue/defect + ZIP/XML.

**İncelenecek/değişecek konumlar:** `source_discovery.py`, `ignore_rules.py`, `project_knowledge_index.py`, `knowledge_ingestion.py`, `knowledge_parsers.py`, `secret_detection.py`, `knowledge_file_plane.py`, `infrastructure/knowledge/document_parsers.py`; öneri `index_selection_policy.py`.

- [ ] **WP-04.01** — Discovery metadata ve içerik eligibility adımlarını ayır. Seçili ağacın `.git`, `node_modules`, `.env`, binary ve generated dosyaları dahil metadata envanterini tut; içerik okuma veya embedding için ayrı politika uygula.
- [x] **WP-04.02** — Traversal'ı streaming ve limitli yap; junction/symlink döngüsü veya kök dışına kaçışı engelle. Limit/erişim hatası varsa tam envanter demek yerine partial + devam checkpoint'i ver.
- [ ] **WP-04.03** — Kaynak türleri için AST/structural parçalama; belge türleri için başlık/paragraf/tablo locator; XML için özel parser seç. Unknown formatı ham UTF-8 sayma; PDF/DOCX tablo/sayfa bilgisi uydurma.
- [ ] **WP-04.04** — `.env` ve `.properties` safe key/structure parser'ını local-only uygula. Synthetic secrets'ın prompt/cache/index/log/artifact'a sızmadığını denetle; secret içeriğe birebir public hash üretme.
- [ ] **WP-04.05** — ZIP'te traversal, symlink, entry count/byte/ratio limitleri; XML'de DTD/external entity/network expansion reddi; belgede çalışabilir macro/script yürütmeme sınırlarını uygula.
- [ ] **WP-04.06** — Proje+logical source+revision kimliğini koru. Aynı adlı farklı dosya, aynı içeriğin başka proje kopyası ve gerçek rename olayları farklı sözleşmeler taşısın; kaynak dosyaya yazma yapma.

**Üretilen sözleşme/çıktı:** `FileInventoryRecord`, `EligibilityDecision`, `SourceVersion`, locator'lı `ContentUnit` mevcut tiplerle uzlaştırılır. Öneri `tests/integration/test_project_artifact_ingestion.py`, `tests/security/test_sensitive_config_metadata.py`.

**Kabul ve negatif testler:** Testler: kaynak/generated aynı uzantı, gitignore metadata korunumu, nested dependency ağacı, UTF-8/BOM, büyük/şifreli/bozuk belge, archive bomb/XXE, same-name/different-content, source drift ve kaynakta sıfır mutation.

**13 Ekim 2026 ara durum — WP-04.01/02 kaynak kesfi:** OpenCode Qwen
uygulamasiyla `source_discovery.py` metadata envanteri ekledi. `.git`,
`node_modules`, `.env`, ikili ve uretilmis girdiler metadata olarak gorunur;
yoksayilan icerik acilmaz. Dizin ve global girdi sinirlari tarama sirasinda
uygulanir; Windows junction ve symlink izlenmez; derinlik, erisim ve metadata
sinirinda ve okunamayan dosyada partial durumu ile yol checkpoint'i doner.
Icerik dosya/bayt siniri
metadata tamligini yanlis partial yapmaz. Tam esik ve okuma sirasinda buyuyen
dosya regresyonlari eklendi. Bu bir ara dilimdir: checkpoint henuz yeniden
baslatma API'sine bagli degil; eligibility ayri karar sozlesmesine ve
index/embedding akisina baglanmadi. WP-04.01/02 kabulleri aciktir.

**13 Ekim 2026 ara durum — eksik envanter indeks kapisi:** OpenCode Qwen
uygulamasiyla proje indeks plani, `truncated` ve tree digest kontrolune ek
olarak `inventory_status=partial` sonucunu da reddeder. Gercek derinlik,
dizin girdi ve global girdi limitleriyle uretilen partial raporlar icin
negatif testler gecti; hata metni yalniz sabit neden kodunu tasir, kaynak
checkpoint yolunu aciga cikarmaz.

**13 Ekim 2026 ara durum — eksik tarama durum kapisi:** Proje `scan`
akisi, `truncated` icerik veya `partial` metadata raporunda revizyon, profil
ve `CURRENT` kayitlarindan once reddeder. Dizin kaynak kimligi de `partial`
envanterden uretilemez; hata metninde kaynak yolu yerine sabit neden kodu
vardir. Bu kapilar tamamlandi; checkpoint'ten gercek devam ve metadata/icerik
karar sozlesmelerinin tam ayrimi WP-04.01/02 icin aciktir.

**13 Ekim 2026 devam — WP-04.01 uygunluk karari ve indeks kapisi:**
`source_discovery.py`, envanterdeki her dosya icin icerik uygunlugunu
`EligibilityDecision` ile ayrica kaydeder; yoksayilan/secret/limitli dosya
metadata olarak kalir, icerik secimine girmez. Metadata kapasitesi dolduktan
sonraki dosya okunmaz ve `files` listesine eklenmez. Proje indeks plani,
uygunluk karari, kesif dosyasi ve envanter metadata'sini capraz dogrular;
eksik/celisik karar reddedilir. Kaynak plani her cagrida taze kesif yapar,
bounded allowlist alt kume secimi korunur. Kesif 64 gecti/2 Windows symlink
atlandi; indeks 21 ve komsu RAG/embedding 109 test gecti, Ruff temiz.
Bu yerel proje indeksi kanitidir; diger ingestion yollarinin ve tum secili
agac turlerinin kabul matrisi tamamlanmadigi icin WP-04.01 aciktir.

**13 Ekim 2026 ara durum — indeks yeniden okuma siniri:** Indeks plani,
kesifin kaydettigi dosya boyutunu ikinci okumaya tasir; dogrulanmis okuma
`read_bytes()` yerine sinirli parcalar kullanir. Dosya stat sonrasi buyurur,
kuculur veya digest/mtime degisirse plan fail-closed reddedilir. Ikinci
okuma aninda buyume regresyonu gecti. WP-04.03 yapisal kod parcasi ve diger
belge kabul maddeleri aciktir.

**13 Ekim 2026 ara durum — PDF yapisal locator kapisi:**
`StructuredDocumentParser`, PDF blogunda pozitif tam sayi sayfa locator'u
olmadan heading veya block index bulunsa bile sonucu reddeder. DOCX icin
sayfa numarasi reddi korunur. Gecerli ve gecersiz sayfa fixture'larinda
57 ilgili parser testi gecti. WP-04.03'un AST kaynak parcasi ve diger format
kabul maddeleri aciktir.

**13 Ekim 2026 ara durum — Python AST kaynak parcasi:** Proje indeksleyici
gecerli `.py` dosyalarinda ust duzey sinif/fonksiyon/async fonksiyonlari
AST satir araligi ve sembol locator'u ile ayirir; decorator baslangici ve
aradaki kod satirlari korunur. Buyuk sembol veya gecersiz Python, karakter
ve token siniri uygulayan satir parcacisina duser. Indeksleyici surumu v2'ye
alindi; ilgili RAG/indeks testleri gecti. Java/JS ve diger kaynak turleri
icin yapisal parcalama ile WP-04.03'un nihai kabulu aciktir.

**13 Ekim 2026 ara durum — secret bulgu izi:** Kaynak kesfinin
`SecretFinding` izi, secret degerinin public dogrudan SHA ozetinden surec
yerel rastgele anahtarli HMAC'e tasindi. Public `as_dict` raporu izi
tasimaz; kural/yol/satir bilgisi kalir. Ilgili 90 test gecti. Bu yalniz
WP-04.04'un bulgu izi kapisidir; `.env`/`.properties` local-only yapi
parser'i ve sizinti kabul testleri aciktir.

**13 Ekim 2026 ara durum — hassas config ham icerik kapisi:**
`.env*` ve `.properties` dosyalari, custom ignore negation olsa bile
kesifte yalniz metadata kalir; icerikleri acilmaz. `.properties` ham proje
indeks seciminden ve genel dizin ingestion'undan cikarildi. Kisa degerli
fixture ile 150 ilgili test gecti. Local-only safe key/structure parser ve
prompt/cache/index/log/artifact sizinti kabulu henuz aciktir.

**13 Ekim 2026 ara durum — local-only config yapi parser'i:** Acik cagrilan,
salt-okunur `.env*`/`.properties` parser'i kok/yol, symlink/junction,
dosya boyutu, satir ve anahtar limitleri uygular. Sonuc yalniz guvenli
anahtar adi, satir ve properties grup yapisini LOCAL_ONLY sinifiyla tasir;
deger, ham icerik veya icerik digest'i tasimaz. Sentetik degerlerin sonuc
ve hata alanina sizmamasi icin provider-free test eklendi. Bu parser henuz
native istemci akisi ve tum prompt/cache/index/log/artifact kabul zincirine
baglanmadigi icin WP-04.04 aciktir.

**13 Ekim 2026 ara durum — kayitli projede yerel config komutu:**
`project config-structure`, kayitli projenin dogrulanmis yerel kaynak
kokunde explicit `.env*`/`.properties` yapi okur; provider, indeks veya
embedding cagirmaz. Hassas anahtar adlari ve satirlari sonuc yerine yalniz
redakte sayacina gider; guvenli anahtarlar ve grup yapisi gorulur. CLI
ucundan sentetik deger/anahtar sizinti ve kok disi yol testleri gecti.
Native dogal dil yonlendirmesi ile tum prompt/cache/index/log/artifact
sistem kabulu tamamlanmadigi icin WP-04.04 aciktir.

**13 Ekim 2026 ara durum — exact config dogal dil yonu:** `zekam ask`
acik `.env*`/`.properties` dosya adli yapi sorusunu kayitli proje
kokundeki local-only parser'a yonlendirir; RAG ve uzak provider yolu
cagrilmaz. Birden cok exact dosya sirayla islenir; belirsiz properties
yolu genel sorguya dusmeden reddedilir. JSON ve duz metin yanitlarinda
yalniz guvenli anahtar ve redakte sayaci vardir. E2E testte provider
cagrisi ve home artifact sizintisi yoktur. Serbest dogal dilin tum
varyantlari, native istemci yonlendirmesi ve sistem-geneli kabul henuz
acik oldugundan WP-04.04 tamamlanmadi.

**13 Ekim 2026 ara durum — arsiv metadata kapisi:** ArchiveInspector,
ZIP/TAR girdilerini limit sirasinda toplar; ZIP symlink, TAR link,
surucu/traversal yolu ve yinelenen adlari reddeder. ZIP'in tekil girdi
sikistirma oranini da sinirlar; toplam arsiv padding'i bu kapiyi
asamaz. Sentetik ZIP ve mevcut arsiv guvenlik testleri gecti. Python
`zipfile` merkezi dizini acilista bellege aldigi icin metadata bellek
siniri, diger belge parser'lari ve XML kabul zinciri aciktir; WP-04.05
tamamlandi sayilmaz.

**13 Ekim 2026 ara durum — DOCX XML ve etkin icerik kapisi:** DOCX XML
parcalari ElementTree oncesi Expat deklarasyon kapisindan gecer; DTD,
entity ve external entity reddedilir (UTF-16 dahil). ZIP'de yinelenen
ad, symlink, surucu/traversal, tekil sikistirma orani ve macro/ActiveX/
embedding parcalari reddedilir; content type XML'i de macroEnabled icin
ayristirilir. Parser profil surumu v2'ye alindi. Hedefli parser testleri
gecti. ZIP merkezi dizin metadata bellek siniri ve diger XML/belge
yuzeylerinin tam kabul taramasi surdugunden WP-04.05 aciktir.

**13 Ekim 2026 ara durum — ODI Smart Export XML kapisi:** Smart Export
on okumasindaki ham bayt regex'i yerine akista Expat deklarasyon kapisi
kondu. DTD/entity ve external entity UTF-8/UTF-16 icin reddedilir;
akista byte siniri korunur. Yerel unit ve mevcut CLI e2e testleri gecti.
ODI'nin diger XML yuzeyi ile butun belge/ZIP bellek ve macro kabulleri
tamamlanmadigi icin WP-04.05 aciktir.

**14 Ekim 2026 — WP-04.02 kok reparse kapisi:** `resolve_source_root`, kok
path'i `resolve()` etmeden once symlink/junction/reparse olarak denetler.
Kaynak kesfi, project source binding ve CLI index girisi ayni kapidan gecer;
cocuk symlink/junction girdileri onceki skip davranisini korur. Source discovery,
project RAG, continuity ve ODI drift testleri birlikte 88 passed, 2 Windows
symlink yetki skip'i verdi. Gercek checkpoint'ten yeniden baslatma API'si ve
tam traversal kabul matrisi henuz yok; o tarihte WP-04.02 acik kaldi.

**14 Ekim 2026 — WP-04.02 kapanis kaniti:** `resume_discovery` bounded
checkpoint frontier'ini kok kimligi, yol siniri ve dosya metadata snapshot'i ile
dogrular; disari tasma, `..`, absolute path, symlink/junction, kok drift'i ve
frontier drift'i fail-closed reddeder. Dogrulama sonrasi provider-free bounded
taze traversal ile tamamlanmamış envanter sessizce complete sayilmaz. Kaynak
kesfi testleri **72 passed, 2 skipped**, Ruff ve diff kontrolu temizdir;
`5dfc2a7` ile push edilmistir.

### WP-05 — Artımlı Embedding, Atomik İndeks Ve Hızlı RAG

**Bağımlılık/onay:** WP-04; model/host bağları WP-08'le bütünleşir.  
**Girdi/kapsam:** REQ-002/003/005/006/007; CAP-03/07; WP-01 acceptance hedefleri.

**İncelenecek/değişecek konumlar:** `project_rag_runtime.py`, `project_knowledge_index.py`, `embedding_provider.py`, `embedding_routing.py`, `knowledge_index.py`, `embedded_project_rag.py`, `retrieval_service.py`, `infrastructure/sqlite/knowledge_index.py` ve mevcut provider ledger/cache.

- [x] **WP-05.01** — Cache key'ini proje/realm, içerik, parser/chunk profil, exact model/revision/dimension/normalization sözleşmesine bağla. HEAD değişimi aynı content'i tek başına pahalı embedding miss'ine çevirmesin.
- [x] **WP-05.02** — Yalnız new/changed chunk'ları embed et; silinmiş kaynağın vektörünü tombstone/yeniden yayınla görünümden çıkar. Rename'de uygun vektörü yeniden kullanırken locator/source ownership'i yeni path'e doğru bağla.
- [ ] **WP-05.03** — Batch'leri sayı+token+payload limitiyle kur; bounded concurrency, timeout, transient backoff, quota ve cancellation uygula. Partial batch sonucunu slot sırasıyla varsayma; provider kimliğini doğrula.
- [ ] **WP-05.04** — Staging generation + transaction/CAS pointer yayını ile önceki hazır indeksi koru. Crash ve tekrar halinde kanıtlı biten embedding'leri yeniden çağırma; sonucu belirsiz dış etkiyi uzlaştır.
- [x] **WP-05.05** — Exact/lexical/dense/graph sinyallerini skor toplamıyla körlemesine karıştırma. Reranker yoksa açıklanmış fallback; query için gereksiz full-tree içerik/qualification tekrarı yapma; selected citation freshness'ini koru.
- [ ] **WP-05.06** — Performans karşılaştırmasında aynı anlamı arayan Türkçe soru, exact sembol ve ilişki qrels kullan. Source-only filtre yüzünden belgelerin yok olmasını veya kalite düşüşünü hız kazancı sayma.

**Üretilen sözleşme/çıktı:** Versioned `IndexPlan/Generation/EmbeddingCacheKey/RetrievalEvidence` kontratları; öneri `tests/integration/test_incremental_project_index.py`, `tests/e2e/test_natural_language_document_update.py`, benchmark raw JSON.

**Kabul ve negatif testler:** Testler: unchanged=0 gereksiz embedding, bir dosya/chunk değişimi, delete/rename, same-size/mtime deceptive change, cache corrupt, profile drift, batch partial, cancelled resume, stale pointer, yanlış scope ve performance/quality gate.

**13 Ekim 2026 ara durum — kapsamli vektor cache v2:** Yerel vektor
cache'i proje/realm, parser ref+surum, chunk profili ve tam embedding
profilinin digest'ine baglandi. Icerik ayniysa degisen HEAD/chunk kimligi
ayni kapsamda alias vektorunu yeniden kullanir; yanlis kapsam, bozuk vektor
digest'i ve kimliksiz eski cache satiri yeniden kullanilmaz. Alias secimi
indexli ve tek satira sinirlidir. Ilgili 69 test gecti. Gercek coklu
generation/artimli embedding ve benchmark kabul kaniti tamamlanmadigindan
WP-05.02 aciktir.

**14 Ekim 2026 — WP-05.01 kapanis kaniti:** Gercek local SQLite indeks akisi,
ayni kaynak agacinda yalniz HEAD degistiginde ayni chunk icerigi ve exact
embedding profili icin vector cache'i yeniden kullandi. Ilk kosuda bir provider
batch'i ve bir yeni embedding varken ikinci kosuda `cache_hits=1`, yeni
embedding ve provider batch sayisi sifir kaldi; source revision degisti, tree
digest ve profil kimligi korundu, generation digest'i yeni nesil olarak yayinlandi.
`test_head_revision_change_reuses_document_embedding_cache` ile kanitlandi.

**14 Ekim 2026 — WP-05.02 kapanis kaniti:** Ayni local SQLite indeks akisi
rename ve delete degisimlerinde yalniz gereken nesli yayinladi. Ayni icerikli
rename sonrasinda provider ikinci kez cagrilmadi (`cache_hits=1`, yeni embedding
ve provider batch sayisi `0`); locator ve source ownership yeni yola baglandi.
Delete sonrasinda silinen path current generation icinden cikti, kalan chunk'in
vektoru yeniden kullanildi ve eski generation tarihce olarak korundu. `tests/
integration/test_project_rag_pending_publish.py` icindeki HEAD, rename ve delete
senaryolari birlikte **6 test** ile, local runtime race ekleriyle birlikte hedefli
kosuda **82 test** ile gecti. WP-05.02 kapandi; batch/cancellation, staging
uzlastirmasi ve kalite/performans kanitlari WP-05.03, WP-05.04 ve WP-05.06'da
acik kalir.

**13 Ekim 2026 ara durum — uc sinirli embedding batch:** Indeks plani
ve uygulama ayni batch planlayicisini kullanir. Planlayici en fazla
64 oge, 8192 tahmini token ve 512 KiB JSON istek payload'i uygular;
tek metin icin saglayicinin 256 KiB UTF-8 sinirini korur. JSON kacis
genislemesi hesaplanir, sira sabittir ve buyuk tek chunk fail-closed
reddedilir. Ilgili 79 test gecti. Timeout, partial slot kimligi,
quota, cancellation ve canli performans kabul kaniti henuz aciktir;
WP-05.03 tamamlanmadi.

**13 Ekim 2026 ara durum — embedding slot ve model kimligi:** OpenCode
remote provider her vektor icin acik, tekil ve aralik icinde tam sayi
`index` ister; karisik gelen satirlari indeksle dogru slota yerlestirir.
Yanitin `model` alani secilen exact modelle eslesmezse veya eksikse
reddeder. Genel OpenAI embedding yanit ayristiricisi da pozisyon
varsayimini ve string index donusumunu kaldirdi. Provider-free negatif
testler eklendi. Timeout, quota, cancellation ve canli kabul kaniti
acik oldugundan WP-05.03 tamamlanmadi.

**13 Ekim 2026 ara durum — embedding receipt baglama:** Her iki proje
indeks yolu, vektorleri kalici alana yazmadan once ortak dogrulayici ile
receipt amaci, exact profil, istenen metinlerin girdi digest'i, vektorlerin
cikti digest'i, adet, boyut ve normalize vektor sozlesmesini denetler.
Uyusmayan receipt fail-closed reddedilir; ilgili 83 birim test gecti.
WP-05.03 icin timeout, quota, cancellation ve canli kabul kaniti aciktir.

**13 Ekim 2026 ara durum — HTTP hata sinifi ve kota geri cekilmesi:**
Provider transportu HTTP durumunu sanitize edilmis typed hata olarak
tasir. 429 hiz/kota siniri ve kalici 4xx yeniden denenmez; 408/425 ve 5xx
icin indeksleyici en fazla dort cagrilik 1/2/4 saniye backoff uygular.
Transport arizasi provider failure olarak receipt'e siniflanir. Ilgili
133 cevrimdisi test gecti. Gercek kota butcesi, cancellation, toplam
deadline ve canli kabul kaniti halen aciktir; WP-05.03 tamamlanmadi.

**13 Ekim 2026 ara durum — yalitilmis provider HTTP koprusu:** Uretimde
kullanilan process-isolated transport, child sonucundaki salt sayisal HTTP
durum kodunu typed hataya cevirir; 429 kalici, 503 gecici sinifi ust
katman retry kararina ulasir. Gecersiz hata kodu ham haliyle disari
verilmez. Ilgili 144 cevrimdisi test gecti, iki POSIX-only test Windows'ta
atlandi. WP-05.03 icin cancellation, toplam deadline, kota butcesi ve
canli kabul kaniti aciktir.

**13 Ekim 2026 ara durum — kullanici kesmesinde child temizligi:**
Capability worker, cocuk surec baslatildiktan sonra gelen Ctrl+C
kesmesinde surec agacini sonlandirir, pipe ve okuyucuyu sinirli surede
kapatir, gec gelen sonucu yayimlamadan KeyboardInterrupt'i yeniden
yukseltir. Gercek uzun calisan child negatif testi dahil 32 ilgili
test gecti; Windows'ta iki POSIX-only test atlandi. Programatik iptal,
toplam deadline, kota butcesi ve canli kabul kaniti acik kalir.

**13 Ekim 2026 ara durum — programatik indeks iptali:**
`index_registered_project` iptal event'ini indeksleme, process-isolated
transport ve capability worker'a tasir. Onceden iptal edilen cagri child
baslatmaz; aktif child cancel mesaji ve sinirli grace sonrasi kapatilir,
gec yanit yayimlanmaz. Kesme, kaynak/metadata hazirligi ve batch
sinirlarinda kontrol edilir; retry backoff beklemesi de iptal edilebilir.
Nesil transaction'i basladiktan sonra atomik yayin tamamlanir; bu sinir
WP-05.04 staging tasariminda yeniden ele alinacaktir. Ilgili 207
cevrimdisi test gecti, iki POSIX-only test Windows'ta atlandi. Toplam
deadline, kota butcesi ve canli kabul kaniti aciktir; WP-05.03 acik.

**13 Ekim 2026 ara durum — toplam indeks sure butcesi:** Kayitli proje
indeksi varsayilan 1800 saniyelik, en fazla 7200 saniyelik monotonic
son zamana baglandi. Hazirlik, probe, batch ve retry ayni sureyi kullanir;
process-isolated HTTP child her cagrida kalan sureye gore kisalir.
Son zamandan sonra tamamlanan yanit reddedilir ve retry beklemesi kalan
sureyi asmaz. Ilgili 213 cevrimdisi test gecti, iki POSIX-only test
Windows'ta atlandi. Nesil transaction'i basladiktan sonra atomik yayin
tamamlanir; kota butcesi ve canli kabul kaniti aciktir. WP-05.03 acik.

**13 Ekim 2026 ara durum — yerel remote-cagri kotasi:** Remote probe,
embedding ve retry ayni thread-safe cagri rezervasyonunu paylasir.
Varsayilan limit 256, kayitli indeks API'sinde ust sinir 4096'dir;
`project index` CLI sure ve cagri limitlerini acik seceneklerle tasir.
limit asimi child veya ag cagrisi baslamadan fail-closed durur. Basarisiz
baslatma denemesi de rezervasyon kullanir; iptal veya deadline nedeniyle
baslatilmayan cagri kullanmaz. Sonucta limit ve rezerve cagri sayisi
raporlanir. Ilgili 236 cevrimdisi test gecti, iki POSIX-only test
Windows'ta atlandi. Saglayici hesap kotasi gozlemi ve canli kabul kaniti
acik oldugundan WP-05.03 tamamlanmadi.

**13 Ekim 2026 ara durum — SQLite staging ve CAS temeli:** Yeni
`stage_generation` tam chunk/FTS/vector setini tek transaction'da
`building` olarak kalici kilarken hazir nesli ve current pointer'i korur.
`publish_staged_generation` persisted nesil digest'ini, satir kimliklerini
ve tamligini denetleyip beklenen onceki pointer ile CAS yapar; mismatch,
kismi kayit ve bozuk kimlikte eski hazir nesil kalir. Kismi staging tekrar
kurulabilir. Eski `build_generation` akisi degismedi. Runtime henuz bu
arayuzu kullanmiyor; `rag-state.json` ile atomik yayin/onarim ve belirsiz
dis etki uzlastirmasi acik oldugundan WP-05.04 tamamlanmadi.

**13 Ekim 2026 ara durum — runtime staging yayini ve toparlama:**
Project RAG runtime, yeni nesli eski hazir pointer'i degistirmeden stage eder.
Private pending kaniti yazildiktan sonra SQLite pointer'i CAS ile yayinlanir;
state dosyasi yazimi kesilirse sonraki index/query/status girisi DB'deki
nesle gore state'i tamamlar. CAS oncesi kesintide eski hazir nesil ve state
korunur. Gercek SQLite ile 4 entegrasyon, ilgili 174 birim test gecti
(Windows symlink yetkisi nedeniyle 6 test atlandi). Belirsiz dis etki
uzlastirmasi ve tum kabul kaniti acik oldugundan WP-05.04 aciktir.

**13 Ekim 2026 ara durum — document embedding tekrar kapisi:**
Remote document embedding effect kimligi exact nitelendirilmis profil
digest'ine baglandi. Kalici provider ledger, ayni realm/proje/effect icin
onceki claim receipt'sizse veya basari receipt'i varsa yeni claim'i tek
transaction icinde reddeder; kanitli failed receipt icin mevcut retry
politikasi korunur. Ilgili 130 cevrimdisi test gecti. Farkli batch
sinirlariyla ayni icerigin yeniden gruplanmasi, dis provider sonucunun
dogrulanarak uzlastirilmasi ve canli kabul kaniti acik oldugundan
WP-05.04 tamamlanmadi.

**13 Ekim 2026 ara durum — belirsiz child sonucu:** Provider child HTTP
durum kodu veya gecerli sonuc dondurmeden biterse sonuc `unknown-effect`
olarak receipt'e yazilir; document embedding ayni cagrida otomatik retry
yapmaz ve kalici ledger ayni proje/effect icin sonraki claim'i reddeder.
Iptal edilen effect de muhafazakar olarak belirsiz sinifina alinir.
127 ilgili cevrimdisi test gecti. Provider-side dogrulama, batch yeniden
gruplanmasi ve canli kabul kaniti acik oldugundan WP-05.04 halen aciktir.

**14 Ekim 2026 — WP-05.05 kapanis kaniti:** Retrieval RRF exact, lexical,
dense ve iliski sinyallerini rank tabanli birlestirir; graph reranker binding
uyusmazligi, kullanilamazlik, hazir olmama ve seed yoklugunda acik bypass
trace'iyle fused sirayi korur. Reranker hatasi veya eksik sonucu citation ve
cevap kimligini bozmaz; generation, source revision, source/content digest ve
rank izi secili citation'a bagli kalir. Graph kapali baseline ile binding drift
fallback'i ayni citation ve generation kimligini koruyan regresyon testleriyle
dogrulandi. `test_code_graph_ranking.py`, `test_retrieval.py`,
`test_project_rag_runtime.py`, `test_project_rag_pending_publish.py` ve
`test_embedded_project_rag.py` birlikte 234 test gecti. Canli provider kalite ve
performans olcumu bu kapanisin kapsami degildir; WP-05.03, WP-05.04 ve WP-05.06
icin acik kanit olarak izlenir.

**13 Ekim 2026 07:40 checkpoint:** Kullanici istegiyle bu sohbet
duraklatildi. Son push edilen HEAD, commit edilmemis runtime taslagi,
test kaniti, acik kabul adimlari ve `next_safe_action`
`docs/CHECKPOINT_2026-10-13_0740.md` icinde kayitlidir. "devam et"
diyene kadar gorev uzerinde calisma yapilmaz.

**13 Ekim 2026 17:50 checkpoint:** Kullanici istegiyle ikinci duraklama
eklendi. Son push edilen HEAD `651c978`; E2E kosusunda 82 test gecti, 1 test
OpenCode model semasi/stale plan dogrulama sirasinda kaldi. Sonraki guvenli
adim ve duraklama kaydi `docs/CHECKPOINT_2026-10-13_1750.md` icindedir.
"devam et" diyene kadar yeni is baslatilmaz.

**15 Ekim 2026 - WP-05 gelistirme dalgasi, testler bekliyor:** Kullanici son
talimati geregi test kosulari son toplu dogrulama asamasina birakildi. Bu dalgada
manifestler generation-scoped private staging'e, PROJECT.yaml ve compatibility
manifest yayini SQLite CAS sonrasi pending recovery akisina baglandi. Mevcut
PROJECT.yaml sahiplik/format kapisi CAS oncesi salt okunur denetlenir ve yazimda
tekrar korunur. Kaynak kesfi, dizin girdileri ve dosya parser donguleri mevcut
iptal/deadline butcesini kontrol eder; Git komutu kendi bounded timeout'u icinde
dondukten sonra ayni butce tekrar denetlenir.

Remote document provider icin yalniz normalize vektorleri saklayan append-only
SQLite journal eklendi. Exact realm/project/effect/model/profile ve terminal
claim/receipt baglari dogrulanir; tamamlanmis vektorler cache yazim kesintisinden
sonra yeni provider cagrisi olmadan okunabilir. Sifir yeni cagri receipt'i ancak
document reconciliation evidence digest'iyle kabul edilir. Kaynak metni ve ham
provider yaniti journal'a yazilmaz. Belirsiz sonuc, receipt oncesi kesinti veya
journal'a ulasmayan sonuc otomatik tekrar edilmez; mevcut recovery kapisi kalir.
Batch concurrency bilincli olarak 1'dir; kaynak hazirliginda tek parser veya Git
komutu icindeki anlik kesme, farkli batch gruplamasi, provider hesap kotasi ve
canli kabul halen aciktir.

Statik bagimsiz incelemenin norm hesaplama, receipt/job realm ve PROJECT.yaml
preflight bulgulari kaynakta duzeltildi. Crash/replay/tamper, projection sahipligi,
source cancellation ve sifir-cagri receipt testleri yazildi, HENUZ CALISTIRILMADI.
WP-05.03/04/06 kutulari bu gelistirme notuyla kapatilmaz.

### WP-06 — ODI, Oracle, Kod Grafiği Ve Jira/Confluence Bağlamı

**Bağımlılık/onay:** WP-03/04/05.  
**Girdi/kapsam:** REQ-005; CAP-08/15/19/24/26; gerçek veri bağıntısını tahmin etmeme.

**İncelenecek/değişecek konumlar:** `odi11g_smart_export.py`, `oracle_metadata_index.py`, `code_graph*`, `atlassian_mcp_server.py`, `project_history_query.py`, ilgili SQLite graph ve CLI/MCP adapter'ları.

- [ ] **WP-06.01** — 11g exporttan doğrulanmış model/datastore/column/package/scenario ve referans bağlarını sürümüne göre çıkar. 11g/12c isimlerini/şemalarını eşdeğer varsayma; unresolved relation'ları sakla, sahte lineage kurma.
- [ ] **WP-06.02** — Oracle yalnız metadata amaçlı yetkili okunsun; row data/DDL varsayılan kapsam dışı. Kullanıcı klasöründeki credentials otomatik bağlantı izni değildir. PL/SQL dynamic SQL ilişkilerinde belirsizliği göster.
- [x] **WP-06.03** — Java/JS/TS/TSX/PLSQL graph kapsamını mevcut Python kapsamıyla karıştırma. Hangi ilişkilerin parser/resolver ile kesin, hangilerinin çıkarım olduğunu schema'da ayır; gövde-benzerliğinden kesin çağrı zinciri üretme.
- [ ] **WP-06.04** — Jira key çözümü → gerçek issue/ek → revision/provenance → proje knowledge bağlantısını kur; Confluence pagination, version, permission ve locator'ı koru. Yerleşik read-only MCP'nin güvenlik paritesini kontrol et.
- [ ] **WP-06.05** — “Bu talepte ne değişti?” sorusunu sadece issue özetiyle değil bounded Git history/diff ile bağla. Markdown bilgi güncellemesini issue yazma talimatı sayma.

**Üretilen sözleşme/çıktı:** `SourceRelationship` exact/inferred/unresolved ayrımı, source-edge provenance ve scoped remote read kayıtları. Öneri `tests/integration/test_odi_project_lineage.py`, `tests/e2e/test_issue_document_code_context.py`.

**Kabul ve negatif testler:** Testler: gerçek/sanitized 11g fixture, unresolved ref, sahte edge reddi, Oracle metadata-only, cross-project ACL, Jira 401/403/404/429/pagination, changed attachment, XML secret ve stale graph.

**14 Ekim 2026 — WP-06.01 kapanis kaniti:** `odi11g_smart_export.py` Smart Export
sanitizer'i model/datastore/column/package/scenario kimliklerini exact ODI 11g
alanlarindan cikarir. Senaryo ve package referanslari exact ID ile baglanir;
cozulemeyen target, source ve scenario referanslari `unresolved_relations`
manifest alaninda saklanir ve lineage kenarina cevirmek reddedilir. Yeni e2e
regresyonu sahte `unknown` kenarini reddeder. Downloads'taki gercek Smart Export
yalnizca yerel, salt-okunur preflight/sanitize ile sinandi: 62.8 MB, 41.442
nesne, `sha256:d98cdf5c04092d8964e4ef3724d21f652599a32946dd34944457bda771d51195`,
Yeni allowlist ve exact ID baglariyla `SnpScenTask`, `SnpScenStep`,
`SnpPopMapping`, `SnpReferencedObject`, key/join ve senaryo degiskeni
metadata'si korunur; kaynak degisimi parse sonrasinda da digest/stat ile
reddedilir. Ayni export read-only kosusunda 21.636 sanitized chunk, 9.063
exact edge ve 16.366 unresolved relation uretildi. Unresolved kayitlarin buyuk
bolumu `SnpReferencedObject` hedef baginin bu exportta genel nesne kimligine
cozulememesidir; sahte edge uretilmedi. WP-06.01 gercek 11g referans semantigi
ve kalan unresolved cozumleri icin acik kalir. Ham dosya depoya alinmadi;
provider veya Oracle cagrisi yapilmadi.

**15 Ekim 2026 — WP-06.03 kapanis kaniti:** Polyglot structural extractor Java,
JavaScript, TypeScript, TSX ve PL/SQL uzantilarini Python AST extractor'undan ayri
profilde isler. Import ve containment `extracted`, tekil yerel cagrilar
`inferred`, nitelikli veya bulunamayan cagrilar `unresolved` olarak ayrilir;
yorum ve string icinden sahte edge uretilmez. `--` maskelemesi SQL/PLSQL ile
sinirlandirildi; Java ve JavaScript decrement operatorleri kaynak graph'ini
bozmaz ve extractor profile version 2 ile eski generation'lar sessizce yeniden
kullanilmaz. Gercek proje koklerinde provider/remote cagri olmadan persisted
graph plan/apply ve SQLite integrity kosusu gecti: `sky-spring-ui` 308 dosya,
3.181 sembol, 18.716 edge, 0 hata; `sky-microservis` 1.486 dosya, 10.287
sembol, 57.091 edge, 0 hata; `gpu-fusion` 2.112 dosya, 12.164 sembol,
83.098 edge, 0 hata. Tum generation'larda integrity `passed` ve kaynak
revision'i exact bagliydi. `node_modules` ve Maven `target` graph eligibility
disinda tutuldu; metadata envanteri/icerik policy'si ayri kalir.

**15 Ekim 2026 - WP-06.05 gelistirme, testler bekliyor:** Issue history sorgusu
hareketli HEAD yerine ilk gozlenen exact commit uzerinden log okur; gecersiz HEAD
ve parent kimlikleri fail-closed reddedilir. Git diff'inde external diff'e ek
olarak textconv da kapatildi. Subject ve bounded diff govdesi karakter/satir
kesmesinden once secret taramasindan gecirilir; kesmenin secret kalibini bolup
gorunur prefix sizdirmesi engellenir. Ilgili regresyon testleri yazildi fakat
kullanici talimatiyla son toplu test asamasina birakildi; WP-06.05 kapanmadi.

### WP-07 — Work, Kuyruk Ve İstemciler Arası Devam

**Bağımlılık/onay:** WP-00/02/03; existing operational schema.  
**Girdi/kapsam:** CAP-02/05/06/22; her uzun süreli index/research/test işinin kalıcı durumu.

**İncelenecek/değişecek konumlar:** `operational_store.py`, `infrastructure/sqlite/operational_store.py`, `operational_schema.py`, `local_runtime.py`, `workspace_resume.py`, `native_continuity.py` (varlığı teyit), `continuity` ve OpenCode lifecycle/spool bileşenleri.

- [ ] **WP-07.01** — Work create/list/show/transition/history/dependency/checkpoint/close yollarını doğal dile bağla; mevcut durum makinesi ve optimistic revision'ı koru. Cycle, stale revision ve evidence'sız kapanış reddedilsin.
- [ ] **WP-07.02** — Index, araştırma ve test işlerini aynı project/work/plan/source bağlarıyla checkpoint'e al; ikinci operational authority veya ayrı gerçeklik JSON'u üretme. SQLite'ta kullanıcı projesine göre ayrım gerçek olsun.
- [ ] **WP-07.03** — Lease, fencing, single-writer, outbox ve recovery'yi gerçek race/crash testleriyle doğrula; provider veya Maven çalışırken DB write lock tutulmasın.
- [ ] **WP-07.04** — Yeni model/CLI eski işin artifacts, source/test/config fingerprint'lerini doğrulayarak devam etsin. Yetki sadece eski sohbetten miras alınmasın; mevcut geçerli grant kendi kaynağından yeniden doğrulansın.
- [ ] **WP-07.05** — Native checkpoint anlatısını doğrulanmış Work geçişinden ayır. Stale PID/spool ownership, out-of-order event, missing CAS ve interrupted close recovery açıklanabilir olsun.

**Üretilen sözleşme/çıktı:** `CheckpointEnvelope` ve semantic resume paketi mevcut ledger/CAS'a bağlı; öneri `tests/e2e/test_cross_client_task_resume.py`, `tests/concurrency/test_project_work_resume_race.py`.

**Kabul ve negatif testler:** Testler: process kill (effect önce/sonra), restart, başka client/model, dirty/source/config drift, aynı işi çift başlatma, iki writer, bozuk/missing evidence, expired grant, already-final ve idempotent outbox.

**14 Ekim 2026 — WP-07.04 kismi kanit:** Checkpoint artik source/test/config
fingerprint uclusunu tam digest bagiyla saklar; cross-client resume assignment
plan kimligini checkpoint revision'ina baglar ve fingerprint eksikligi veya
drift'inde fail-closed reddeder. Native model/CLI devam testlerinde plan drift
ve her uc fingerprint drift vakasi dahil 15 test gecti. Canonical Work/Run,
lease/claim/receipt store binding'i ve mevcut grant'in kaynagindan yeniden
dogrulanmasi henuz tamamlanmadigi icin WP-07.04 acik kalir.

**14 Ekim 2026 — WP-07.03 kismi kanit:** Local runtime tek-yazar yarisi
8 yerine 20 paralel worker ile tam bir owner sonucunu korudu. Efekt callback'i
ayri SQLite baglantisinda `BEGIN IMMEDIATE` ve geri alma ile DB write lock'in
efekt cagrisi boyunca tutulmadigini dogrudan sinadi. Iki yeni test ve mevcut
integration kosusu birlikte gecti; process-level crash, provider/Maven canli
kosusu ve tam recovery kabul matrisi henuz olmadigi icin WP-07.03 acik kalir.

**15 Ekim 2026 - WP-07 devam gorunumu, testler bekliyor:** Bounded Work sorgusu
artik state filtresini SQL LIMIT'ten once uygular. `work resume` ve workspace
resume ayri acik/tamamlanmis pencereleri okur; yeni tamamlanmis kayitlar eski acik
isi gizlemez. Archived kayitlar canonical OPEN_STATES disinda kalir. CLI has_more,
packet open_truncated ve blocked_count_scope alanlari bounded gorunumun sinirini
belirtir. Proje filtresi ve mevcut varsayilan sorgu davranisi korunur. Regresyon
testleri yazildi fakat HENUZ CALISTIRILMADI; Work/lease/grant yeniden edinimi ve
WP-07.01..05 kapanis kabulleri halen aciktir.

### WP-08 — Gerçek Alt Ajanlar, Model Seçimi Ve Araştırma

**Bağımlılık/onay:** WP-03/05/07.  
**Girdi/kapsam:** CAP-09/12/25/30; dar context, gerçek bağımsız execution ve config-driven modeller.

**İncelenecek/değişecek konumlar:** `agent_dispatch.py`, `agent_control.py`, `research_runtime.py`, `research_service.py`, `model_registry.py`, `model_gateway.py`, `model_decision_service.py`, `provider_adapter.py`, `infrastructure/clients/adapters.py`, mevcut benchmark/role policy.

- [ ] **WP-08.01** — Host-neutral çalışma kontratını native host capability'lerine bağla; OpenCode'a ait named-agent gereksinimi diğer client'ı sessizce devre dışı bırakmasın. Gerçek child tool yoksa sahte delegate oluşturma.
- [ ] **WP-08.02** — Researcher/explorer, builder, tester, reviewer/verifier, synthesizer ve curator rollerini bütçe/permission/identity/result envelope ile yönet. Ana context'e tüm source veya bütün alt ajan konuşmaları taşınmasın.
- [ ] **WP-08.03** — Paralel salt-okunur araştırma mümkün; aynı writable kaynağa tek builder. Verifier gerçek farklı execution'a ve acceptance subject'ine bağlı olsun; bir modelin başka başlıkla verdiği onay bağımsız sayılmasın.
- [ ] **WP-08.04** — Model ID/provider prefix/catalog revision gerçek config'ten çözülsün. Quality, tools, structured output, context, latency, failure ve cost sinyallerini ayrı ölç; bilinmeyen kotayı yüzde uydurarak doldurma.
- [ ] **WP-08.05** — Canlı benchmark için hedef/fixture/repetition/call/token/maliyet bütçesi göster ve yetki al. Mock pipeline başarısını üretim modeli qualification'ına çevirmeden, veri/model/host snapshot'ını bağla.
- [ ] **WP-08.06** — Araştırma tam soru kapsamını sabit sayıda chunk'a zorlamadan budget ve evidence-gap ile yönetilsin. Kaynak/citation doğrulaması, çelişki, partial ve yanıt üretimini ayrı raporla.

**Üretilen sözleşme/çıktı:** Versioned `AgentTaskEnvelope/AgentResultEnvelope/ModelQualification` mevcut tiplerden türetilir; öneri `tests/e2e/test_host_neutral_research.py`, `tests/security/test_independent_verifier_subject.py`.

**Kabul ve negatif testler:** Testler: missing child capability, malformed result, timeout/cancel, duplicate/cross-project envelope, same verifier identity, model unavailable, quota unknown, unsupported modality ve bounded real host deneyi.

### WP-09 — Test-Only Unit Test Mühendisliğini Tamamlama

**Bağımlılık/onay:** WP-03/07/08.  
**Girdi/kapsam:** CAP-02/13; kullanıcının hedef sınıf/modül ve coverage talebi.

**İncelenecek/değişecek konumlar:** `unit_test_intent.py`, `unit_test_plan.py`, `unit_test_runtime.py`, `unit_test_measurement.py`, `unit_test_loop.py`, `unit_test_plateau.py`, `unit_test_ledger.py` ve mevcut Maven/JaCoCo/native helper adapter'ları.

- [ ] **WP-09.01** — Hedef .java, Maven reactor/modül, package/class ve var olan başarılı testleri çöz. Kullanıcıdan IDE coverage veya teknik digest istemeden gerçek baseline ölç.
- [ ] **WP-09.02** — Analiz→anlamlı test senaryosu→test yazımı→compile/test→taze coverage→bağımsız kalite doğrulama döngüsünü bütçeli ve kalıcı yap. Line/branch ile per-file/aggregate hedeflerini ayır.
- [ ] **WP-09.03** — Varsayılan test-only çalışmada production kaynak, build/pom ayarı, coverage exclusion, mevcut başarılı test silme veya API'yi public yapma yok. Reflection/private-only ve assertionsız/duplicate test yasak.
- [ ] **WP-09.04** — Regresyon, exception, boundary, state/side-effect, tarih, precision/rounding ve bağımlılık hatası ihtiyacını gerçek koddan çıkar. Bir testin yüksek coverage'ını davranış doğruluğu sayma.
- [ ] **WP-09.05** — Fresh Surefire/JaCoCo exec/XML/HTML, test tree/source/toolchain fingerprint ve gerçek exit status ile sonucu bağla. Test yok/failed/stale/wrong-module durumları coverage success değildir.
- [ ] **WP-09.06** — Plateau'da son doğrulanmış kazancı koru, erişilemeyen branch/line ve nedenleri göster; hedef altını başarı sayma. Refactoring ayrı iş/onaydır. Kesintide görev sıfırlanmasın.

**Üretilen sözleşme/çıktı:** Mevcut request/attempt/receipt/terminal ledger; insan okunur rapor + JSON + raw JaCoCo/log/diff/checkpoint referansları. Öneri `tests/e2e/test_natural_language_test_engineering.py`.

**Kabul ve negatif testler:** Testler: multi-module, same class name, existing tests preserved, zero tests, stale JaCoCo, failed build, no-progress, model switch resume, rounding threshold ve protected production/build digest.

### WP-10 — Bellek, Skill, Fikir, Radar Ve Ölçülü Gelişim

**Bağımlılık/onay:** WP-05/07/08; task promotion ile bağlı.  
**Girdi/kapsam:** CAP-10/14/17/18/31; verified lessons, candidate lifecycle ve onaylı activation.

**İncelenecek/değişecek konumlar:** `memory_*`, `skill_packages.py`, `skill_runtime.py`, `experience_skill_bridge.py`, `evolution_*`, `research_campaign_runtime.py`, radar/tournament/measured-loop katmanları ve ilgili SQLite ledger'ları.

- [ ] **WP-10.01** — Kayıt türlerini karar/deneyim/başarısızlık/ders/skill olarak ayır. Provenance, scope, freshness, superseded, conflict ve yetkili düzeltme/arşivleme yollarını doğal dile bağla.
- [ ] **WP-10.02** — Doğrulanmış kök nedenden lesson candidate üret; aynı evidence'ı iki bağımsız gözlem sayma. Memory'yi kullanıcı talimatı veya execution authority'si gibi uygulama.
- [ ] **WP-10.03** — Skill adayını değerlendir, independent review, sürüm, active pointer, dağıtım, gerçek invocation, deprecation ve rollback ile tamamla. Kaynak paket/projection drift'i okunabilir raporlansın.
- [x] **WP-10.04** — Projede ve Zekam'da iyileştirme fikirleri üret; impact/cost/risk/kanıt ve benzer aday dedup bilgisi olsun. Öneriden Work'a geçiş açık scope/approval bağını korusun.
- [ ] **WP-10.05** — Radar/tournament için gerçek kaynak/alternatif, objective/fixture/baseline ve bütçe/no-progress stop kur. Öz değişikliği otomatik kendi kendine onaylatma; root instructions/security/schema/retention kritik değişiklikler ayrı karar gerektirir.
- [ ] **WP-10.06** — Evolution shadow/canary/activation/rollback ve disabled/paused/grant-drift durumlarını gerçek state ile raporla. Model veya ağ yoksa hayali öğrenme sonucu üretme.

**Üretilen sözleşme/çıktı:** Mevcut candidate/eval/activation/invocation/evolution ledger; öneri `tests/e2e/test_natural_language_memory_skill.py`, `tests/integration/test_idea_to_verified_work.py`.

**Kabul ve negatif testler:** Testler: iki projede aynı karar adı, outdated/contradictory memory, forged lessons, self-promotion, baseline regression, skill drift, rollback, expired grant, duplicate candidate ve plateau stop.

**14 Ekim 2026 — WP-10.04 kapanis kaniti:** Proposal impact/cost/risk ve
digest-only evidence alanlarini, deterministik dedup sonucunu ve exact proje/
approval scope bagini korur. `idea.work-create --uygula` effect claim/receipt
zinciriyle ayni Work/job/receipt replay'ini verir; E2E tek Work kaydini ve
terminal evidence readback'ini dogrular. Bagimsiz inceleme ve provider-free
hedefli kosu **52 passed**, Ruff ve manifest temiz; commit `6db35b1` ile
push edilmistir.

**15 Ekim 2026 — WP-10.03 yerel rollback dilimi:** Skill v2 activation effect
artik rollback gecisini hedef/source revision ve onceki CAS head ile ayri digest'e
baglar. `rollback_to_revision` yalniz daha once terminal receipt ile etkinlesmis
eski revision'a, guncel active source head ve yeni `skill.activate-v2` receipt'i
ile gecis yapar; replay ve stale CAS reddedilir. Yeni entegrasyon testi explicit
transition, source binding ve discovery'nin eski revision'a donusunu dogruladi;
iki hedefli kosu **2 passed**, Ruff ve paket manifest temizdir. Native CLI
activation/distribution ve canli client kabul kaniti WP-10.03 icin acik kalir.

**15 Ekim 2026 - WP-10.03 SQLite readback sertlestirmesi, testler bekliyor:**
Activation event dogrulamasi SQLite Row uyeliginde alan adi yerine deger arama
hatasini kaldirir; event digest, revision ve action kontrolleri `row.keys()`
uzerinden uygulanir. Rollback source head'i de terminal active receipt ile
dogrulanmadan hedefe gecis yapilmaz. Gercek sqlite3.Row ile uc alan bozulmasini
sinayan regresyon testi yazildi; son toplu test asamasinda calistirilacaktir.
Bu not WP-10.03'u kapatmaz.

### WP-11 — Operasyon, Rapor, Backup Ve Kurtarma

**Bağımlılık/onay:** WP-07/10; model usage WP-08.  
**Girdi/kapsam:** CAP-11/16/17/22/27; gerçek kayıtlar, scheduler ve veri sahipliği.

**İncelenecek/değişecek konumlar:** `infrastructure/local_backup.py`, `local_analytics.py`, CAS/store/file-plane, scheduler/worker/evolve/doctor ve native scheduler adapter'ları.

- [ ] **WP-11.01** — Kullanıcı “ne yaptın, ne kaldı, hangi model ne kadar sürdü?” dediğinde kanonik events/receipt/usage'dan rapor üret. Ölçülmeyen maliyet bilinmiyor; yüzde/tutar uydurma.
- [ ] **WP-11.02** — Scheduler varsayılanını klasör açmakla etkinleştirme. Onaylı standing grant varsa kapsam/bütçe/expiry/pause/disable/recovery ve exact native kayıt readback'ini uygula.
- [ ] **WP-11.03** — Backup'ı SQLite online snapshot + CAS/manifest ile al; çalışan DB'yi gelişigüzel kopyalama. Restore yalnız yeni/hedefi onaylı alanda, logical digest ve dosya sahipliği doğrulamasıyla yapılır.
- [ ] **WP-11.04** — Derived index/analytics'in yeniden kurulabileceğini, operational state'in oralardan türetilmediğini göster. Missing/corrupt CAS, disk full, outbox backlog ve cancelled recovery'de güvenli davranışı test et.
- [x] **WP-11.05** — Salt “core healthy” sonucunu bütün model/CLI/feature'lar hazır diye sunma. Rapor her CAP'nin gerçek eksik kurulum/kanıt/izin ayrımını taşısın.

**Üretilen sözleşme/çıktı:** Runtime health + usage + backup/restore evidence; öneri `tests/e2e/test_natural_language_operations.py` ve var olan restore/fault testlerinin genişletilmesi.

**Kabul ve negatif testler:** Testler: new-target restore, owner conflicts, index rebuild, backup drift, corrupt/missing object, scheduler pause and disable, two-cycle readback (yalnız onaylı gerçek cihazda), disk/full failure.

**14 Ekim 2026 — WP-11.05 kapanis kaniti:** `capability_readiness` statik envanter,
yerel kanit, izin acigi ve istemci kurulum acigini ayri raporlar; provider/network
cagrisi yapmaz, core sagligini CAP hazirligi olarak yukseltemez ve authority vermez.
Doctor ve `readyz` bu raporu core sagligindan ayri tasir. Capability/health, parser,
research ve project-RAG son focused kosusunda **172 test** gecti; paket validatori,
Ruff ve diff kontrolu temizdir. Bagimsiz WP-11 incelemesi bu ayrimi ve degisken
readiness davranisini dogruladi. Canli provider, native scheduler ve diger acik
WP kabul kapilari bu kapanisin disindadir.

### WP-12 — Bütünleşik Kullanıcı Kabulü, Paket Ve Yaşayan Mimari

**Bağımlılık/onay:** WP-02..WP-11 tamam veya açık blokajlı; final kapı her zorunlu bağı kapatır.  
**Girdi/kapsam:** Bütün REQ/CAP/GAP kayıtları; uygulama diff/test/ADR/evidence.

**İncelenecek/değişecek konumlar:** Mevcut `tests/architecture`, `tests/unit`, `tests/integration`, `tests/e2e`, `tests/security`, concurrency testleri; `pyproject.toml`, paket validator/reachability; mimari paket katalog/üreticileri.

- [ ] **WP-12.01** — Temiz wheel-only kurulumda gerçek public komut/MCP/skill yollarını doğrula. Checkout'ta kalan ama dağıtıma girmeyen modülü hazır diye raporlama. Tüm testleri son değişikliklerden sonra tekrar çalıştır.
- [ ] **WP-12.02** — Bölüm 10 doğal dil kabul senaryolarını gerçek desteklenen istemci/model/OS matrisinde çalıştır; beklenen tool çağrısı, project/effect/citation/response'u ölç. Fixture ve live sonuçlar ayrı kalsın.
- [ ] **WP-12.03** — Baseline'a göre end-to-end kalite, cold/unchanged/delta index ve query tail latency raporunu üret. İyileşmeyen zorunlu hedefi gizleme veya ölçüm sonrası eşiği değiştirip başarı yapma.
- [ ] **WP-12.04** — CAP, arayüz, tablo/migration, test, açık karar ve kaynak hash'lerini tek kanonik mimari girdide güncelle; Markdown/HTML/ZIP/diyagram yerel çıktılarının link/hash/ID tutarlılığını doğrula.
- [ ] **WP-12.05** — Miro/GitBook için yalnız yerel export/plan oluştur; gerçek yayın ayrı onay. Araç/kaynak kullanım günlüğünü, hangi kontrolün yapılmadığını ve version drift yöntemini koru.
- [ ] **WP-12.06** — REQ→CAP→WP→test→run→evidence→commit zincirini tamamla. Kullanıcı senaryosu açıkken ürün tamamlandı deme. Son tam suite, bağımsız review ve release-scope kabul olmadan final Work kapanışı üretme.

**Üretilen sözleşme/çıktı:** `completion-report.md/json`, güncel mimari referans ve aynı `AKTIF_GOREV.md`, test raw results ve scoped commit listesi; gerçek push yok.

**Kabul ve negatif testler:** Testler: clean package, full regression, supported native entry/E2E, fixture/live ayrımı, restore + resume + doc/graph consistency; mandatory unresolved gap varsa incomplete/blocked final.

## 9. Performans, Kalite Ve Veri Kabul Sözleşmesi

### 9.1 Sabit Karşılaştırma Korpusu

Küçük kontrollü source fixture, büyük metadata ağacı, çok modüllü Java+frontend örneği,
Türkçe teknik belgeler, aynı isimli iki proje nesnesi, ODD/GTD ve sanitize ODI XML seçilir.
Fixture/snapshot ve qrels hash'leri, OS/donanım/Python/SQLite/extension, model/profil,
network route ve sıcak/soğuk cache koşulu kayıtlı olsun. Kurumsal gerçek kaynak ancak
yetkili kapsamda kullanılır; yoksa sentetik sonuç “GPU'da doğrulandı” yapılmaz.

| Ölçüm Koşusu | Kesin Kontrol |
|---|---|
| İlk indeks | Dosya/byte/chunk, aşama süreleri, provider calls/tokens, bellek/disk ve hazır generation |
| Değişikliksiz tekrar | Geçerli aynı içerik/profil için yeniden source-chunk embedding çağrısı **0** |
| Tek dosya/parça değişimi | Yalnız etkilenen yeni/changed chunk'lar; diğer geçerli vektörler korunur |
| Silme/rename | Yanlış eski locator yok; gereksiz embedding yok; diğer kaynaklar ve tarih korunur |
| Model/parser/profil değişimi | Etkilenen scope cache invalidation; eski vektör boyutunu karıştırma yok |
| Kesinti ve devam | Kanıtlı tamamlanan işler replay edilmez; unknown sonuç ayrı recovery; önceki indeks sağlam |
| Query | Resolver/retrieval/graph/rerank/synthesis aşamaları ve uçtan uca p50/p95 ayrı |
| Projeler arası izolasyon | Tek proje isteğinde başka scope kanıtı/metadata/sensitive cache sızıntısı **0** |
| Güvenlik canary | Sentetik secret değerleri dış payload/log/vector/raporda **0** |

İçerik embedding tekrarı ile gerçekten süresi dolmuş provider sağlık probe'u ve yeni
query embedding'i farklı sayaçlardır; toplama gizlenmez. Bütün cache'leri kapatıp
sonra açmak yapay performans kazancı sayılmaz. `mtime/size` sadece hızlandırıcıdır;
eşit boyut/zamanla değiştirilmiş içerik için sağlam fingerprint politikasını doğrula.

**Hız ve kalite hedefleri:** WP-01'de baseline ölçümünden sonra, uygulamadan önce,
`acceptance-targets.json` içinde sabitlenir. Soğuk/delta/unchanged süre, p95 sorgu,
Recall@k/MRR/nDCG, citation doğruluğu ve ilişki edge doğruluğu için ölçülebilir hedef,
campaign repetition/budget ve kabul toleransı yazılır. Rastgele “10 kat hızlı” veya
her ortamda bir saniye garantisi yoktur. Kritik exact-symbol/permission/izolasyon
senaryolarında toleransla yanlış proje veya yetkisiz yanıt kabul edilmez.

### 9.2 Envanter Ve İçerik İndeksinin Ayrı Gerçekleri

Tam envanter `.git`, `.env`, dependencies, binary ve desteklenmeyen dosyanın varlığını
kapsar; hepsinin içeriğini okumak, hash'lemek veya embed etmek zorunlu değildir.
Metadataya erişim bile izin dışı veya hassassa bunu güvenli sayım/reason olarak raporla.
Dosya okunamazsa uydurma size/digest yazma. Çok büyük ağaçta bounded streaming/checkpoint
kullan; sessiz truncation veya bütçe dışında sınırsız traversal yoktur. Tamlığı
kanıtlanmamış envanteri complete işaretleme.

### 9.3 Yeni Teknik Kontratlar

WP-01 çıktısında mevcut tiplere uyarlanarak şu sınırlar kesinleşmiş olmalı:

| Mantıksal Sınır | Zorunlu Semantik |
|---|---|
| RequestContext | Gerçek metin/girdi referansı, client/model metadata, doğrulanmış work/scope, provenance |
| ProjectResolution | selected/ambiguous/unavailable; project/family/role; çelişki nedenleri; authority=false |
| InventoryRecord | logical path, kind, okunabilen metadata, eligibility, reason, snapshot; secret payload yok |
| SourceVersion | project/realm/source kind/origin/revision/content policy, user/generated ownership |
| ChunkIdentity | Source/symbol/locator, normalize content, parser/chunk profile; rename ile content yeniden kullanım ayrımı |
| EmbeddingCacheKey | Scope + safe content + exact model/revision/dimension/profile/normalization; hash çakışması kontrolü |
| IndexGeneration | staged/ready/superseded, manifest, publication pointer, interrupted state ve rollback |
| AnswerEnvelope | retrieval/generation state ayrı; citations, scope, limits, actual effects, next safe action |
| ExecutionEvidence | actor/child/run/step, plan digest, effect claim/receipt, gerçek exit/usage, verified subject |
| ContinuityPacket | Kaynak/work/config fingerprint, tamamlanan kanıtlar, pending/blocked, yetki readback |

Bu adlar mantıksal tasarım girdileridir; mevcut schema'ya denk alan varsa onun adını
koru. Körlemesine on yeni sınıf/servis üretme. Yeni DB yapılarında kimlik/ilişkiler/iş
alanları/audit alanlarının fiziksel sırası, tip/null/default/check/index/comment,
UUID taşınabilirliği ve migration/rollback açıkça yazılır. Serbest JSON alanları
şemasız “buraya her şey” deposu yapılmaz.

## 10. Doğal Dil Kabul Senaryoları

Aşağıdaki cümleler **test girdileridir; gerçekleştirilmiş işlem veya gerçek issue adı
iddiası değildir**. Fixture'da beklenen nesneler yaratılır; gerçek GPU/SKY kabulünde
mevcut gerçek referanslarla değiştirilir. Kullanıcının `zekam ...` komutu girmesi
bu tabloda hiçbir senaryonun önkoşulu değildir.

| ID | Kullanıcı İsteği / Olay | Beklenen Sonuç | Paket |
|---|---|---|---|
| NL-001 | Zekam kökünde yeni destekli istemci açılır | İnce giriş/aktif iş keşfi; yetkisiz yeni effect yok | WP-02 |
| NL-002 | Başka projede aynı istemci açılır | Zekam global müdahalesi veya otomatik job yok | WP-02 |
| NL-003 | “GPU'da PCK_HAKEDIS ne yapıyor?” | GPU scope, gerçek/sentetik doğrulanmış sembol, locator'lı açıklama | WP-03/05 |
| NL-004 | “SKY müşteri ekranını incele” | Doğru aile/UI repo, backend ile gerekirse açık ilişki | WP-03/06 |
| NL-005 | “Bunu güncelle” ve bağlam yok | Sadece eksik hedef/işlem açıklaması, rastgele proje seçimi yok | WP-03 |
| NL-006 | “GPU ile SKY arasındaki bu akışı karşılaştır” | Yetkili iki scope ayrı kanıtla karşılaştırılır | WP-03/06 |
| NL-007 | “GPU 5077 talebini incele” | Önce key/scope, sonra gerçek issue erişimi; yoksa açık hata | WP-03/06 |
| NL-008 | “Bu talep için hangi kodlar değişmiş?” | Issue ile bounded Git history/diff bağlantısı, source evidence | WP-06 |
| NL-009 | “Bu GTD dosyasını GPU bilgisine ekle” | Format/ownership/source version; güvenli parse/index; orijinal dosya değişmez | WP-04/05 |
| NL-010 | “Aynı dosyayı tekrar güncelle” | Geçerli cache ile gereksiz embedding 0; gerçek durum raporu | WP-05 |
| NL-011 | “Bu klasördeki ODD'leri GPU'ya ekle” | Tam metadata, type-specific parser; bilinmeyen format reason ile | WP-04/05 |
| NL-012 | “Bu .env ve properties'ten proje yapısını öğren” | Local safe structure; sırlar dışarı veya vektöre girmez | WP-04 |
| NL-013 | “Bu 11g export XML'ini GPU için işle” | 11g nesne/referans/lineage kanıtı; bağlantı/DDL çalıştırma yok | WP-06 |
| NL-014 | “Bu prosedürü kim çağırıyor?” | Kesin graph ilişkisi veya unresolved; benzerlikten çağrı uydurma yok | WP-06 |
| NL-015 | “Şu servis değişirse hangi dosyalar etkilenir?” | Bound impact graph + kaynak revision/locator | WP-06 |
| NL-016 | “Bu sınıfın testlerini %90 branch coverage'a çıkar” | Doğru modül, baseline, test-only döngü ve gerçek sonuç; hedef altı başarı değil | WP-09 |
| NL-017 | Test üretimi ortasında istemci kapanır | Son doğrulanmış test/artifacts korunur; yeni oturum güvenli resume | WP-07/09 |
| NL-018 | “Modeli değiştirdim, devam et” | Aynı Work, geçerli grant/readback, source/evidence doğrulaması | WP-07/08 |
| NL-019 | “GPU'daki açık işlerimi ve engelleri göster” | Gerçek Work geçmişi/bağımlılık; hayali tamamlanan iş yok | WP-07 |
| NL-020 | “Bu araştırmayı uzman ajanlarla incele” | Destek varsa gerçek child execution ve bağımsız verifier | WP-08 |
| NL-021 | “Hangi model bu işte daha uygun?” | Gerçek qualification + unknown ayrımı; kendiliğinden ücretli campaign yok | WP-08 |
| NL-022 | “Geçen sefer bu konuda ne kararlaştırmıştık?” | Scope/freshness/provenance ile karar; çelişki saklanmaz | WP-10 |
| NL-023 | “Bu çözüme göre sonraki sefer aynı hatayı yapma” | Doğrulanmış lesson candidate; otomatik talimat/yetki promotion yok | WP-10 |
| NL-024 | “Bu tekrar eden işlem için skill hazırla” | Candidate/eval/review/active/rollback; aktiflik ile gerçek kalite ayrı | WP-10 |
| NL-025 | “Projede performans iyileştirmesi bul” | Ölçüm/kod kanıtlı öneri; automatic production mutation yok | WP-10 |
| NL-026 | “Zekam kendisini iyileştirsin” | Scope/bütçe/standing-grant ve candidate review; sınırsız öz değişim yok | WP-10/11 |
| NL-027 | “Hangi işleri yaptın, ne kadar sürdü?” | Gerçek events/usage/receipt, bilinmeyen maliyet ayrı | WP-11 |
| NL-028 | “Verilerimi yedekle ve bütünlüğünü doğrula” | Yetkili kapsamda backup+verify, source overwrite yok | WP-11 |
| NL-029 | “Zekam neden çalışmıyor?” | Tek doğal dil health açıklaması; component gap ve next safe action | WP-11/12 |
| NL-030 | “Değişiklikleri commit et, push yapma” | Kapsamlı ASCII Türkçe commit; diğer kullanıcı diff'leri/push yok | WP-02/12 |
| NL-031 | Onaysız push veya eşdeğer yayın denemesi | Kanıtlanabilir publication gate reddi; shell allow tek başına garanti değil | WP-02 |
| NL-032 | Embedding ortasında provider timeout olur | Eski ready generation korunur; unknown/retry/cancel sınırı ve resume | WP-05/07 |
| NL-033 | Kullanıcı dosyası işlem sırasında değişir | Source drift görülür; karışık nesil ve stale citation başarı sayılmaz | WP-04/05/07 |
| NL-034 | Jira/Confluence yetkisi yok veya erişim kesik | No access/unavailable ayrı; cached source tazeliği yanlış sunulmaz | WP-06 |
| NL-035 | “Şu MCP'yi bu istemcilerde kullan” | Var olan config korunarak plan; gerçek global write gerekiyorsa ayrı onay | WP-02 |
| NL-036 | “Bu veriyi sadece yerelde işle” | Remote-disclosure 0; dense yoksa doğru local/degraded sınırı | WP-04/05/08 |
| NL-037 | Görev kaydı/indeks/CAS bozuk | Kanıtlı integrity error; uydurma resume/başarı yok | WP-07/11 |
| NL-038 | İki ajan aynı dosyada değişiklik ister | Tek writer, scope/resource conflict görünür | WP-07/08 |
| NL-039 | Desteklenmeyen client veya model seçilir | Yanlış destek/qualification iddiası yok; kayıtlı açık destek sınırı | WP-02/08 |
| NL-040 | “Şimdi otomasyonu durdur” | Yeni admission kesilir, aktif iş güvenli checkpoint; veri silinmez | WP-07/10/11 |

Doğal dil testleri exact cümle eşlemesine indirgenmez. Paraphrase, Türkçe karakter,
küçük yazım hatası, çok turlu referans, isim/numara çelişkisi ve bilinmeyen niyet
varyantları eklenir. Hata/soru/onay akışında kullanıcıya teknik komut yüklemek yerine
anlaşılır neden sunulur. Native gerçek oturum testi yapılmadıysa yalnız unit parser
başarısıyla `natural-language-verified` yazılmaz.

### 10.1 14 Ekim 2026 uygulama checkpointi

Yerel ve kanıtlanabilir uygulama ilerlemesi şu commitlerde sabitlendi:

- `c17aafe`: host metadata doğrulaması, polyglot Java/JS/TS/PLSQL graph ayrımı ve model qualification bilinmeyenleri.
- `e784372`: fikir proposal/review/work-plan, Work olay/run/usage raporu ve wheel reachable-runtime denetimi.
- `bb23453`: Work transition/run/step/checkpoint/close komutlarının kanonik yüzey sözleşmesi.
- `081b998`: exact approval scope ile fikirden SQLite Work oluşturma.
- `c0c26bd`: stale GPU fixture revision'ını başarı gibi göstermeden `skip/not-run` raporlama.

Son doğrulama: paket doğrulaması `passed`, Git güvenlik bulgusu yok; değişen akışların hedef testleri yeşil.
Tam suite koşusunda 4.179 test geçti ve 1.283 test platform/native/source fixture sınırı nedeniyle atlandı.
Gerçek native client binary'leri, GPU/SKY kaynak revision'ı, Jira/Confluence erişimi, canlı provider/model
qualification ve release acceptance matrisi yetkili ortam kanıtı bekleyen açık kapılardır.

`next_safe_action`: mevcut local test/manifest kanıtını koruyarak canlı kabul bağımlılıklarını ayrı scope'ta
çalıştır; bu kapılar kanıtlanmadan WP-02..12 veya GATE-I/GATE-U tamamlandı işareti koyma.

### 10.2 14 Ekim 2026 yerel regresyon doğrulaması

Checkpoint sonrasında yalnız kaynak kökündeki yerel ve provider-free doğrulamalar tekrarlandı:

- `tests/unit/test_request_routing.py tests/e2e/test_cli_request_route.py tests/unit/test_code_graph_polyglot_extractor.py`: 57 passed.
- `tests/e2e/test_cli_package_acceptance.py tests/unit/test_operational_store.py tests/e2e/test_cli_work_status_natural.py`: 59 passed.
- `tests/unit/test_archive_scanner_security.py tests/unit/test_model_qualification.py tests/unit/test_idea_proposals.py tests/e2e/test_cli_ideas.py`: 22 passed; duplicate ZIP girdisi için beklenen Python uyarısı.
- `tests/e2e/test_cli_backup.py tests/e2e/test_cli_local_worker_scheduler.py tests/e2e/test_cli_local_core.py`: 35 passed.
- `python scripts/paket_dogrula.py`: `status=passed`, `git_security.findings=[]`, `warnings=[]`, `errors=[]`.

Bu sonuçlar yerel davranışın güncel committe korunduğunu gösterir; gerçek OpenCode/Codex/Claude/Gemini
binary kabulü, Jira/Confluence/ODI/ GPU-SKY kaynak erişimi, provider/model qualification ve tam
release matrisi için kanıt üretmez. Açık kapılar aşağıdaki bağımsız gruplardır:

1. Native host ve shell/MCP/push sınırı: WP-02, WP-03.04 ve WP-12.02'nin gerçek istemci/OS koşusu.
2. Harici kaynak bağları: WP-06.01, WP-06.02, WP-06.04, WP-06.05 için yetkili revision ve okuma kanıtı.
3. Canlı model ve performans: WP-08.05, WP-12.03 için provider bütçesi, gerçek ölçüm ve qrels.
4. Test mühendisliği ve devam: WP-09.01..06 için hedef gerçek Maven projesi, taze JaCoCo ve bağımsız verifier.
5. Kesinti/backup/rollback kabulü: WP-07.03..05 ve WP-11.02..04 için gerçek race, crash, native devam ve restore koşusu.
6. GATE-I/GATE-U: bağımsız inceleme, doğal dil kabul matrisi ve release-scope kanıtlarının tek zincirde birleşmesi.

Bu gruplar tamamlanmadan ilgili kutular ve `ready` etiketleri açık kalır; yerel test başarısı canlı kabul yerine yazılmaz.

### 10.3 14 Ekim 2026 ortam probe'u

Provider-free sürüm probe'u gerçek yerel binary'lerle yapıldı: OpenCode `v2.0.29`, Codex
`0.162.0-alpha.17.2`, Maven `3.9.16` ve Java `21.0.11 LTS` bulundu. Claude Code ve Gemini
CLI bu hostta bulunamadı. `tests/integration/test_real_maven_unit_test_fixture.py` 2 testle
geçti. OpenCode lifecycle sunucu koşusu sonuç üretmeden sonlandırıldığı için kabul kanıtına
çevrilmedi; bu kapı `not-run` olarak kaldı.

### 10.4 14 Ekim 2026 14:13 checkpointi

Bu checkpoint'te HEAD `dab4d02` ve `origin/main` ile senkron durumdadır. Son yerel kanıtlar
paket doğrulaması `passed`, Git güvenlik bulgusu yok, yönlendirme/graph 57, paket/work 59,
güvenlik/model/fikir 22, backup/scheduler/core 35 ve Maven fixture 2 test geçişidir.

Native OpenCode probe'u `v2.0.29` binary'sinin çalıştığını, fakat lifecycle kabul fixture'ının
`--log-level ERROR` argümanını reddettiğini gösterdi; bu sürümde geçerli değer küçük harfli
`error` olmalıdır. Bu nedenle native lifecycle kapısı henüz kabul edilmedi ve test düzeltmesi
sonraki devam adımına bırakıldı. Codex `0.162.0-alpha.17.2` çalışıyor; Claude Code ve Gemini
CLI bu hostta yok.

`next_safe_action`: OpenCode lifecycle fixture'ında log-level değerini sürüm uyumlu hale getir,
izole native server health/session/delete akışını tekrar çalıştır, sonra kalan native/MCP,
harici kaynak, canlı model ve release kapılarını kanıtla. Kullanıcı tarafından tutulan dört
untracked prototype/target yolu stage edilmedi ve değiştirilmedi.

### 10.5 14 Ekim 2026 OpenCode v2 lifecycle checkpointi

OpenCode `2.0.29` native host gercek Bun runtime'inda incelendi. V1 named export eklenti
girisi v2 tarafinda yuklenmedigi icin lifecycle kaydi uretemiyordu; kaynak template'e V2
`default`/`setup` adaptoru eklendi ve mevcut V1 runtime yolu korundu. Adaptör Zekam workspace
sinirini yeniden kullanir, V2 event subscription ile `session.created` olayini ayni spool/fake
executable yoluna yollar ve kapatma fonksiyonunu kaydeder.

Kanit:

- `OPENCODE_EXECUTABLE=... python -m pytest -q tests/e2e/test_opencode_lifecycle_plugin_runtime.py`:
  `10 passed`.
- `PYTHONPATH=src python -m pytest -q tests/unit/test_opencode_agent_bootstrap.py` ile bootstrap
  template/ownership testleri gecti; ayni dosyanin native testi executable yolu acik verilerek
  ayrica `passed` oldu.
- Native provider-free kosu gecersiz `missing/nope` model kimligi ile sinirlandi; gercek provider
  veya credential cagrisi yapilmadi. Gercek event capture'da `session.created` goruldu.
- Persistent `serve` + ayni server'a baglanan `run` ve `session delete` istemci akisinda
  `session.created` ve `session.deleted` olaylari gercek event capture ile goruldu.
- Provider-free V2 harness'i `session:context`, `session:compaction`, `tool:execute.before` ve
  `tool:execute.after` kayitlarini ve sentetik created/deleted yonlendirmesini `passed` olarak
  olctu; bu, gercek model/tool calismasi kabulü degildir.

Bu checkpoint V2 plugin yukleme ve created/deleted olay teslimini kanitlar; native compaction,
tool hook, MCP/shell siniri, harici kaynak, canli model ve release kabul kapilari acik kalir. Dört kullanici
untracked prototype/target yolu stage edilmedi ve degistirilmedi.

`next_safe_action`: native compaction ve tool hook icin ayni persistent server/client akisini ayri bir
izole fixture olarak kur; sonra WP-02..12 icin kalan canli, harici kaynak, performans ve release
kapilarini tek tek kanitla. `tamamlandi` iddiasi icin acik kapilari kapatma veya `not-run` olarak
raporlama gerekir.

### 10.6 14 Ekim 2026 dogal dil yerel regresyon kaniti

OpenCode v2 degisikligi sonrasinda dogal dil routing ve RAG yuzeyleri provider-free tekrar kosuldu:

- `tests/unit/test_natural_language_dispatch.py tests/unit/test_natural_request_scope.py
  tests/unit/test_natural_request_retrieval.py tests/unit/test_natural_request_answer.py`:
  `116 passed`.
- `tests/integration/test_natural_request_evidence.py tests/integration/test_natural_request_cli_fanout.py`:
  `22 passed`; Windows symlink negatif fixture'i privilege gerektirmeyen reparse metadata
  simulasyonu ile artik atlanmiyor.
- `tests/e2e/test_natural_language_workspace_entry.py tests/e2e/test_cli_project_rag.py -k not real`:
  `24 passed, 2 deselected`.

Bu kanitlar deterministic proje/scope, citation kapisi ve offline RAG akisinin yerel regresyonunu
gosterir. Gercek native discovery, Jira/Confluence/ODI okuma, canli model, performans qrels ve
release acceptance yerine yazilmaz; WP-03.02/04/05 ve WP-12.02/03 acik kalir.

`next_safe_action`: native compaction/tool hook davranisini canli provider'dan ayri kabul siniri
olarak koru; canli kaynak, performans ve release kapilarini tek tek kanitla. Kullaniciya ait
untracked prototype/target yollarini stage etme.

### 10.7 14 Ekim 2026 symlink ve V2 hook kaniti

Windows'ta `CreateSymbolicLink` yetkisi olmadan kosan negatif test artik `skip` degil. Gercek
symlink olusturulamayan ortamlarda yalniz hedef dosyanin `lstat` reparse/symlink metadata'sini
fixture ile modelleyerek `session-file-symlink` fail-closed politikasini dogruluyor; kaynak
icerigi veya sistem yetkisi degistirilmiyor. Integration evidence: `22 passed`.

OpenCode V2 provider-free harness'i kayitla sinirli kalmayip kaydedilen callback'leri cagiriyor:
`session:context` ve `session:compaction` resume packet'i output'a ekliyor; `tool:execute.before`
ve `tool:execute.after` olaylari ayni call id ile lifecycle spool/event yuzeyine iletiyor.
Native persistent OpenCode `2.0.29` kosusu da ayni committe `session.created` ve `session.deleted`
olaylarini gercek server/client akisi ile `10 passed` olarak korudu. Canli model/provider, gercek
native compaction ve tool calistirma, MCP/shell, harici kaynak ve release kabul kapilari halen
`not-run`/acik; bu fixture kaniti bu kapilarin yerine gecmez.

`next_safe_action`: canli kaynak ve model gerektiren kapilar icin yetkili ortami ve butceyi
ayristir; native compaction/tool acceptance'i provider-free callback kanitindan ayri raporla.

### 10.8 14 Ekim 2026 native istemci ve Maven probe kaniti

Provider-free ortam probe'u hedefli olarak yeniden kosuldu:

- `tests/integration/test_real_maven_unit_test_fixture.py`: `2 passed` (Java `21.0.11`, Maven
  `3.9.16`; test-only fixture ve taze rapor kaniti).
- `tests/e2e/test_codex_lifecycle_hook_runtime.py tests/e2e/test_claude_lifecycle_hook_runtime.py
  tests/e2e/test_real_client_binaries.py`: `1 passed, 5 skipped`. Codex kurulu `0.162.0-alpha.17.2`
  sabitlenmis kabul surumu degil; Claude ve Gemini CLI yok; tarihsel OpenCode/Codex binary
  eslesmeleri de kurulu degil. Bu skip'ler basariya cevrilmedi.
- `OPENCODE_EXECUTABLE=... python -m pytest -q tests/e2e/test_opencode_lifecycle_plugin_runtime.py`:
  `10 passed`; native persistent server/session lifecycle ve provider-free V2 callback kaniti
  10.7 ile ayni kapsamda korundu.

Bu probe yerel toolchain/fixture sagligini gosterir; canli provider, harici Jira/Confluence/ODI,
gercek kaynak revision'i, performans qrels'i, sabitlenmis eski client acceptance'i veya release
kapsamini kapatmaz. Ilgili WP-02..12 kutulari ve GATE-I/GATE-U bu nedenle acik kalir.

`next_safe_action`: mevcut kurulu Codex alpha surumu icin yeni acceptance sozlesmesi tanimlamadan
versiyon kapisini acma; yetkili sabit binary veya kullanici karari saglandiginda native matrisini
ayri calistir. Harici kaynak ve canli model kaniti yokken `completed` sonucu uretme.

### 10.9 14 Ekim 2026 ingestion ve checkpoint bag kaniti

Iki yerel fail-closed acikligi kapatildi:

- `DirectoryScanner` artik `Path.rglob` ile tum agaci once toplamiyor. Isim sirali iteratif
  `os.scandir` traversal'i kullaniyor; symlink ve Windows reparse/junction girdileri hedefe
  girilmeden `symlink izlenmez`/`symlink/junction izlenmez` karari aliyor. `max_entries` ve
  `max_total_bytes` sinirlari dahil edilen her dosyada uygulanarak limit asiminda tarama hemen
  duruyor. Kokun kendisi de reparse/symlink ise resolve edilmeden reddediliyor.
  `tests/security/test_knowledge_security.py`: `14 passed, 1 skipped`; privilege-independent
  sahte reparse metadata fixture'leri hem kokun hem junction hedefinin kuyruga alinmadigini
  kanitliyor. Gercek symlink kurulumu Windows yetkisi nedeniyle tek skip olarak kaldi.
- SQLite `record_checkpoint` artik Run'daki `source_snapshot_id` ile checkpoint girdisini tam
  eslestiriyor. Kaynakli Run'da snapshot'siz, kaynaksiz Run'da snapshotli checkpoint reddediliyor;
  `tests/unit/test_operational_store.py`: `54 passed`; `tests/integration/test_native_continuity.py`:
  `12 passed`.

Bu kanit WP-04.02 ve WP-07.02/04 icin yerel guvenlik paritesini artirir; parser/format eligibility,
gercek race/crash recovery, native devam, harici kaynak, canli model ve release kabul kapilarini
tek basina kapatmaz. Kullaniciya ait untracked prototype/target yollarina dokunulmadi.

`next_safe_action`: reparse/junction negatifini privilege-independent fixture ile security suite'e
ekle; ardindan WP-04 parser/ZIP/XML kapsamlarini ve WP-07 lease/outbox crash kosularini ayri kanitla.

### 10.11 14 Ekim 2026 code graph reparse kaniti

SQLite code graph store'un writable path/parent ve writer lock kontrolleri ile read-only sidecar
identity akisina Windows reparse/junction reddi eklendi. Boylece `is_symlink()` false donen
junction girdisi graph veritabani veya lock dosyasi olarak acilamiyor. Yetki gerektirmeyen bir
mock reparse negatif testi ile davranis sabitlendi.

Kanıt: `tests/unit/test_code_graph_negatives.py tests/unit/test_code_graph_query.py
tests/unit/test_code_graph_ranking.py`: `39 passed, 1 skipped`; tek skip gercek file symlink
olusturma yetkisine bagli ve mock reparse kaniti bunun yerine geciyor.

Bu degisiklik graph path guvenligini artirir; graph parser/resolver'in tum dil kapsami, canli
harici kaynak ve release kabul kapilarini kapatmaz.

`next_safe_action`: ayni reparse parity'sini gerekiyorsa diger SQLite projection stores'larinda
hedefli olarak denetle; ortak private path helper'i uzerinden duplicate security kodu olusturma.

### 10.12 14 Ekim 2026 knowledge index reparse kaniti

SQLite knowledge index de graph store ile ayni junction/reparse sinirina getirildi: writable
index path/parent, writer lock, read-only ancestor ve offline sidecar identity kontrolleri
`lstat` reparse metadata'sini de reddediyor. Windows file-symlink yetkisi gerektirmeyen negatif
test path'in daha acilmadan durdugunu kanitliyor.

Kanıt: `tests/unit/test_sqlite_knowledge_index.py`: `80 passed, 6 skipped`; skip'ler mevcut
gercek symlink fixture'larinin Windows yetki siniridir, mock reparse negatifi kosmustur.

Bu parity duzeltmesi projection path guvenligini kapsar; diger SQLite projection'larinda ayni
kontrolun tum yuzeylere yayilmasi, harici kaynak ve release kabul kapilari halen aciktir.

`next_safe_action`: ortak path-security helper'inin kalan SQLite projection store'larinda
hedefli kullanimini ve sidecar lock negatiflerini envanterle; gereksiz toplu refactor yapma.

### 10.13 14 Ekim 2026 package/security tekrar dogrulamasi

Yeni reparse testleri sonrasinda paket dogrulama yeniden kosuldu ve `status: passed` dondu:
required files, schema/YAML projection, model/phase sayilari ve Git security gecisleri temiz.
Security scan'in tarihcede yakaladigi sentetik `id_rsa` fixture izi current testten kaldirildi;
onceki immutable commitlerdeki ayni test fiksturu icin yalniz exact path/rule/fingerprint history
allowance korundu. Bu allowance secret degeri saklamaz ve yeni credential eklemez.

`next_safe_action`: allowance listesini genisletmeden kalan SQLite projection path'lerini hedefli
incele; her yeni fixture'da secret pattern yerine siradan sentetik metin kullan.

### 10.14 14 Ekim 2026 operational ve projection reparse parity kaniti

Kalan yuksek etkili SQLite path yuzeylerinde junction/reparse kontrolleri dar kapsamla
tamamlandi. Dormant V4 close/PreCompact acilislarinda ortak lstat metadata kontroli,
operational schema'da leaf ve tum parent zinciri kontrolu, evidence routing/improvement/model
registry store'larinda ise ortak `path_or_parent_is_link_or_reparse` yardimcisi kullaniliyor.
Boylece `Path.is_symlink()` false donen Windows reparse noktasi `resolve()` veya SQLite URI
acilisina ulasmadan duruyor.

Kanıtlar: `tests/unit/test_local_continuity_v4_writer.py`: `28 passed`; dormant macOS
PreCompact test modulleri bu Windows hostunda `pwd/fcntl` import siniri nedeniyle kolekte
edilmiyor, degisiklikler `py_compile` ile ayrica dogrulandi. Operational schema reparse,
v3, v5 ve v6 kosulari toplam `102 passed`; ortak local file/store yuzeyi `45 passed, 5 skipped`.
Commitler `aecfc18`, `eb8454e`, `216faf7` ASCII-only mesajlarla origin/main'e push edildi.

Bu kanit yerel path guvenligini genisletir; WP-04 parser/format, WP-07 gercek race/crash,
canli provider/model, harici Jira/Confluence/ODI ve release kabul kapilarini kapatmaz.

`next_safe_action`: kalan SQLite projection path'lerini ayni ortak yardimciyla envanterle
ve yalniz somut sidecar/lock acigi varsa hedefli test ekle; toplu refactor yapma.

### 10.15 14 Ekim 2026 learning ve benchmark reparse parity kaniti

Envanterin sonraki dar halkasinda `SQLiteLocalLearning`, `SQLiteSkillLifecycle` ve
`SQLiteLocalBenchmarkLab` path admission/connection kontrolleri ortak
`path_or_parent_is_link_or_reparse` yardimcisina baglandi. Parent junction'i mevcut regular
dosyanin `private_regular` kontrolunu gecse bile `resolve()` ve SQLite acilisindan once
reddediliyor; artifact root ve migration backup hedefleri de ayni sinira tabi.

Kanıt: `tests/unit/test_local_file_security.py`,
`tests/unit/test_wp16_sqlite_local_stores_coverage.py`,
`tests/unit/test_wp16_sqlite_runtime_worker_extra_coverage.py` ve
`tests/unit/test_wp16_embedding_learning_remaining_wp05.py`: `84 passed, 5 skipped`.
Commit `4a73038` ASCII-only mesajla origin/main'e push edildi. Windows symlink yetkisi ve
macOS/POSIX izin semantigi skip'leri onceki kapsamla ayni kaldı.

Bu adim local memory/skill/benchmark path guvenligini genisletir; gercek model benchmarki,
provider, Jira/Confluence/ODI, performans qrels ve release kabul kapilari halen not-run/acik.

`next_safe_action`: kalan `local_runtime`/sidecar/lock yazim noktalarini hedefli incele;
operational status helper'i zaten parent reparse reddettigi icin duplicate refactor yapma.

### 10.16 14 Ekim 2026 WP-02 MCP ve native envanter kapanis denemesi

WP-02 hedefli provider-free regresyon kosusu `89 passed, 1 skipped` verdi; proje kokusu,
alt dizin ince girisi, baska proje inert davranisi, duplicate canonical instruction, capability
matrix, MCP plan/apply/readback/rollback ve OpenCode publish siniri birlikte dogrulandi.
Windows symlink skip'i privilege siniriyla sinirli kaldı. Native OpenCode V2 lifecycle kabulunde
`10 passed` var; bu instruction discovery veya shell/push kabulunun yerine yazilmaz.

MCP icin `mcp_integrations.py` native hedef dosyanin tum mevcut parent zincirinde
`symlink/reparse` reddi, guvenli native root admission'i ve rollback planindan once
receipt-sonrasi native digest drift kontrolu ekledi. Yeni negatif kanitler:
`tests/unit/test_mcp_integration.py` ve `tests/e2e/test_cli_mcp.py` toplam `23 passed`;
Codex ve Claude yabanci girdi/credential korumasi da ayri parametrik fixture ile olculdu.
Commit `f3c05bb` ASCII-only mesajla origin/main'e push edildi.

Salt-okunur gercek kullanici envanteri ayni oturumda `integration status --json` ve
`integration sync --scope user --json` ile okundu. Kanit: `status_digest=sha256:670c7d9ed8f55d6555b5a2b4b8350403f569119b787bfe0bcf9481886f32d121`,
`plan_digest=sha256:897bad95bfabec37e551abffdf293238e70188efb34f927374738df3dfdfb8ba`,
`read_only=true`, `provider_calls=0`, `network_calls=0`, `operations=[]`,
`pending_project_cleanup=[]`; mevcut `.agents/skills` conflict'i korunarak migration no-op/plan-only
kaldi. HOME veya credential tasinmadi ve native global ayar yazilmadi.

MCP ayni isim conflict'inde default yol yabanci girdiyi korur; `--adopt` yalniz acik kullanici
secimiyle destructive sahiplik devridir ve eski yabanci degerin rollback'te geri gelmesi garanti
edilmez. Farkli isimli yabanci girdiler icin Codex/Claude parametrik apply ve rollback kaniti
eklendi.

WP-02 kutulari yine de tamamen kapanmadi: Claude Code ve Gemini CLI bu hostta yok,
Codex/OpenCode sabit reviewed acceptance surumleriyle eslesmiyor; native instruction discovery
ve duplicate skill winner davranisi gercek yeni oturumda olculmedi. OpenCode dogrudan `git push`
deny JSON policy'si mevcut olsa da gercek shell transport enforcement'i ve `bash -c`/wrapper gibi
dolayli publication guard'i yok. Capability matrix bu durumlari `unobserved`/`declared` olarak
koruyor; bu siniri ready veya tamamlandi diye etiketlemek kanit sozlesmesine aykiri.

`next_safe_action`: WP-02 icin eksik native binary/versiyon kabul kosularini yetkili hostta,
shell transport authorization ledger'ini ise ayri typed adapter olarak tamamla; mevcut hostta
global migration apply veya provider cagrisi yapma.

### 10.17 15 Ekim 2026 son yerel dogrulama

Son kaynak degisikliklerinden sonra `PYTHONPATH=src` ile final yerel shard kosulari
yenilendi. Capability/health, parser, research ve project-RAG focused paketi **172
passed** verdi; a-f unit shard'i tek Windows process'inde **841 passed, 63 skipped**,
n-r unit shard'i **1612 passed, 52 skipped**, e2e shard'i **226 passed, 48 skipped**,
integration+architecture+property+security shard'i **638 passed, 253 skipped**
verdi. Diger unit shard'lari da hata vermeden tamamlandi. Paralel kosuda gorulen
iki Windows ACL/rename yarisi tekli kosuda tekrarlanmadı; hedef testler tekli
kosuda **2 passed** oldu. Paket validatori `status=passed`, Ruff ve `git diff --check`
temizdir. Skip'ler Windows symlink yetkisi, macOS/POSIX modulleri, bulunmayan native
CLI ve erisilemeyen gercek kaynak/provider fixture'lari olarak ayri tutuldu.

Bu yerel kanit native istemci, canli provider/model, harici Jira/Confluence/ODI,
gercek Java hedefi, scheduler/restore cihaz kabulü ve release GATE-I/GATE-U kapilarini
kapalmaz. `next_safe_action`, bu yetkili kanitlari ayri scope'ta toplamak ve acik
kutulari `ready` olarak isaretlememektir.

### 10.18 15 Ekim 2026 DOCX ZIP metadata kapisi

DOCX parser, merkezi dizin boyutunu `ZipFile` olusturulmadan once
`MAX_DOCX_CENTRAL_DIRECTORY_BYTES` ile sinirlar ve limiti parser profilinde tasir.
Boyut asimi fail-closed `PolicyViolation` ile durur; mevcut girdi sayisi, acilmis
bayt, sikistirma orani, traversal, symlink, duplicate, XML DTD/entity, external
relationship ve etkin icerik kapilari korunur. Parser surumu 3'e yukseltildi.
Yeni erken-reddetme testi ile parser + manifest kabul paketi **19 passed**,
`paket_dogrula.py`, Ruff ve `git diff --check` temizdir.

Bu dilim DOCX merkezi dizin metadata baskisini azaltir; WP-04.05'in diger belge
yuzeyleri, tam format matrisi ve canli/release kabulleri halen aciktir.

### 10.19 15 Ekim 2026 Java structural declaration dilimi

Proje kaynak indeksleyicisi, Java ve polyglot yapisal parcaciklarda ust duzey
`enum` ve `record` bildirimlerini de isim, satir araligi ve `SYMBOL` locator'i ile
ayirir. Dengesiz veya belirsiz kaynaklar icin bounded satir fallback'i korunur;
indexer surumu 3'e yukseltildi. Ilgili unit ve embedding boundary paketi
**40 passed** verdi; Ruff ve `git diff --check` temizdir.

Bu dar dilim WP-04.03'un Java declaration boslugunu kapatir; AST/Tree-sitter
kalitesi, diger dil yuzeyleri ve tam kabul matrisi halen aciktir.

### 10.20 15 Ekim 2026 backup kaynak census kapisi

Yerel bundle olusturma, SQLite online snapshot ve staging/atomic publish akisina
ek olarak baslangic ve bitis kaynak census kimligini karsilastirir. Islem sirasinda
yeni veya silinen kaynak, ya da kaynak dosya kimligi degisimi gorulurse staging
temizlenir ve hedef bundle yayinlanmaz. Yalniz mtime degisimi drift sayilmaz;
spool/analytics gibi canli agaclarda gereksiz reddetme yapilmaz. Backup e2e,
manifest ve kalan backup coverage paketi **83 passed, 16 skipped** verdi; Ruff,
manifest kabulü ve `git diff --check` temizdir.

Bu dilim WP-11.03/04'un kaynak census boslugunu daraltir; gercek cihazda iki
dongulu restore, scheduler ve release kabul kapilari halen aciktir.

### 10.21 15 Ekim 2026 TypeScript type alias parcasi

Proje kaynak indeksleyicisi, ust duzey TypeScript/Flow `type` alias bildirimlerini
generic parametre, nesne tipi, tuple ve fonksiyon tipi gibi dengeli govdelerle
sembol parcacigina ayirir. Maskelenmis yorum/dizgi icerigi alias olarak taninmaz;
semicolon olmayan dosya sonu alias'i da bounded olarak desteklenir. Belirsiz veya
dengeyi bozan alias'lar onceki satir parcasi fallback'ine duser. Indexer surumu
4'e yukseltildi ve type alias locator testleri eklendi.

Bu dar dilim WP-04.03'un TypeScript declaration boslugunu daraltir; tam AST/
Tree-sitter kalitesi, diger dil yuzeyleri ve kabul matrisi halen aciktir.

### 10.22 15 Ekim 2026 XML agac limitleri

Proje kaynak XML parcacigi, DTD/entity deklarasyon kapisina ek olarak bounded
element sayisi ve agac derinligi kontrolu uygular. Limit asimi `PolicyViolation`
ile fail-closed reddedilir; gecerli XML'de var olan child entry locator semantigi
korunur. Indexer surumu 5'e yukseltildi ve element/derinlik sinir testleri eklendi.

Bu dar dilim WP-04.05'in kaynak XML bellek ve derinlik riskini azaltir; ODI,
belge parser'lari, diger XML girisleri ve tam kabul matrisi halen aciktir.

### 10.23 15 Ekim 2026 bounded Git diff kayit kapisi

Jira issue anahtariyla HEAD gecmisi okuyan bounded history parser'i, `-z`
name-status ciktisinda eksik veya bos kaydi sessizce yutmaz; bozuk kayit
`GitCommandError` ile fail-closed reddedilir. Gecerli kayitlar onceki dosya,
hunk ve redaksiyon limitlerini korur.

Bu dar dilim WP-06.05'in malformed Git history kanit boslugunu daraltir;
gercek issue/Confluence okuma, canli provider ve tam proje degisiklik baglantisi
halen aciktir.

### 10.24 15 Ekim 2026 bounded Git log kayit kapisi

Jira issue gecmisi parser'i, bounded `git log` kayit akisi icindeki bos veya
eksik ara kaydi sessizce atlamaz. Tek izinli son ayirici korunur; aradaki bos
kayit `GitCommandError` ile fail-closed reddedilir ve commit sayisi kaniti
kaydirilmaz.

Bu dar dilim WP-06.05'in malformed Git log kanit boslugunu daraltir;
gercek issue/Confluence okuma, canli provider ve tam proje degisiklik baglantisi
halen aciktir.

### 10.25 15 Ekim 2026 bounded Git log tail dogrulamasi

History parser'i MAX_COMMITS kesmesinden sonra kalan bounded kayitlarin temel
alan bicimini de kontrol eder. Bozuk SHA veya ayirici sayisi tasiyan tail kaydi
gorunume alinmasa bile `GitCommandError` ile reddedilir; truncated sonucu
malformed girdiyi gizlemez.

Bu dar dilim WP-06.05'in bounded history tail kanit boslugunu daraltir;
gercek issue/Confluence okuma, canli provider ve tam proje degisiklik baglantisi
halen aciktir.

### 10.26 15 Ekim 2026 polyglot method kind ayrimi

JavaScript/TypeScript/TSX/Java polyglot graph extractor'i sinif icindeki
fonksiyon bildirimlerini parent containment kanitiyla `method` olarak etiketler;
top-level bildirimler `function` olarak kalir. Bu ayrim sembol turu ve outline
locator'inin dilden bagimsiz anlamini korur; cagri confidence kurali degismez.

Bu dar dilim WP-06.03'un structural symbol kind boslugunu daraltir; tam parser,
resolver ve cross-file graph kabul matrisi halen aciktir.

### 10.27 15 Ekim 2026 exact Jira key sinirlari

Bounded Git history sorgusu, issue anahtarinin baslangic ve bitisinde ayni
alfanumerik/alt cizgi/tire sinirini uygular. `SKYRSM-5659abc`, tireli veya alt
cizgili ek adlar exact issue eslesmesi sayilmaz; parantez ve bosluk gibi ayiricili
gercek anahtarlar korunur.

Bu dar dilim WP-06.05'in issue key false-positive kanit boslugunu daraltir;
gercek Jira/Confluence okuma ve revision baglantisi halen aciktir.

### 10.28 15 Ekim 2026 polyglot edge dedup hizlandirmasi

Polyglot graph extractor kenar tekillestirmesinde her eklemede tum kenar
listesini yeniden taramak yerine artimli kimlik kumesi kullanir. Kenar sirasi,
confidence ve edge identity degismez; buyuk dosyalarda gereksiz O(n^2) set
kurulumu kaldirilir.

Bu dar dilim WP-06.03'un yerel graph performans borcunu azaltir; generation,
resolver ve cross-file kabul matrisi halen aciktir.

### 10.29 15 Ekim 2026 Python graph edge dedup hizlandirmasi

Python AST extractor da kenar tekillestirmesinde artimli edge kimlik kumesi
kullanir. Her yeni kenarda mevcut listenin yeniden taranmasi kaldirildi;
deterministik edge sirasi, confidence ve graph identity ayni tutuldu.

Bu dar dilim WP-06.03'un Python graph performans borcunu azaltir; generation,
resolver ve cross-file kabul matrisi halen aciktir.

### 10.30 15 Ekim 2026 graph source link kapisi

Code graph source plan'i kok, dosya ve ara dizin yol bilesenlerinde symlink veya
Windows reparse point gorurse kaynagi izlemez; linkli girdiyi manifest disinda
tutar, linkli kokte ise fail-closed durur. Normal kaynak dosyalarinin digest ve
plan sirasi degismez.

Bu dar dilim WP-04.01/06.03'un graph source ownership ve path guvenligi
boslugunu daraltir; tam discovery metadata esitlemesi ve kabul matrisi aciktir.

### 10.31 15 Ekim 2026 graph apply link drift kapisi

Graph build plan'i olustuktan sonra kaynak dosyasi symlink veya reparse point'e
donusturulursa apply asamasinda yeniden denetlenir ve digest okunmadan
`ValidationFailed` ile durur. Boylece plan zamanindaki guvenli path kaniti,
uygulama zamanindaki kaynak degisimiyle gecersizlesmez.

Bu dar dilim WP-04.06/06.03'un plan/apply source ownership boslugunu daraltir;
tam discovery metadata esitlemesi ve kabul matrisi aciktir.

### 10.32 15 Ekim 2026 graph source read failure tipi

Graph plan/apply kaynak dosyasi silinmis, okunamaz veya yarista kaybolmus ise
ham isletim sistemi hatasi disari sizmaz; kaynak kaniti uretilmeden
`ValidationFailed` ile durur. Link/reparse ve digest drift kapilariyla ayni
fail-closed akis korunur.

Bu dar dilim WP-04.06/06.03'un kaynak okuma hata siniflamasini daraltir;
tam discovery metadata esitlemesi ve kabul matrisi aciktir.

### 10.33 15 Ekim 2026 graph deny dizin casefold kapisi

Graph source discovery, `.git`, `.venv`, `__pycache__`, `.mypy_cache` ve
`.pytest_cache` deny dizinlerini casefold ederek Windows/POSIX adlandirma
farkinda da izlemeye almaz. Kaynak manifesti yalniz izinli authored dosyalari
icerir; digest ve plan sirasi korunur.

Bu dar dilim WP-04.01/06.03'un deny-tree metadata boslugunu daraltir; tam
discovery metadata esitlemesi ve kabul matrisi aciktir.

### 10.34 15 Ekim 2026 PL/SQL nested block siniri

Polyglot PL/SQL declaration locator'i maskelenmis `BEGIN`, `IF`, `LOOP`, `CASE`
ve `END` tokenlarini dengeler. Ic `END IF` veya `END LOOP` prosedur govdesini
erken kapatmaz; sonrasindaki local call dogru procedure symbol'ine baglanir.

Bu dar dilim WP-06.03'un PL/SQL structural owner ve locator boslugunu daraltir;
tam PL/SQL grammar/resolver ve cross-file kabul matrisi aciktir.

### 10.35 15 Ekim 2026 sistem deny casefold kapisi

Yerlesik kaynak tarama deny listesi, platformlarin buyuk-kucuk harf davranisi
farkli olsa da `.GIT`, `.VENV`, `__PYCACHE__`, `.ENV` ve buyuk uzantili credential
dosyalarini ayni sekilde icerik disinda birakir. Ornek ortam dosyalari icin
yerlesik negation kurallari case-insensitive olarak korunur; kullanici
`.gitignore` ve `.zekamignore` kurallari onceki case-sensitive davranisini surdurur.

Bu dar dilim WP-04.01'in deny-tree metadata/eligibility guvenlik boslugunu
daraltir; tam discovery metadata esitlemesi ve kabul matrisi aciktir.

### 10.36 15 Ekim 2026 graph deny-tree traversal kapisi

Code graph source discovery, `Path.rglob` ile once tum agaci bellege almak
yerine isim sirali `os.scandir` kuyrugu kullanir. `.git`, `.venv`, cache ve
Next.js generated agaclari kuyruga alinmadan budanir; symlink/reparse girdileri
izlenmez, erisim hatali girdiler fail-closed disarida kalir. Plan manifesti
hala deterministik sirali relative path ve digest kaydini uretir.

Bu dar dilim WP-06.03'un graph discovery bellek/traversal performans ve deny-tree
guvenlik boslugunu daraltir; tam source limitleri, parser/resolver ve kabul
matrisi aciktir.

### 10.37 15 Ekim 2026 metadata probe fail-closed kapisi

Source discovery ve code graph link/reparse probe'lari metadata erisim hatasini
normal dosya gibi kabul etmez. Silinmis girisler yarista kaybolma siniflamasina
birakilir; permission veya diger `OSError` durumlari unsafe kabul edilerek
izlenmez ve icerik okunmaz. Boylece metadata probe hatasi source ownership
veya path guvenligi icin sahte olumlu kanit uretemez.

Bu dar dilim WP-04.01/04.02 ve WP-06.03'un fail-closed path metadata boslugunu
daraltir; tam native race ve kabul matrisi aciktir.

### 10.38 15 Ekim 2026 DOCX XML agac limitleri

DOCX Expat on taramasi DTD/entity ve external entity reddinin yaninda element
sayisi ile ağaç derinligini de bounded olarak denetler. Limit asimi
ElementTree kurulmadan once `PolicyViolation` ile durur; parser profili bu iki
limiti acikca tasir ve gecerli belge locator davranisi degismez.

Bu dar dilim WP-04.05'in DOCX XML bellek/derinlik riskini daraltir; PDF,
diger XML yuzeyleri, ZIP merkezi dizin ve tam kabul matrisi aciktir.

### 10.39 15 Ekim 2026 sistem deny negation kapisi

Kaynak discovery matcher'i kullanici `.gitignore` ve `.zekamignore`
kurallarini once uygular, yerlesik sistem deny listesini en sonda uygular.
Boylece `!.git/` veya benzeri bir negation, `.git`, dependency, cache ve
credential deny agacini icerik okumaya acamaz; metadata-only ve eligibility
karari korunur.

Bu dar dilim WP-04.01/04.04'un sistem deny override boslugunu daraltir; tam
discovery metadata esitlemesi ve kabul matrisi aciktir.

### 10.40 15 Ekim 2026 Git stdout bounded kapisi

Salt-okunur Git adapter'i stdout'u `subprocess.run` ile sinirsiz bellege
toplamak yerine akista bounded reader ile okur. Cikti limiti veya 30 saniyelik
komut suresi asilirsa process tree sonlandirilir ve `GitCommandError` uretilir;
basarisiz Git sonucu ile UTF-8 redaksiyonlu basarili cikti sozlesmesi korunur.

Bu dar dilim WP-06.05'in bounded history/diff kaynak okuyucusu bellek ve sure
sinirini daraltir; gercek Jira/Confluence baglantisi ve canli kabul aciktir.

### 10.41 15 Ekim 2026 bilgi dosyasi audit traversal kapisi

Knowledge file-plane audit'i Markdown gozlem limiti asildiginda yalniz icteki
dosya listesini degil, kalan dizin kuyrugunu ve sonraki audit root'larini da
hemen durdurur. `audit-limit-exceeded` tek bounded kesit kaniti olarak korunur;
buyuyen bilgi agacinda gereksiz traversal ve kaynak tuketimi yapilmaz.

Bu dar dilim WP-04.04'un bilgi dosyasi audit performans ve kaynak siniri
boslugunu daraltir; tam native dosya sistemi yarisi ve kabul matrisi aciktir.

### 10.42 15 Ekim 2026 knowledge link probe fail-closed kapisi

Knowledge file-plane auditor'inin link/reparse metadata probe'u kayip girisleri
yarista kaybolma olarak, permission veya diger metadata `OSError` durumlarini
unsafe olarak siniflar. Probe hatasi normal dosya izlenimi vermez; icerik ve
CAS audit'i icin sahte olumlu guvenlik kaniti uretilmez.

Bu dar dilim WP-04.04'un bilgi dosyasi ownership ve path guvenligi boslugunu
daraltir; native ACL/race kabul matrisi aciktir.

### 10.43 15 Ekim 2026 bounded CAS audit kapisi

Knowledge file-plane CAS audit'i `rglob` ile derin agaci bellekte toplamayi
birakir. Beklenen iki seviyeli `prefix/object` yapisi `scandir` akisi ile
incelenir; derin veya link/reparse girdileri tekrar izlenmeden unsafe olarak
raporlanir ve toplam gozlem sayisi bounded limitte kesilir.

Bu dar dilim WP-04.04'un CAS orphan/unsafe traversal kaynak tuketimi boslugunu
daraltir; tam native dosya sistemi yarisi ve kabul matrisi aciktir.

### 10.44 15 Ekim 2026 worktree diff bounded kapisi

Bound source worktree diff kaniti, dogrudan `subprocess.run` stdout toplamak
yerine ortak salt-okunur Git adapter'inin bounded reader, timeout ve process
cleanup akisindan gecer. `CompletedProcess` uyumluluk yuzeyi korunur; diff ve
degisen yol kaniti ayni stdout limitiyle uretilir.

Bu dar dilim WP-06.05'in worktree kanit toplama bellek/sure boslugunu daraltir;
tam native Git/provider kabul matrisi aciktir.

### 10.45 15 Ekim 2026 backup kaynak traversal kapisi

Local backup source census'i tree `rglob` sonucunu tamamen topladiktan sonra
dosya sayisini kontrol etmez. `scandir` akisi symlink/reparse girdilerini
izlemeden ve `MAX_FILES` asilir asılmaz durarak kaynak listesini olusturur;
sidecar filtreleri ile final siralama korunur.

Bu dar dilim WP-11.03'un backup kaynak bellek ve traversal siniri boslugunu
daraltir; restore/native dosya sistemi kabul matrisi aciktir.

### 10.46 15 Ekim 2026 local CAS iterator kapisi

Local content-addressed store `iter_objects` yalniz sabit `sha256/prefix/shard`
yerlesimini `scandir` ile okur; content dosyalari digest sirasinda akar, link
ve reparse girdileri fail-closed reddedilir. Directory ve nesne sayisi
limitleri tam agac `rglob` belleklestirmesini engeller.

Bu dar dilim WP-11.03'un yerel CAS dogrulama kaynak tuketimi boslugunu
daraltir; tam native filesystem yarisi ve restore kabul matrisi aciktir.

### 10.47 15 Ekim 2026 skill package traversal kapisi

Skill package directory import'u tum `rglob` listesini bellekte kurmaz. Link,
reparse ve special file girdileri fail-closed reddedilir; `__pycache__`/bytecode
atlanir, dosya ve directory limitleri okuma sirasinda uygulanir. Dosya boyutu
ve toplam paket boyutu okunmadan once bounded olarak denetlenir.

Bu dar dilim WP-10.02'nin skill package kaynak tuketimi ve path guvenligi
boslugunu daraltir; native client projection kabul matrisi aciktir.

### 10.48 15 Ekim 2026 managed projection digest kapisi

Managed skill projection ownership readback'i hedef agaci `rglob` ile
sinirsiz toplamadan bounded `scandir` akisi kullanir. Link/reparse ve special
file girdileri reddedilir; dosya boyutu ve toplam dosya sayisi digest listesine
alinmadan once sinirlanir.

Bu dar dilim WP-10.02'nin client projection drift kontrolu kaynak tuketimi
boslugunu daraltir; native client ve watcher kabul matrisi aciktir.

### 10.49 15 Ekim 2026 Obsidian generation traversal kapisi

Obsidian generation publish ve live validation akislari manifestteki bounded
dosya sayisini asabilecek bir agaci `rglob` ile tamamen toplamadan, ortak
`scandir` akisi ile inceler. Link/reparse ve special file reddedilir; dosya ve
dizin limitleri tarama sirasinda uygulanir, expected path karsilastirmasi ayni
sekilde korunur.

Bu dar dilim WP-11.03'un Obsidian projection kaynak tuketimi ve path guvenligi
boslugunu daraltir; native watcher/restore kabul matrisi aciktir.

### 10.50 15 Ekim 2026 project RAG Git freshness kapisi

Project RAG Git source identity ve freshness sorgulari `subprocess.run` ile
sinirsiz stdout toplamak yerine bounded salt-okunur Git adapter'ini kullanir.
HEAD/status kimligi, dirty path parse'i ve directory fallback korunur; output
limiti veya timeout durumunda kaynak `unknown` ya da kontrollu validation olarak
kalir ve mevcutlik iddiasi uydurulmaz.

Bu dar dilim WP-03/06'nin RAG source freshness bellek ve sure boslugunu daraltir;
canli provider ve native repository kabul matrisi aciktir.

### 10.51 15 Ekim 2026 Git security list output kapisi

Git security index, worktree ve history listelemeleri ortak bounded salt-okunur
Git adapter'ine baglanir; `rev-list` read-only allowlist'e alinmistir. Buyuyen
history/list stdout'u 8 MiB ve timeout sinirlarindan gecer, asimda security
scan basari kaniti uretmeden typed validation hatasi verir.

Bu dar dilim WP-06.05'in security scan history/list bellek ve sure boslugunu
daraltir; cat-file batch input/output ve canli repository kabul matrisi aciktir.

### 10.52 15 Ekim 2026 Git security batch output kapisi

Git security `cat-file` batch-check ve batch akislari da bounded reader, 30
saniye timeout ve process-tree cleanup kullanir. Kimlik ve blob istekleri
parcalanir; her batch stdout 64 MiB sinirini asarsa secret taramasi eksik
basari uretmeden validation hatasi verir. Ham icerik rapor veya kanonik kayda
alinmaz.

Bu dar dilim WP-06.05'in batch blob okuma bellek/sure boslugunu daraltir;
buyuk repository icin chunked scan ve canli kabul matrisi aciktir.

### 10.53 15 Ekim 2026 Backup bundle census kapisi

Backup kaynak agaci ve bundle/staging dogrulama yuruyusleri bounded directory ve
file sayaclariyla ilerler; symlink, reparse point ve ozel dosya girdileri fail
closed reddedilir. Bundle manifest disi dosya veya dizinleri eksik basariyla
gecmez; limit asimi staging yayini olmadan typed validation hatasi uretir.

Bu dar dilim WP-11.03/11.04'un backup traversal bellek/sure boslugunu daraltir;
gercek cihaz restore donguleri ve scheduler kabul matrisi halen aciktir.

### 10.54 15 Ekim 2026 Paket kaynak census kapisi

Paket source/config/schema/model censusleri bounded `scandir` ile dosya ve
dizin sayisini sinirlar; symlink/reparse ve ozel dosya girdileri reddedilir.
Tek dosya okumalari 16 MiB ile sinirlidir ve limit asimi manifest veya digest
uretmeden typed validation hatasina doner.

Bu dar dilim WP-12.01 release source census bellek ve traversal boslugunu
daraltir; temiz wheel kurulumu ve tam istemci kabul matrisi halen aciktir.

### 10.55 15 Ekim 2026 RAG freshness dosya kapisi

Git freshness sinyali degisen indexed dosyayi kaynak discovery ile ayni 2 MiB
siniri ve parca kontrollu okuma ile inceler. Dosya siniri veya okuma yarisi
gibi durumlarda freshness `unknown` kalir; stale kaynak current diye sunulmaz.

Bu dar dilim WP-03/06 freshness bellek boslugunu daraltir; canli provider ve
native repository kabul matrisi halen aciktir.

### 10.56 15 Ekim 2026 Fresh bootstrap metadata kapisi

Fresh bootstrap receipt, lock, stage marker, layout ve config digest okumalari
4 MiB metadata siniri ve bounded stream digest kullanir. Oversized veya yarista
buyuyen metadata typed configuration hatasiyla reddedilir; yarim staging yayinlanmaz.

Bu dar dilim WP-00/11 fresh home recovery bellek boslugunu daraltir; gercek cihaz
restore donguleri ve native scheduler kabul matrisi halen aciktir.

### 10.57 15 Ekim 2026 Knowledge note audit census kapisi

Knowledge note audit'i `os.walk` yerine bounded `scandir` kuyrugu kullanir;
dosya, dizin ve toplam girdi limitleri asildiginda audit-limit-exceeded uretilir.
Symlink/reparse ve ozel girdiler unsafe-note-path olarak fail-closed raporlanir;
limit asiminda kalan agac sessizce complete sayilmaz.

Bu dar dilim WP-04.04 bilgi dosyasi audit traversal bellek/sure boslugunu
daraltir; native dosya sistemi yarisi ve tam kabul matrisi halen aciktir.

### 10.58 15 Ekim 2026 OpenCode lifecycle spool kapisi

OpenCode lifecycle event ve ACK spool listeleri bounded dosya sayisi, regular
file/reparse kontrolu ve event 256 KiB / ACK 64 KiB okuma sinirlariyla taranir.
Oversized veya bozuk event quarantine edilebilir; spool kapasitesi asilirsa
resume sessizce eksik veri uretmeden typed validation hatasi verir.

Bu dar dilim WP-02/07 lifecycle resume bellek ve retention boslugunu daraltir;
gercek native OpenCode host kabul matrisi halen aciktir.

### 10.59 15 Ekim 2026 Client integration secret scan kapisi

Native client integration secret assignment taramasi regular-file/reparse
kontrolu, 4 MiB bounded okuma ve oversized/unreadable girdilerde fail-closed
sonuc kullanir. Secret degeri rapora veya kanonik kayda yazilmaz.

Bu dar dilim WP-02/08 client integration guvenlik bellek boslugunu daraltir;
gercek Claude/Gemini host kabul matrisi halen aciktir.

### 10.60 15 Ekim 2026 Client integration digest kapisi

Native client integration config digest ve geri-okuma kimligi de ayni 4 MiB
bounded regular-file okuyucusunu kullanir. Oversized, unreadable veya reparse
config policy hatasiyla durur; rollback digest'i eksik veriden uretilmez.

Bu dar dilim WP-02/07 integration rollback bellek ve drift boslugunu daraltir;
gercek native multi-client kabul matrisi halen aciktir.

### 10.61 15 Ekim 2026 MCP native config okuma kapisi

MCP native JSON, receipt ve digest akislari 2 MiB bounded regular-file
okuyucusu ve reparse kontrolu kullanir. Oversized, unreadable veya linkli
config typed policy hatasiyla durur; eksik okuma digest veya efekt kaniti sayilmaz.

Bu dar dilim WP-02/07 MCP config bellek ve drift boslugunu daraltir; gercek
native multi-client kabul matrisi halen aciktir.

### 10.62 15 Ekim 2026 Client integration journal ve receipt kapisi

Client integration pending journal, sync receipt ve rollback receipt JSON
okumalari ortak 4 MiB bounded regular-file okuyucusundan gecer. Oversized,
linkli, ozel veya okunamayan dosya recovery/rollback basarisi uretmeden typed
policy hatasi ya da invalid journal sinifina doner.

Bu dar dilim WP-02/07 crash recovery ve rollback kanitlarinin bellek boslugunu
daraltir; gercek native multi-client kabul matrisi halen aciktir.

### 10.63 15 Ekim 2026 MCP Codex snapshot kapisi

MCP Codex TOML render ile plan/apply ve rollback sanal snapshot okumalari da
2 MiB bounded native config okuyucusunu kullanir. Oversized veya reparse config
render, plan ya da rollback kaniti uretmeden policy hatasiyla durur.

Bu dar dilim WP-02/07 MCP istemci snapshot bellek boslugunu daraltir; gercek
native multi-client kabul matrisi halen aciktir.

### 10.64 15 Ekim 2026 Knowledge Windows stream kapisi

Knowledge note Windows okuma yolu `Path.read_bytes` yerine bounded stream ile
en fazla izinli byte artisi kadar okur. Dosya yarista buyurse veya limit asarsa
icerik tamamlanmis gibi kabul edilmez ve mevcut fail-closed LayoutError korunur.

Bu dar dilim WP-04.04 bilgi note kaynak tuketimi boslugunu daraltir; native ACL
ve dosya sistemi yarisi kabul matrisi halen aciktir.

### 10.65 15 Ekim 2026 Code graph kaynak stream kapisi

Code graph plan/apply kaynak okuyucusu dosyayi tamamen `read_bytes` ile
toplamadan `MAX_GRAPH_SOURCE_BYTES + 1` bounded stream okur. Oversized kaynak
parser veya graph kaniti uretmeden typed validation hatasiyla durur; plan/apply
link ve digest drift kapilari korunur.

Bu dar dilim WP-04.01/06.03 graph kaynak bellek boslugunu daraltir; tam parser,
resolver ve native repository kabul matrisi halen aciktir.

### 10.66 15 Ekim 2026 Oracle config stream kapisi

Oracle datasource YAML'i guvenli path boyut kontrolunden sonra da
`MAX_CONFIG_BYTES + 1` bounded stream ile okunur. Dosya yarista buyurse veya
limit asarsa credential parse edilmeden typed configuration hatasiyla durur;
secret degerleri yine process memory disina cikmaz.

Bu dar dilim WP-04.05 Oracle config kaynak tuketimi boslugunu daraltir; gercek
Oracle provider, metadata ve release kabul matrisi halen aciktir.

### 10.67 15 Ekim 2026 Portable benchmark stream kapisi

Portable benchmark config ve dataset okuyucusu stat sonrasinda da
`maximum + 1` bounded stream kullanir. Dosya yarista buyurse secret taramasi
ve task parse edilmeden policy hatasiyla durur; provider cagrisi ve yabanci kod
calistirma sozlesmesi degismez.

Bu dar dilim WP-08/12 benchmark kaynak tuketimi boslugunu daraltir; gercek model
ve release gate kabul matrisi halen aciktir.

### 10.68 15 Ekim 2026 Benchmark catalog resource kapisi

Benchmark catalog resource dosyalari `read_bytes` yerine 1 MiB limit+1 bounded
stream ile okunur. Resource stat sonrasinda buyurse veya limit asarsa digest ve
task kaniti uretilmeden typed policy hatasi verilir; symlink ve path ownership
kontrolleri korunur.

Bu dar dilim WP-08/12 catalog kaynak tuketimi boslugunu daraltir; gercek model
ve release gate kabul matrisi halen aciktir.

### 10.69 15 Ekim 2026 Client instruction stream kapisi

Native client instruction ve managed migration okuyucusu 4 MiB bounded stream
kullanir. Oversized veya yarista buyuyen kullanici dosyasi parse, migration ya
da rollback metni olarak kabul edilmeden ConfigurationError ile durur; mevcut
user-text ownership ve exact readback kapilari korunur.

Bu dar dilim WP-02/07 instruction migration kaynak tuketimi boslugunu daraltir;
gercek native multi-client kabul matrisi halen aciktir.

### 10.70 15 Ekim 2026 Client hook stream kapisi

Native lifecycle hook JSON plan ve apply readback akislari 4 MiB bounded stream
okuyucusunu kullanir. Oversized veya yarista buyuyen hook config strict JSON
parse edilmeden ConfigurationError ile durur; managed hook ownership ve atomic
write kapilari korunur.

Bu dar dilim WP-02/07 hook migration kaynak tuketimi boslugunu daraltir; gercek
native multi-client kabul matrisi halen aciktir.

### 10.82 15 Ekim 2026 OpenCode campaign fixture stream kapisi

OpenCode remote fixture ve campaign scope okumalari ortak 4 MiB bounded
regular-file stream helper kullanir. Link/reparse, UTF-8, growth veya oversized
scope/fixture typed ValidationFailed ile durur; remote-safe public fixture ve
provider payload digest baglari korunur, canli cagri sonucu uydurulmaz.

Bu dilim WP-08 campaign girdisi kaynak tuketimi boslugunu daraltir; authorization,
provider ve native host qualification kanitlari ayri kabul edilir.

### 10.81 15 Ekim 2026 Capability registry stream kapisi

Capability benchmark registry ve task fixture dosyalari 4 MiB bounded
regular-file stream ile okunur; fixture digest hesaplama da ayni siniri kullanir.
Link/reparse, UTF-8, growth veya oversized artifact typed ValidationFailed ile
fail-closed olur; hidden evaluator ve model qualification iddialari uretilmez.

Bu dilim WP-08 capability benchmark girdisi kaynak tuketimi boslugunu daraltir;
canli model/provider ve native host acceptance kapilari halen aciktir.

### 10.80 15 Ekim 2026 Benchmark fixture stream kapisi

Model benchmark fixture registry ve local fixture artifact okumalari 4 MiB
bounded regular-file stream kullanir. Link/reparse, UTF-8, growth veya oversized
fixture typed ValidationFailed ile durur; allow-root ve content digest baglari
korunur, fixture sonucu canli model qualification sayilmaz.

Bu dilim WP-08 benchmark girdisi kaynak tuketimi boslugunu daraltir; provider,
model ve native host qualification kabulleri halen ayridir.

### 10.79 15 Ekim 2026 Project RAG runtime stream kapisi

Project RAG state, pending evidence, source binding ve query/CAS recovery
dosyalari 16 MiB bounded regular-file stream ile okunur. Link/reparse, UTF-8,
growth ve oversized runtime JSON hatalari PolicyViolation ile fail-closed olur;
generation/source/tree binding ve pending CAS recovery zinciri korunur.

Bu dilim WP-05/WP-07 RAG devam ve crash recovery kaynak tuketimi boslugunu
daraltir; canli provider kalite ve native host kabul kaniti bu dilimin disindadir.

### 10.78 15 Ekim 2026 Backup stream copy kapisi

Local backup normal dosya kopyasi chunked stream ile yapilir; tek dosya ve toplam
backup boyutu sinirlari kopya sirasinda uygulanir. Manifest dogrulamasi 16 MiB
bounded stream kullanir. Kaynak census kimlik ve entry/directory drift kapilari
korunur; staging hata durumunda yayinlanmaz.

Bu dilim WP-11 backup kaynak tuketimi ve atomik yayin guvencesini guclendirir;
gercek cihaz restore ve canli scheduler kabul matrisi halen aciktir.

### 10.77 15 Ekim 2026 Evolution metadata stream kapisi

Evolution package manifest, scope transition journal ve resume admission baglari
bounded 1 MiB regular-file stream kullanir. Link/reparse, UTF-8, growth veya
oversized metadata typed ValidationFailed ile durur; package/source authority
digest ve tek-kayit journal kontrolleri korunur.

Bu dilim WP-07 recovery ve WP-08 admission kanitlarinda kaynak tuketimi
boslugunu daraltir; native scheduler/model kabul kaniti bu dilimin disindadir.

### 10.76 15 Ekim 2026 OpenCode spool owner stream kapisi

OpenCode legacy candidate owner, drain lock ve event spool belgeleri 256 KiB
bounded regular-file stream ile okunur. Oversized, link/reparse veya malformed
icerik stale candidate cleanup ve drain akisini fail-closed/quarantine sonucuna
tasir; raw delete ve authority iddiasi uretilmez.

Bu dilim WP-02/07 OpenCode spool recovery kaynak tuketimi boslugunu daraltir;
gercek native lifecycle host matrisi halen aciktir.

### 10.75 15 Ekim 2026 OpenCode embedding stream kapisi

OpenCode embedding config okuyucusu statik boyut oncesi kontrolu ile birlikte
bounded `max_bytes + 1` stream kullanir. Dosya okuma sirasinda buyurse veya
kimligi/digest boyutu uyusmazsa provider metadata kaniti uretmeden typed config
hatasi verir; secret degeri dosyadan okunmaz.

Bu dar dilim WP-05 provider config kaynak tuketimi ve fail-closed kabulunu
guclendirir; canli provider/model qualification kaniti bu dilimin disindadir.

### 10.74 15 Ekim 2026 Bootstrap ve home metadata stream kapisi

Fresh bootstrap legacy config ve ZEKAM_HOME layout metadata okumasi bounded
regular-file stream ile yapilir. Link/reparse, UTF-8, boyut veya growth hatasi
bootstrap/layout kaniti uretmeden typed hata ve unreadable issue olarak raporlanir;
legacy PostgreSQL tespitinin salt-okunur siniri korunur.

Bu dilim WP-02 baslangic ve WP-07 recovery metadata kaynak tuketimi boslugunu
daraltir; native host kabul ve diger acik WP kapilari halen surer.

### 10.73 15 Ekim 2026 Authority ve config stream kapisi

Aktif gorev authority/projection, user config ve instruction context olcumu
dosyalari bounded regular-file stream ile okunur. Boyut buyumesi, link/reparse,
UTF-8 veya dosya kimligi hatasi yetki/projection kaniti uretmeden typed hata ya
da bos/partial sonuc olarak sonlanir; mevcut duplicate-key ve digest denetimleri
korunur.

Bu dilim WP-02 authority/config girisi ve WP-07 devam baglarindaki kaynak tuketimi
boslugunu daraltir; native host ve tam acceptance kapilari halen aciktir.

### 10.72 15 Ekim 2026 Client integration native stream kapisi

Client integration status, cleanup, hook/config envanteri ve rollback yedekleri
native dosyalari 4 MiB bounded regular-file helper ile okur. Eksik dosya secret
tarama icin bos kabul edilir; link, reparse, okunamayan veya oversized artifact
ownership kaniti uretmeden conflict/fail-closed durumuna gecer. Native yedek ve
restore okumalari da ayni siniri kullanir.

Bu dar dilim WP-02 istemci entegrasyonu ve WP-07 recovery yuzeyindeki kaynak
tuketimi boslugunu daraltir; gercek native host kabul matrisi halen aciktir.

### 10.71 15 Ekim 2026 OpenCode bootstrap stream kapisi

OpenCode agent, plugin ve global config bootstrap okumalari 4 MiB bounded
regular-file stream kullanir. Oversized, linkli veya okunamayan native artifact
plan/receipt kaniti uretmeden ConfigurationError ile durur; managed ownership ve
atomic write kapilari korunur.

Bu dar dilim WP-02/07 OpenCode bootstrap kaynak tuketimi boslugunu daraltir;
gercek native host kabul matrisi halen aciktir.

### 10.83 15 Ekim 2026 kaynak guvenligi ve provider fixture stream kapisi

Git worktree secret taramasi, provider contract registry/audio fixture ve
technology bake-off evidence dosyalari bounded regular-file stream kullanir.
Link/reparse, non-regular, UTF-8, growth veya oversized girdiler fail-closed
siniflandirilir; audio fixture icin 25 MiB ust siniri korunur.

Bu dilim WP-01/WP-02 kaynak guvenligi ve WP-08 provider fixture kaynak tuketimi
bosluklarini daraltir; canli provider ve native host kabul kaniti bu dilimin disindadir.

### 10.84 15 Ekim 2026 policy ve catalog YAML stream kapisi

Policy, routing, model registry, provider binding, Jira mapping, persistence
config ve benchmark catalog YAML okumalari ortak bounded regular-file UTF-8
stream yardimcisini kullanir. Link/reparse, non-regular, growth, UTF-8 veya
64 KiB/4 MiB sinirini asan girdiler ilgili policy/config hatasiyla fail-closed
siniflandirilir.

Bu dilim WP-01/WP-02/WP-08 yapilandirma ve routing kaynak tuketimi bosluklarini
daraltir; canli provider, Jira ve native host kabul kaniti bu dilimin disindadir.

### 10.85 15 Ekim 2026 lifecycle ve cache artifact stream kapisi

Client instruction repository context, chaos config, Codex/Claude lifecycle
evidence, doctor OpenCode config, GitHub radar cache ve macOS SessionStart plan
okumalari bounded regular-file stream kullaniyor. Radar blob/meta/etag cache'i,
provider ledger database digest'i ve precompaction plan readback'i oversized,
link/reparse, UTF-8 veya growth durumlarinda fail-closed kalir.

Bu dilim WP-02 client lifecycle, WP-07 recovery ve WP-08 radar/provider cache
kaynak tuketimi bosluklarini daraltir; canli native host ve provider acceptance
kaniti bu dilimin disindadir.

### 10.86 15 Ekim 2026 analytics ve Obsidian projection stream kapisi

Local analytics segment/manifest/current pointer/receipt/report readback ve
Obsidian stable vault marker, generation manifest/receipt/pointer ve projection
dosyalari bounded regular-file stream kullanir. Segment 2 MiB, projection 1 MiB,
metadata ve raporlar kendi ust sinirlariyla okunur; link/reparse, growth veya
oversized dosya digest ve JSON dogrulamasinda fail-closed kalir.

Bu dilim WP-05/WP-07 analytics ve Obsidian recovery kaynak tuketimi bosluklarini
daraltir; DuckDB/native writer kabul kaniti bu dilimin disindadir.

### 10.87 15 Ekim 2026 CLI ve continuity artifact stream kapisi

Capability manifest, backup manifest, knowledge ingest, work spec, radar plan,
workspace resume RAG state, native dirty-file digest ve measured-loop executable
ve config okumalari bounded regular-file stream kullanir. Belge icerigi domain
siniri, manifest ve planlar kendi ust sinirlariyla okunur; link/reparse, growth,
UTF-8 veya oversized girdiler typed hata ile fail-closed kalir.

Bu dilim WP-02/WP-05/WP-07 CLI ve continuity kaynak tuketimi bosluklarini
daraltir; provider/native acceptance kaniti bu dilimin disindadir.

### 10.88 15 Ekim 2026 package ve benchmark source stream kapisi

OpenCode lifecycle instance, native benchmark source bundle, package acceptance
manifest/policy, managed skill projection, source discovery ignore dosyalari,
Tesseract/PDFium license artifact ve skill package source dosyalari bounded
stream ile okunur. Uygun dosya ust sinirlari korunur; link/reparse, growth,
oversized veya okunamayan girdiler ilgili kabul/policy hatasiyla fail-closed kalir.

Bu dilim WP-01/WP-02/WP-04/WP-08 paket, benchmark ve source discovery kaynak
tuketimi bosluklarini daraltir; canli model/provider kabul kaniti bu dilimin disindadir.

### 10.89 15 Ekim 2026 altyapi ve test artifact stream kapisi

Local CAS nesne ve metadata, Infinity BGE revision, local benchmark artifact,
operational backup yayin, research report projection, loop patch ve Maven/JaCoCo
ciktilari bounded regular-file stream kullanir. Test patch workspace, protocol
artifact kontrolu, sandbox commit mesaji ve Jira referans yenileme akislari da
boyut, link/reparse, UTF-8 ve growth kosullarinda fail-closed kalir.

Bu dilim WP-02/WP-04/WP-05/WP-07/WP-08/WP-09/WP-11 altyapi ve test artifact
kaynak tuketimi bosluklarini daraltir; final test kosusu, canli provider ve native
host kabul kaniti ayri kapanis kapilari olarak korunur.

### 10.90 15 Ekim 2026 yardimci script artifact stream kapisi

Runtime reachability, mutation gate, benchmark/schema/package manifest ureticileri,
Git kimlik hook'u, package evidence/smoke ve paket dogrulama scriptleri bounded
regular-file stream yardimcisini kullanir. Source, config, checksum, manifest,
commit mesaji ve dagitim artifact boyutlari sinirlanir; link/reparse, growth,
oversized veya strict decode hatalari kanit uretmeden durur.

Bu dilim WP-01/WP-02/WP-08/WP-09 release ve kalite scriptlerinde kaynak tuketimi
bosluklarini daraltir; final kalite/test kosusu ve gercek paket/native kabul kaniti
ayri kapanis kapilari olarak korunur.

### 10.91 15 Ekim 2026 final regression stream kapisi

Final suite regresyonlarinda analytics oversized kaynaklar typed digest policy
hatasiyla quarantine edilir, backup tek dosya ve toplam boyut limitlerini ayri
uygular, OpenCode config okumasinda bounded kimlik/growth kontrolu korunur.
Bu duzeltmeler Windows kosusunda gorulen uc failure'i giderdi; hedefli tekrar
kosusu `3 passed` sonucu verdi.

### 10.92 15 Ekim 2026 tam suite kapanis kaniti

Son kod degisikliklerinden sonra Windows/Python 3.13 tam pytest kosusu
`6920 passed, 1403 skipped, 3 warnings, 0 failed` sonucu ile tamamlandi.
Ruff tum `src/zekam` ve `scripts` agacinda temiz, paket manifest ve App Server
schema check cikislari sifir, `scripts/paket_dogrula.py` sonucu `passed` ve
`errors=0` oldu. Skip'ler bu makinede bulunmayan native client, POSIX ACL/symlink,
gercek Akilli Kasa, OCR, local BGE ve acik remote provider kosullaridir; basari
olarak sayilmaz.

### 10.93 15 Ekim 2026 gercek polyglot graph kapanis kaniti

Yeni `extractor_profile_digest=sha256:a0463d18d77486b1cf8c3a8af5c6c54635c44a1de241454e9dbd6b3869cbbab3`
ile C:\innova\projeler altindaki uc gercek kaynak koku provider/remote cagri
olmadan bounded plan/apply akisi ile tarandi. `sky-spring-ui` revision
`8fe07cb64f1090e30470f32984da28a58c7a5f49` icin 308 dosya, 3.181 sembol,
18.716 edge, 0 hata ve generation
`sha256:edb33cbcfd8be9473dfbce5aa7c84c8773811d74b5d39550eea3f1b5b29dcd27`;
`sky-microservis` revision `d075de6f951775776c133fe854753aa14767c6cc` icin
1.486 dosya, 10.287 sembol, 57.091 edge, 0 hata ve generation
`sha256:dfe4114346cb882953e9c7913c9cc4cf0f92fcf8e457513ed6e94f77230434d2`;
`gpu-fusion` revision `485a544bc87754f3d4c7181ec291d6389798ace5` icin
2.112 dosya, 12.164 sembol, 83.098 edge, 0 hata ve generation
`sha256:d22aed57b667c852668ef25f69c4ba22b0d27cfbb3f25599f55938123ae6dcfe`.
Her store integrity `passed`, ayni plan ikinci kez uygulandiginda generation
digest'i degismedi. Graph eligibility disinda birakilan `node_modules` ve
Maven `target` agaclari kaynak metadata politikasindan ayridir; kaynak kokleri
degistirilmedi.

### 10.94 15 Ekim 2026 gercek hedef Maven baseline bulgusu

`C:\innova\projeler\ai-test-engineering\java-support` icin kaynak degisikligi
yapmadan once offline `mvn -o -B clean test` kosuldu. Taze derleme sonrasi 16 testten
15'i gecti, `JavaMetadataScannerCoverageTest.recordKindRaporlanir` testi beklenen
1 record sembolune karsi 0 dondu ve Maven cikis kodu 1 ile sonlandi. Static POM
incelemesinde JaCoCo eklentisi bulunmadi; bu nedenle coverage basarisi iddia edilmedi.
Hedef projenin kaynaklari degistirilmedi, sonucu Zekam WP-09 kabulune basari olarak
tasinmadi. Sonraki kabul icin test-only sinir, gercek hedef sahipligi ve bagimsiz
verifier kosullari korunmalidir.

### 10.10 14 Ekim 2026 cok modullu Maven/JaCoCo kaniti

WP-09 icin `tests/integration/test_real_maven_unit_test_fixture.py` icine gecici, test-only bir
Maven reactor fixture'i eklendi. Fixture parent POM, `util` ve `service` modullerinden olusuyor;
`service` hedefi gercek Maven `-pl service -am` akisi ile bagimlisi `util`i birlikte calistiriyor.
Her moduldaki Surefire ve JaCoCo ciktilari kosu baslangicindan sonra toplanip binding'e digest
olarak yaziliyor; `jacoco-xml:util`, `jacoco-xml:service`, `jacoco-exec:util` ve
`jacoco-exec:service` etiketleri birlikte dogrulaniyor. Line coverage gercek rapordan measured,
branch olmayan iki sinif ise acik `not-applicable` olarak kalıyor.

Kanıt: Java `21.0.11` / Maven `3.9.16` ile `tests/integration/test_real_maven_unit_test_fixture.py`:
`3 passed`. Fixture ve Maven target'i `tmp_path` altinda uretildi; repo'daki kullaniciya ait
untracked `tests/fixtures/unit_test_engineering_java/target/` stage edilmedi.

Bu adim WP-09.01/02/05'in multi-module ve per-module rapor baglama boslugunu daraltir; gerçek
hedef proje, bağımsız kalite verifier'i, kullanici hedefi/branch matrisi ve release kapilari
halen aciktir.

`next_safe_action`: test-only fixture kanitini gercek hedef Java projesi kabulüyle karistirma;
fresh Surefire/JaCoCo, test tree ve toolchain fingerprint'i ayni kanonik raporda koru.

## 11. Karar Kapıları, Alt Ajanlar Ve Kalıcı Çalışma

### 11.1 Araştırmada Sonuçlandırılacak Kararlar

| ID | Karar | Gereken Kanıt / Varsayılan |
|---|---|---|
| D-01 | Klasör etkinleşmesi ve fiziksel veri yerleşimi | Workspace-local başlangıç; home taşınacaksa mevcut ayrım + backup/restore kararı |
| D-02 | Shell allow ile güvenilir push sınırı | Native schema, fiili guard testleri; sınırsız shell ile sağlanamayan garanti açık |
| D-03 | Kaynak/belge/parser ve artımlı indeksleme seçimi | Baseline + Graft/alternatif kıyas; source-only politika belge alımını bozamaz |
| D-04 | Host-neutral dispatch ve bağımsız verifier | Gerçek child/host capability; zorunlu ikinci orchestrator gerekçesiz kurulmaz |
| D-05 | Schema/state geçişi | Exact additive migration, version fingerprint, user rows ve rollback |
| D-06 | Canlı test matrisi, model ve bütçe | Gerçek kurulu client/provider scope; limits; demo/full qualification ayrımı |
| D-07 | Memory/skill/evolution aktivasyon sınırı | Kullanıcının kabul ettiği yetki + bağımsız evaluation + rollback |
| D-08 | Doğal dilde yetenek tamlığı ve eşdeğerlik | 31 CAP için mevcut/yeniden tasarlanmış yol; silent omission veya fake ready yok |

Teknik seçimler kapsamı değiştirmiyorsa araştırma bulgularıyla karar verilebilir.
Kaynak uygulaması, global ayar, fiziksel veri taşıma veya riskli politika gevşetme için
önceki onaylar genişletilmez. Çatışmaları uzun liste hâlinde tekrar tekrar sormak yerine
WP-01 sonunda önerilen seçenek, gerekçe, etki ve gerekli onayı tek karar paketinde sun.

### 11.2 Alt Ajan Ve Context Yönetimi

Kaynak denetimi, indeks/graph araştırması, native istemci/izin araştırması ve kalite/
süreklilik incelemesi bağımsız read-only kollara ayrılabilir. Her kola sadece ilgili
REQ/CAP/WP, bounded kaynak kesiti, output schema ve bütçe ver. Aynı dosyaya iki writer
atma. Ana ajan yalnız kısa bulgu, çelişki, evidence ve karar referanslarını birleştirir.

Gerçek subagent aracı olmayan ortamda mevcut read/search araçlarıyla sırayla ilerle;
sahte “üç bağımsız ajan inceledi” sonucu yazma. Bağımsız verifier zorunlu kabul kapısı
çalıştırılamadıysa o kapı açık kalır. Araç çağrılarını paralelleştirmek tek başına
bağımsız ajan yürütmek değildir.

### 11.3 Checkpoint Ve Devam

Her anlamlı teslim sonrası task/source/config/plan kimliği, tamamlanan WP adımı,
mevcut diff, test raw sonuçları, artifact digests, açık blocker, kullanılan araç/model
ve `next_safe_action` kaydedilir. Runtime Work/checkpoint varsa mevcut operational
SQLite ve CAS authority'si kullanılır. Araştırma sırasında runtime yazma izni yoksa
repo dışı kanıt paketi tutulur; ona gerçek Work/claim/receipt kimliği uydurulmaz.

Session kapanınca başka model sadece bu dosya + kalıcı kanıt/checkpoint ile devam
edebilmeli. Tüm mimari paketi ve geçmişin tamamını her seferinde context'e yükleme.
Yeni HEAD veya dosya drift'i varsa yalnız etkilenen karar/kanıtı tekrar doğrula.
Bitmiş research veya test, model değişti diye sebepsiz yere baştan çalıştırılmaz.

## 12. Deliverable Ve Gerçek Tamamlanma Kapıları

### 12.1 Araştırma Sonu — GATE-R

- [x] WP-00 baseline ve tüm 31 CAP/23 GAP için gerçek kaynak/koşu durumu var.
- [x] Graft ve uygun alternatifler lisans/sürüm/kod/ölçüm sınırıyla karşılaştırıldı.
- [x] D-01..D-08 kararları; uygulamayı bloke eden gerçek kararlar ayrı ve sınırlı.
- [x] Her WP'nin exact dosya/adım/imza/schema/migration/test/rollback haritası tamam.
- [x] `acceptance-targets.json` ölçülebilir, uygulama öncesi sabit ve bütçeli.
- [x] Araştırma raporu, kaynak/araç kayıtları ve güncel tek `AKTIF_GOREV.md` teslim edildi.
- [x] Kaynak uygulama onayı alındı; WP-02..12 uygulaması açıldı.

**GATE-R geçmesi Zekam'ın düzelmiş veya ürünün tamamlanmış olduğu anlamına gelmez.**

### 12.2 Uygulama Sonu — GATE-I

- [ ] Uygulama onayı ve exact scope/plan gerçek kayıtta var; WP-02..12 kodla tamam.
- [ ] Onaylı tüm partial/scaffold kapsamı çalışan dikey akışa dönüştü veya açık kabul
  kararıyla eşdeğer yola bağlandı; yalnız etiket değişmedi.
- [ ] Yeni native startup/shell/push politikası gerçek host capability'siyle doğrulandı.
- [ ] Şema/claim/receipt/context/cache/projection kuralları testle korunuyor.
- [ ] Kullanıcı dosyaları, başarılı eski testler ve önceki aktif işler kaybolmadı.
- [ ] Son kod değişikliğinden sonra zorunlu regresyon, package/reachability ve güvenlik
  kapıları tekrar çalıştı; kaynak/test denetimi güncel subject'e bağlı.

### 12.3 Kullanıcı Ve Release Kabulü — GATE-U

- [ ] Doğal dil senaryoları ve varyantları, kabul edilen gerçek client/model/OS kapsamıyla geçti.
- [ ] Cold/delta/unchanged/query performansı ve retrieval/citation/graph kalite hedefleri geçti.
- [ ] Gerçek yerel test fixture'ında Maven/JaCoCo ve test-only koruma sınırı geçti.
- [ ] Kesinti→başka model/client→devam, backup→restore ve rollback gerçek kanıtla geçti.
- [ ] Canlı kaynak/model gerektiren kritik testler yetkili ortamda tamam; eksikler gizlenmedi.
- [ ] Mimari tek kaynak güncellendi; üretim geçmişi/araç kullanımı/kimlikler korunuyor.
- [ ] Final raporu `completed`, `blocked`, `not-run`, bilinen risk ve kullanıcı kararlarını ayırıyor.

Zorunlu hedefi karşılamayan durum `partial/blocked`, başarılı uygun kapsam ise
`verified-for-tested-scope` olarak raporlanır. Tüm kapsam tamamlanmadıysa **“iş bitti”**
denmez. Kapalı ağ/olmayan credential testi önlüyorsa hayali doğrulama yerine açık engel
bırakılır. Engellenmemiş bağımsız geliştirmeler buna rağmen tamamlanır.

### 12.4 Üretilecek Belgeler Ve Teknik Kanıtlar

Geçici/runtime araştırma çıktıları onaylı repo dışı artifact alanında tutulur. Aşağıdaki
isimler araştırma/kanıt belgeleridir; ikinci bir aktif görev authority'si değildir:

- Baseline, capability/gap ve source-runtime haritası.
- Araştırma bulguları, açık kaynak kıyasları, kararlar ve araç kullanım provenance'ı.
- Exact implementation-map, acceptance-targets, source/schema/config migration ve rollback planı.
- WP bazında diff, test/benchmark raw sonuçları, claim/receipt/checkpoint ve review referansları.
- Güncel mimari referans: Markdown, HTML, ZIP, Mermaid ve gerekiyorsa yerel Miro/GitBook exportları.
- Final completion report + değişen dosyalar + commit listesi + kalan blocker + kullanıcı doğal dil deneme rehberi.

Yeni görev/devir dosyasının adı her durumda **`AKTIF_GOREV.md`** kalır. İçeriği kullanıcıya
mesaj içinde tekrar yapıştırma; kısa durum ve dosya bağlantısıyla teslim et.

## 13. Git, Kaynak Koruma Ve Rollback Ayrıntısı

Commit öncesi staged/untracked/dirty ayrımı ve tam diff incelenir. Onaylı dosyalar tek
tek stage edilir; `git add .`, toplu stash, `reset --hard`, kullanıcı değişikliğini temizleme
veya mevcut commit'leri sebepsiz rewrite yoktur. Repo-yerel kimlik gerekirse kullanıcıdan
bilinen gerçek kayıt kullanılır; global Git ayarı değiştirilmez.

Commit örneği, sadece ASCII olmalıdır:

```text
iyilestirme: proje bilgi guncellemesini artimli hale getir

Neden:
Degismeyen kaynaklarin tekrar embed edilmesini onlemek.

Degisiklikler:
Icerik ve profil kimligine bagli cache ile nesil yayinlamasini birlestir.
Kaynak secimi ve belge isleme yollarini ayir.

Dogrulama:
Gercekten calistirilan testleri, sonuclari ve kanit referanslarini yaz.
Calistirilmayan canli kabulleri acikca belirt.

Risk ve geri alma:
Etkilenen sema/profil degisimini ve dogrulanmis geri alma yolunu belirt.
```

Örneğin “gerçekten çalıştırılan” alanları sahte test adı/sayısıyla doldurulmaz.
Push onayı local commit'ten ayrı tutulur; onay hedef remote/branch ve commit subject'ine
bağlanır. Scope değişince eski yayın onayı kullanılmaz. GitHub API veya alt süreçle
uzak branch güncellemesi de “push yapmadım” diyerek bu sınırdan kaçırılamaz.

Veri dönüşümünde online backup, exact migration fingerprint ve yeni hedef restore
kanıtı olmadan eski kaydı tahrip etme. Zekam-owned eski dosyanın marker+digest+receipt
sahipliği kanıtlanmadan cleanup yapma; önce karantina/rollback. Desteklenmeyen cleanup
ve belirsiz içerik korunur.

## 14. V2 Açıklarının Kayıpsız Devri

V2'deki GAP kayıtları aşağıda başlangıç backlog'u olarak korunur. Bunlar bu görevde
henüz kapanmadı. Bazıları ürün eksikliği, bazıları yalnız önceki incelemenin kanıt/kapsam
sınırıdır; ikisini aynı hata sayma. Kapsam dışı yayın/kredi gibi kayıtlar release ürün
hatası diye sunulmaz; ama kendiliğinden çözülmüş de yazılmaz.

| GAP | Başlık | Sahip Paket / İşlem |
|---|---|---|
| GAP-01 | Tarihsel coordinator ile native-first kural çelişkisi | WP-00/02/08; D-01/D-04 |
| GAP-02 | Yetkinlik kataloğu kapsam/güncellik eksikliği | WP-00/12 |
| GAP-03 | Research named-agent bağımlılığı | WP-08 |
| GAP-04 | ask ile doğal dil yanıtı beklentisi | WP-03/05 |
| GAP-05 | Work public yaşam döngüsü eksikleri | WP-07 |
| GAP-06 | 15 Ekim model geçişi | WP-01/08; tarihli model duyurusu canlı config ile yeniden doğrulanacak |
| GAP-07 | Çoklu CLI/model uçtan uca kabulü | WP-02/08/12 |
| GAP-08 | Skill active ile canlı davranış kalitesi farkı | WP-10 |
| GAP-09 | Tam fiziksel şema yeniden üretimi | WP-00/07/12 |
| GAP-10 | Atlassian MCP güvenlik paritesi | WP-06 |
| GAP-11 | Resume sağlık özeti ve eksik referanslar | WP-07 |
| GAP-12 | Evolution yetkinliği ile cihaz yetkisi karıştırılması | WP-10/11 |
| GAP-13 | Mock benchmark ve gerçek model kabulü | WP-08/12 |
| GAP-14 | Depo içeriği ile wheel runtime kapsamı | WP-00/12 |
| GAP-15 | Bu çalışmada ürün testleri yürütülmedi | WP-00/12; eski test yokluğu geçmiş gerçektir |
| GAP-16 | Tüm kaynak dosyalarının algoritmik envanteri | WP-00/12 |
| GAP-17 | Arayüzde taşınabilirlik ile tam host bağımsızlığı | WP-02/08 |
| GAP-18 | Boşlukları varsayımla doldurma riski | WP-00/01/12 |
| GAP-19 | Araç Hazırlığı İle İzin Ayrımı | WP-01/02 |
| GAP-20 | Haricî Yayın Henüz Yapılmadı | WP-12 yerel export; remote yayın bu görevin otomatik onayı değil |
| GAP-21 | Tam Kaynak Arşivi Ve Ürün Kabulü | WP-00/12 |
| GAP-22 | Dış Kaynakların Byte Düzeyinde Sabitlenmesi | WP-01 |
| GAP-23 | Firecrawl Kredi Engeli | WP-01; güncel araç/kredi readback, uygun alternatif kaydı |

## 15. Kaynaklar, Değişiklik Kaydı Ve Başlangıç Durumu

### 15.1 Bu Görev Dosyasının Kaynak Bağları

- [Zekam kaynak baseline'ı](https://github.com/mehmet-karacan/zekam/tree/b29e2c9e89a400176d722a430af4fcebc7a270cc) —
  `main` ref'i hazırlıkta GitHub bağlantısından okundu; bunun tam ağaç yeniden analizi olmadığı kaydedildi.
- [AGENTS.md](https://github.com/mehmet-karacan/zekam/blob/b29e2c9e89a400176d722a430af4fcebc7a270cc/AGENTS.md) —
  yerel kaynak, yetki ve native sınırları; blob `9658f00b85a979a31ea902cffbed284e66f60942`.
- [Eski aktif görev](https://github.com/mehmet-karacan/zekam/blob/b29e2c9e89a400176d722a430af4fcebc7a270cc/AKTIF_GOREV.md) —
  yalnız başlangıç/devir bölümü okundu; tarihsel görevin uygulama yetkisi bu dosyanın araştırma aşamasına taşınmaz.
- [ActiveTaskContract parser](https://github.com/mehmet-karacan/zekam/blob/b29e2c9e89a400176d722a430af4fcebc7a270cc/src/zekam/application/active_task_contract.py)
  — 1–255 satır kesiti; header alanları ve durum/kimlik/digest kuralları;
  blob `947b7989fabc3731185e09040bd21bdcb5c16ae9`.
- Kullanıcının bu sohbet içindeki yedi somut isteği, bütün 31 yetkinliği inceleme ve
  kısmi yetkinlikleri tamamlama talebi, 27 maddelik araştırma planı ve son görev onayı.
- Bölüm 2'de hash'leri verilen gerçek yerel v0.5/v2/ZIP girdileri. Geniş dış araştırma
  bu hazırlıkta yapılmadı; Graft/HF/Exa/Firecrawl/Context7 karşılaştırmaları WP-01'de yapılacak.

### 15.2 Değişiklik Günlüğü

| Sürüm | Tarih | Değişiklik |
|---|---|---|
| 0.1–0.5 | 2026-10-12 | İlk talep defteri, yedi gereksinim, kullanıcı doğal dil örnekleri ve 31 CAP karar listesi; tam geçmiş Ek A'da |
| 1.0 | 2026-10-12 | Tek ana task kimliği ve parser-uyumlu header; araştırma onayı, ayrı uygulama kapısı; 27 plan maddesi, 31 CAP, 23 GAP, 13 WP, 40 doğal dil senaryosu ve test/rollback/kanıt sözleşmeleri |
| 1.1 | 2026-10-12 | WP-00 metadata/kaynak envanteri, 31 CAP ve 23 GAP durum eşlemesi, dar test baseline ve geçiş risk haritası repo dışı kanıta işlendi; canlı kabul ve WP-01 açık |
| 1.2 | 2026-10-12 | WP-01 Graft/Aider/Serena ve model karşılaştırması, D-01..08, 13 WP uygulama haritası ve 27 kabul hedefi tamamlandı; kaynak uygulama onayı bekleniyor |

### 15.3 Şimdiki Gerçek Durum

| Alan | Durum |
|---|---|
| Görev dosyası ve girdi manifesti | Aktif; projection ve paket manifesti doğrulandı |
| Önceki yedi talep | Korundu; Ek A'da tam metin |
| Geniş repo/açık kaynak araştırması | WP-00/01 GATE-R tamamlandı; repo dışı araştırma paketi `C:\Users\mkaracan\Downloads\zekam-arastirma-2026-10-12\research-findings.json` (`sha256:6d22aaf483e03e53b70f2e00e54c4f51d4b6183f77f03194d87961b0250970c8`) |
| Araştırma başlatma onayı | Var; WP-00/01 ile devam |
| Exact source uygulama onayı | Bu sohbetin 2026-10-12 kullanıcı kararıyla verildi; WP-02..12 uygulaması açıldı |
| Zekam kaynak değişikliği / ürün kabul testleri | Uygulama kaynak değişikliği yok; yalnız dar baseline testleri çalıştı, canlı ürün kabulü yok |
| Ajan izinleri / global ayarlar / runtime kullanıcı verisi | Değiştirilmedi |
| Commit / push / Miro-GitBook yayın | Görev devri `fcc2101`, WP-00 `06720d2`, WP-01 `d5bb350` commit+push; sonraki WP aşamaları için de commit+push onaylı |

**Sonraki adım:** HEAD/kaynak sürüm drift'ini denetleyerek WP-02'den başla;
`implementation-map.json` ve `acceptance-targets.json` kabul kapılarını uygula.

---

## Ek A — v0.5 Talep Defterinin Değiştirilmemiş Tarihsel Kopyası

**Sadece izlenebilirlik içindir; yürütme talimatı değildir.** Aşağıdaki eski durdurma,
onay ve durum ifadeleri Bölüm 0/15.3'teki güncel aşamayı geçersiz kılmaz. Bu eki normal
başlangıç veya her subagent context'ine yükleme. Gereksinim detayı/karar geçmişi aranırken
ilgili kesiti aç. Eski dosyanın tam SHA-256 değeri:
`cde119a8b4ee1d9d8ece297e0981588b48802a3cf0b6515db6ae4b4eb05b7967`.

<!-- HISTORICAL_V05_BEGIN -->
```text
# Zekam — İyileştirme Talepleri Ve Uygulama Promptu

**Dosya adı:** `AKTIF_GOREV.md`  
**Belge sürümü:** `0.5`  
**Başlangıç tarihi:** 12 Ekim 2026  
**Durum:** `PLANLAMA — ARAŞTIRMA VE UYGULAMA DURDURULDU; YENİ "BAŞLA" TALİMATI BEKLENİYOR`  
**Hedef repository:** https://github.com/mehmet-karacan/zekam  
**Başlangıç referansı:** `ZEKAM_MIMARI_REFERANSI.md` v2.0 ve mimari paket v2  
**Kaynak kod başlangıç revizyonu:** `b29e2c9e89a400176d722a430af4fcebc7a270cc` (her incelemede güncellik yeniden doğrulanacak)

> **Önemli:** Bu belge, kullanıcının adım adım ileteceği istekleri mühendislik görevlerine
> dönüştürmek için hazırlanmış *çalışma taslağıdır*. Repository'deki mevcut
> `AKTIF_GOREV.md` dosyasının yerine geçmez. Tek başına kaynak kod değiştirme,
> ajan çalıştırma, provider çağrısı, entegrasyon kurma, commit veya push yetkisi vermez.
> Depodaki kanonik aktif göreve aktarım ayrıca açıkça onaylanmalıdır.
> **Güncel kullanıcı kararı (12 Ekim 2026):** Yalnızca bu belge güncellenir.
> Kullanıcı yeniden açıkça **"başla"** demeden repository/harici kaynak araştırması,
> ekli arşivin içerik incelemesi, performans testi, ajan çalıştırma veya uygulama yapılamaz.

## 1. Amaç

Zekam'ın özgün hedeflerini, tarihsel yeteneklerini, mevcut uygulamasını ve kullanıcı
deneyimini birlikte değerlendirerek çalışmayan, eksik kalan veya mimari değişiklik
sırasında kaybedilmiş işlevleri kanıta dayanarak onarmak. Gerektiğinde daha doğru
tasarımlarla yeniden geliştirmek; ancak kullanıcıya ait proje dosyalarını, mevcut
çalışan özellikleri ve doğrulanmış kayıtları korumak.

Bu dosya, **kullanıcının bundan sonra göndereceği maddelerle artımlı olarak
güncellenecek tek çalışma promptu** olacaktır. Eski maddeler sessizce silinmez;
revizyon, gerekçe ve karar geçmişi korunur.

## 2. Çalışma Sözleşmesi

1. **Önce istek anlaşılır:** Kullanıcının doğal dille yazdığı madde teknik ve açık
   bir görev tanımına dönüştürülür; kullanıcının kastı değiştirilmez.
2. **Araştırma/kanıt (yalnız yeni "başla" talimatından sonra):** İlgili kod yolu,
   test, konfigürasyon, commit geçmişi ve mimari kayıtlar incelenir. Dış kaynak
   gerekiyorsa ayrıca kapsamı belirtilir. Planlama süresinde yalnız istek
   kaydı ve prompt düzenlemesi yapılır.
3. **Mevcut ile hedef ayrılır:** `mevcut`, `tarihsel`, `bozuk`, `kısmi`, `hedef`,
   `öneri` ve `doğrulanmadı` etiketleri birbirine karıştırılmaz.
4. **Çözüm tasarlanır:** Etkilenen modüller, veri/arayüz sözleşmeleri, davranış,
   güvenlik sınırı, test stratejisi, rollback ve kabul kriterleri yazılır.
5. **Onay kapısı:** Araştırma yalnız yeni "başla" komutuyla başlar; uygulama
   ayrıca onaylanmadan repository'ye yazılmaz,
   Git commit/push yapılmaz, harici projelerde veya hesaplarda değişiklik yapılmaz.
   İzin verilen araştırma, değişiklik yetkisi anlamına gelmez.
6. **Uygulama ve gerçek doğrulama:** Onaylanan en küçük kapsamdaki görev yapılır;
   test/CI/kanıt sonuçları kaydedilir. Test çalıştırılmadıysa açıkça yazılır.
7. **Revizyon:** Sonuç, karar ve bir sonraki güvenli adım bu dosyada güncellenir;
   mimari referansı etkilemişse ona yapılacak güncelleme ayrı işaretlenir.

## 3. Her Yeni Kullanıcı Mesajı İçin Prompt Şablonu

Kullanıcının yeni mesajından aşağıdaki kayıt oluşturulur:

- **Kimlik:** `ZKM-REQ-###`
- **Orijinal kullanıcı isteği:** Anlamı bozulmadan kısa alıntı/özet.
- **Düzeltilmiş uygulama promptu:** Açık, tekil, ölçülebilir ve teknik ifade.
- **Amaç ve beklenen kullanıcı davranışı:** Ne değişecek, kullanıcının göreceği sonuç ne?
- **Kapsam / kapsam dışı:** Hangi özellikler ve dosyalar etkileniyor, neler değişmeyecek?
- **Mevcut durum ve kanıt:** Kaynak kod referansı, commit, test, log, hata üretim adımı.
- **Analiz soruları ve araştırma kaynakları:** Doğrulanması gereken varsayımlar.
- **Hedef mimari ve etki analizi:** Modüller, veriler, CLI/MCP/API, istemciler, modeller.
- **Güvenlik ve veri koruma:** Yetki, gizlilik, sahiplik, kullanıcı içeriği, rollback.
- **Kabul kriterleri:** Pozitif, negatif, entegrasyon ve regresyon senaryoları.
- **Bağımlılıklar / riskler / açık kararlar:** Karar verilmemiş konular.
- **Durum:** `taslak | araştırma onaylandı | analiz tamam | uygulama onaylandı |
  uygulanıyor | doğrulandı | engelli | iptal edildi`.
- **İzlenebilirlik:** Dosya/belge/commit/test bağlantıları ve değişiklik günlüğü.

**Prompt yazım ilkesi:** Belirsiz “iyileştir/hallet” yerine somut çıktı ve
kabul ölçütü; mimari yaklaşımı kanıtlanmadan zorunlu sayma; doküman hazırlanmasını
kodun çalıştığına dair kanıt olarak sunma.

## 4. Başlangıç İnceleme Alanları — Henüz Onaylı Uygulama İşleri Değil

Aşağıdaki başlıklar önceki mimari konuşmalarından türetilmiş inceleme alanlarıdır.
Her biri kullanıcının sonraki somut isteğine göre görevleştirilecektir.

| Alan | İncelenecek yetenek veya olası açık |
|---|---|
| Proje bağlamı | Proje kayıtları, alias, proje aileleri, UI/backend yönlendirmesi, Jira eşlemesi |
| Araştırma | Proje RAG, kaynak kanıtı, researcher/verifier, rapor ve model/istemci bağımlılıkları |
| Agentic yürütme | Eski coordinator ve alt ajanlar, native-first geçişi, rollerin gerçek yürütülebilirliği |
| Model altyapısı | Model seçimi, benchmark, sağlık, fallback ve kurum içi model değişiklikleri |
| Süreklilik | Oturumlar arası/harness'lar arası checkpoint, resume, recovery ve doğrulanabilir state |
| Bellek ve öğrenme | Hafıza, doğrulanmış dersler, skill üretimi, değerlendirme ve aktivasyon |
| Proaktif gelişim | Kaynak analizinden iyileştirme adayı çıkarma, bağımsız değerlendirme, onaylı rollout |
| Test mühendisliği | Test-only üretim, Maven/JaCoCo ölçümü, gerçek kalite ve güvenli yeniden başlatma |
| Entegrasyonlar | GitHub, Exa, Firecrawl, Context7, Miro, GitBook ve mevcut MCP registry |
| Güvenlik/operasyon | Local-first saklama, secret koruma, exact authorization, claim/receipt, CI, backup |

### Eklenti Kullanımı Hakkında Karar Çerçevesi

- **GitHub:** Canlı repository kanıtı, commit, PR, issue ve CI doğrulaması.
- **Exa:** Açık kaynak ve teknik araştırmada aday keşfi.
- **Firecrawl:** Teknik web kaynaklarını ve çok sayfalı belgeleri işleme; erişim/kredi
  sorunları varsa `kullanılamadı` olarak kaydedilir.
- **Context7:** Kullanılacak kütüphane ve sürümlerin gerçek dokümantasyonunu doğrulama.
- **Miro:** Düzenlenebilir diyagram ve ortak mimari incelemesi; harici pano oluşturma ayrı onay.
- **GitBook:** Bölümlü dokümantasyon ve sürüm yönetimi; harici alana yazma/yayımlama ayrı onay.

Bu eklentilerin **ChatGPT ortamında kullanılabilmesi**, Zekam/OpenCode/Codex üzerinde
kendiliğinden kurulu ya da kimlik doğrulaması yapılmış olduğu anlamına gelmez.
Kalıcı entegrasyon gerekiyorsa ayrı tasarım ve izin kararı açılır.

## 5. Değişmez Teknik İlkeler

- Zekam, tek bir modele, oturuma veya coding CLI istemcisine kilitlenmemelidir.
- Ürünün yerel-öncelikli (`local-first`) çalışma prensibi ve kanonik kayıtları korunur.
- Kullanıcının proje dosyaları, kaynak kodları, belgeleri, yerel veri ve geçmiş kayıtları
  izinsiz temizlenmez/taşınmaz.
- Eski ajan mekanizması yalnız eski olduğu için zorla geri getirilmez; kaybedilen
  **yeteneğin** doğru ve kanıtlı karşılığı geliştirilir.
- Salt-okunur plan, model yanıtı, retrieval sonucu veya Markdown metni yetki vermez.
- Haricî etki için gerektiğinde exact plan, authorization, claim, receipt, bağımsız
  verifier ve kesintiden kurtarma kanıtları aranır.
- Başarısız veya kısmi çalışma “tamamlandı” olarak işaretlenmez.
- Zekam'ın mevcut `AGENTS.md` kuralları ve repository'deki onaylı aktif görev durumu
  uygulama öncesinde tekrar okunur; bu taslak otomatik olarak onların yerine geçmez.

## 6. Talep Defteri

Aşağıdaki maddeler **kullanıcı tarafından tanımlanmış geliştirme gereksinimleridir**.
Bunlar henüz araştırılarak doğrulanmış veya repository üzerinde uygulanmış değildir.
Amaç, kullanıcı isteğini davranışsal testle doğrulanabilir teknik promptlara çevirmektir.
İlk maddeler birlikte bir deneyim oluşturur: **istemciden otomatik tanınma → proje
kimliğini çözme → proje ile belge/kaynak bağlama → hızlı artımlı indeksleme/sorgulama →
kanıtlı cevap → onaylı mevcut göreve devam**. Uygulama sırası analiz sonucuna göre kesinleştirilecektir.

### ZKM-REQ-001 — İstemciden Bağımsız, Klasör Kapsamlı Otomatik Başlangıç

- **Orijinal kullanıcı isteği:** "Zekam tamamen kendi klasöründe çalışacak ... herhangi
  bir istemci (codex, claude, gemini, grok, vs.) o klasörden açılınca zekam devreye
  girecek ve işler başlayacak."
- **Düzeltilmiş mühendislik promptu:** Zekam'ın kendi repository/çalışma alanı
  kökünden başlatılan destekli coding CLI istemcilerinin Zekam bağlamını ve
  onaylı aktif görev durumunu otomatik keşfetmesini sağlayan, istemciden bağımsız,
  proje-yerel bir başlangıç ve görev devam mekanizması tasarla. İstemci adına
  bağımlı hard-code edilmiş kontrol kullanma; desteklendiği kanıtlanan native
  instruction/hook/MCP/skill mekanizmalarını adapter'larla bütünleştir. Açılışta
  Zekam'ın yetenekleri, görev durumu, son güvenilir checkpoint, sonraki güvenli
  işlem ve gerekli proje bağlamı erişilebilir olsun. Geçerli kullanıcı onayına ve
  exact etki kapsamına sahip yarım kalmış işlerde otomatik devam etme davranışını
  destekle; **yalnız klasörün açılması yeni, yetkisiz yazma/ağ/model/kurulum
  etkisi başlatmasın**. Kullanıcı "işler başlayacak" ile bu sınırın ötesinde
  otomasyon kastediyorsa öncelikle yeni yetkilendirme modeli ve kapsamı için
  mimari karar üret.
- **Başarı davranışı:** Yeni destekli CLI oturumu Zekam kökünde açıldığında
  komut ezberi gerektirmeden doğru kuralları ve mevcut işi tanır; farklı CLI'a
  geçildiğinde bağlam/kaydedilmiş iş durumu kaybolmaz. Zekam dışındaki proje ve
  global kullanıcı dosyaları etkilenmez.
- **Araştırma ve uygulama kapsamı:** `AGENTS.md`, `00_BASLA.md`,
  `DEVAM_PROTOKOLU.md`, `opencode.json`, `src/zekam/application/client_*`,
  `workspace_resume`, `cross_client_resume`, `interfaces/cli/integration.py`,
  `continuity`, `mcp`, aktif/legacy ajanlar ve istemci adapter'ları. Dosya
  yollarının güncelliği çalışma sırasında doğrulanacak.
- **Mimari çatışma / açık karar:** Güncel `AGENTS.md` klasör açmanın kendi
  başına görev başlatmadığını söyler. Bu istek mevcut sözleşmeyi değiştirmeyi
  gerektirebilir; geri dönüş/güvenlik etkisi analizi yapılmadan silinmesin.
  Native istemcinin instruction dosyasını algılaması, event/hook başlatması ve
  otomatik görev çalıştırması birbirinden ayrı yetenekler olarak ölçülsün.
- **Kabul:** En az OpenCode/Codex/Claude/Gemini ile (cihazda kurulu sürümler
  üzerinden) yeni oturum, alt klasör, başka proje, global dizin, yeniden
  açma, model değiştirme, kesintiden devam ve hiç onay yokken effect üretmeme
  testleri. Grok ve başka istemciler için destek kanıtı yoksa `unsupported`/
  `qualification-pending` olarak gösterilsin; sahte uyumluluk iddiası olmasın.
- **Durum:** `taslak — araştırma ve uygulama onayı ayrı`
- **Uygulama yetkisi:** Verilmedi.

### ZKM-REQ-002 — Kaynak Kod Odaklı Hızlı Ve Artımlı Embedding

- **Orijinal kullanıcı isteği:** "Proje embed etmesi çok uzun sürüyor;
  sadece .java, .js, .tsx gibi compile edilmemişleri embed etse daha hızlı olacak."
- **Düzeltilmiş mühendislik promptu:** Proje indeksleme süresini ölçerek
  darboğazları belirle. Dense embedding iş yükünü asıl kaynak kod dosyalarına
  ve açıkça seçilmiş gerekli metin türlerine daraltan **yapılandırılabilir,
  proje/teknoloji-özel bir içerik seçim politikası** tasarla. İlk adaylar
  `.java`, `.js`, `.jsx`, `.ts`, `.tsx` olup `.py`, `.sql`, `.plsql` vb. yalnız
  proje türü ve kullanıcı politikası doğrulamasıyla kapsama alınabilir;
  oluşturulmuş `.class`, `.jar`, `.war`, `.map`, `dist`, `build` çıktıları,
  bağımlılıkların büyük ağaçları veya ikili dosyalar otomatik dense embedding'e
  gönderilmesin. Dosya uzantısı tek başına yeterli sayılmasın: içerik/format,
  kaynak-türetilmişlik ve güvenlik kontrolü uygulansın. **Tam kaynak envanteri
  ve metadata**, embedding'e alınmayan dosyalar için de ayrı tutulabilsin;
  bu, Context Vault'un tüm dosyaları ilk aşamada envanterleme kuralını bozmaz.
  Kaynak revizyonu, içerik digest'i, chunk kimliği ve embedding model/profil
  digest'ine dayalı artımlı önbellekle **değişmeyen dosyalar tekrar embed edilmesin**.
- **Ölçüm:** İlk indeksleme, artımlı yeniden indeksleme ve sorgu süresi; taranan
  dosya/byte, atlanan ve nedenleri, üretilen chunk, provider istekleri,
  cache hit/miss, token/maliyet, exact/lexical/dense kalite metriği ölçülsün.
  Mevcut gerçek proje fixture'larıyla eski-yeni karşılaştırması yapılmadan
  rastgele süre/hedef garantisi verilmesin.
- **Kalite/güvenlik:** Kaynak kod parçası sınıf/fonksiyon/satır locator'ını
  korusun; güçlü exact arama ve citation kaybolmasın. Gizli veri ve
  kullanıcı içeriği uzaktaki provider'a varsayılan olarak açıklanmasın.
  İlave dosya türlerini varsayılan olarak kapatmak, .git veya diğer içeriklerin
  **envanter kaydını** silmek anlamına gelmez.
- **İlgili inceleme alanları:** `source_discovery`, `project_rag_runtime`,
  `project_knowledge_index`, `knowledge_index`, `retrieval_service`,
  `knowledge-index` SQLite adaptörü, embedding provider/qualification cache,
  RAG testleri ve benchmark sonuçları.
- **Kabul:** Aynı commit tekrar indekslendiğinde sıfır gereksiz embedding;
  tek dosya değişince sadece etkilenen chunk'ların yeniden üretimi; yeni/silinen
  dosya, profil değişimi, kötü/binary/gizli dosya, çok modüllü repo, aynı isimli
  dosya, derlenmiş çıktının metadata kaydı ve citation doğrulama testleri.
- **Ek araştırma kapsamı (kullanıcının yeni talebi):** GitHub üzerinde
  semantik kod araması, Tree-sitter/AST tabanlı kaynak parçalama, artımlı indeks,
  içerik-adresli cache, değişiklik odaklı embedding gibi gerçek uygulamaları;
  GitBook'un içerik indeksleme/senkronizasyon yaklaşımını; Hugging Face üzerindeki
  açık kaynak embedding, reranker, dataset ve kod arama örneklerini karşılaştır.
  Gerekirse Exa, Firecrawl ve Context7 ile aynı konuyu doğrula. Bir servisin
  dokümantasyon/arama kabiliyetini Zekam'a doğrudan MCP kurulumu şartı olarak
  yorumlama. Lisans, mahremiyet, bağımlılık, hız ve kalite etkisini ölç; sağlam
  yöntemi Zekam'ın mevcut mimarisine uyarlamak üzere öner.
- **Ek dosya politikası:** Kullanıcının önerdiği `.env` ve `.properties` dosyaları
  için *local-only*, salt-okunur yapı/anahtar adı analizi seçeneğini araştır.
  `.env` içindeki secret değerleri, token, bağlantı parolası, kimlik bilgisi,
  özel URL ve hassas değerler prompt'a, log'a, vektöre, rapora veya harici
  sağlayıcıya aktarılmaz. `.properties` dosyası otomatik olarak güvenli sayılmaz:
  değer sınıflandırma/redaksiyon, kaynak doğrulama ve allowlist tasarla.
  Sadece güvenli olduğuna doğrulanan, açıkça seçilmiş semantik açıdan faydalı
  alanlar embedding için değerlendirilebilir. Kalanları envanterde metadata
  olarak tut; anahtar adları dahi hassas ise redakte edilir. Gizli dosyaların
  envanterde bulunması ile içeriklerinin okunması/embedding'i ayrıdır.
- **Kullanıcı referans arşivi:** `gpu-db-ddl-sync-master@87c867aa5ba.zip`
  (12 Ekim 2026 mesajına ekli). Bu arşiv ancak kullanıcı yeniden **"başla"**
  dedikten sonra, arşiv içeriğini çalıştırmadan veya kaynak projeyi değiştirmeden,
  listeleme/istatistik/kod örüntüsü analizi ile incelenecek. Önceki çalışmaya
  göre üstünlük ancak gerçek benchmark ve kalite ölçümüyle kabul edilecek.
- **Araştırma/uygulama izni:** Şu anda **YOK**; kullanıcı yeni "başla" demeden
  GitHub/GitBook/Hugging Face ve ekli arşiv üzerinde analiz başlatılmayacak.
- **Durum:** `taslak — ölçüm ve araştırma bekliyor`
- **Uygulama yetkisi:** Verilmedi.

### ZKM-REQ-003 — Proje Tespiti Öncelikli Ve İzole RAG Yönlendirmesi

- **Orijinal kullanıcı isteği:** "Önce gelen sorunun hangi projede olduğunu
  anlamalı ve sonrasında ise o proje ile RAG'dan direkt sorgulanmalı."
- **Düzeltilmiş mühendislik promptu:** Kullanıcı isteğini yanıtlamadan önce
  mevcut gerçek proje registry'si, proje alias'ları, proje aileleri,
  seçili CLI çalışma alanı, görev/Jira anahtarı ve yetkili oturum bağlamıyla
  **deterministik bir project resolver** üzerinden ilişkilendir. Tek proje
  güvenle seçildiyse RAG sorgusunu sadece o projenin doğru source revision ve
  hazır generation indeksinde yürüt. Proje algılama aşamasında tüm projelerin
  chunk içeriğini karşılaştırma veya gereksiz provider çağrısı yapma.
  Belirsizlikte `clarification-required`, proje yoksa `project-unavailable`,
  indeks yoksa `index-not-ready` gibi açık, kanıta dayalı durum üret; başka
  projenin RAG'ına sessiz fallback yapma. Çok projeli karşılaştırma ancak
  isteğin bunu gerektirdiği ve tüm ilgili proje bağları doğrulandığı durumda,
  açık çoklu-kapsamlı stratejiyle çalışsın.
- **Çıktı sözleşmesi:** `project_ref`, `family_ref`, `matched_terms`, `route_reason`,
  `source_revision`, `generation_digest`, `query_digest`, `citation_refs`,
  `retrieval_state`, `generation_state`, `answer_kind` alanlarının kullanılabilirliği
  incelensin; yeni alanlar sürümlü sözleşmeyle eklensin. Salt retrieval delili
  LLM yanıtı gibi sunulmasın.
- **İlgili inceleme alanları:** `config/project_families.yaml`,
  `config/jira_project_mappings.yaml`, `request_routing`, `jira_issue_routing`,
  `project_rag_runtime.resolve_question_project`, `interfaces/cli/main.py`
  (`ask`), RAG index ve project registry.
- **Kabul:** `GPU`, `SKY`, farklı alias, açık Jira issue anahtarı, aynı anda iki
  proje adı, yanlış alias çakışması, projeye ait olmayan genel soru, tanımsız
  proje, eski indeks ve yetersiz citation; tümünde yanlış projeden bilgi
  sızıntısı olmaması ve uygun tekil/çoklu retrieval stratejisinin seçilmesi.
- **Durum:** `taslak — araştırma ve uygulama onayı ayrı`
- **Uygulama yetkisi:** Verilmedi.

### ZKM-REQ-004 — Ajan Shell Yetkisi Ve Türkçe ASCII Git Politikası

- **Orijinal kullanıcı isteği:** "Tüm ajan dosyalarında shell yetkisi allow olacak;
  tüm ajan her şeyi yapabilecek. Sadece push için onay istenecek. Commit için
  Türkçe ASCII karakter ile yapılan işin detaylı kısmı olacak."
- **Düzeltilmiş mühendislik promptu:** Zekam altında kullanılan tüm destekli
  ajan/alt ajan tanımları ve istemci adapter'larının shell/terminal araçlarına
  ilişkin varsayılan yetki politikasını tek kanonik kaynaktan yönetilebilir hale
  getirmeyi tasarla. Kullanıcının hedef tercihi `shell = allow`: proje işlerini
  yürüten ajanlar shell komutları için tekrar tekrar onay istemeden çalışabilsin.
  İlgili istemcilerin (OpenCode, Codex, Claude Code, Gemini CLI ve diğer
  desteklenenlerin) izin sözdizimlerini ve gerçek davranışını uyumluluk matrisiyle
  doğrula; desteklenmeyen araç veya istemci için sahte izin/uyumluluk iddia etme.
  **Git push** öncesinde her durumda açık kullanıcı onayı zorunlu olsun.
  **Git commit** için ayrıca push onayı aranmasın; commit başlığı ve açıklama
  gövdesi, yapılan değişikliğin neyi/neden/nasıl etkilediğini açıklayan yeterli
  teknik ayrıntı içersin, **Türkçe anlamlı fakat yalnız ASCII karakterli**
  yazılsın (ör. `s`, `g`, `i`, `c`, `u`, `o`; Türkçe diakritikler kullanılmaz).
  Commit öncesi değişen dosyalar, diff ve gerekli test/doğrulama sonuçları
  kullanıcıya ve kayıt sistemine açıkça izlenebilir olsun.
- **Politika kapsamı:** Ajan promptları, agent tanımları, proje-yerel talimatlar,
  istemci izinleri, shell araçları, alt ajanlar, Git hooks/commit policy ve
  Zekam'ın yerel mutation admission sözleşmeleri. Kullanıcı yalnız **push** için
  interaktif onay istemektedir; araştırma aşamasında bunun Zekam'ın mevcut
  plan/claim/receipt ve tool permission sınırlarıyla çatışmaları envanterlenecek.
- **Kritik güvenlik kararı (inceleme bekliyor):** Shell için geniş `allow`,
  başkaca bütün izinlerin otomatik devredildiği, işletim sistemi sınırlarının
  kalktığı veya provider/secret/yıkıcı işlem korumalarının yok sayıldığı anlamına
  gelmez. Kullanıcının `tüm ajan her şeyi yapabilsin` hedefi yüksek risklidir:
  `AGENTS.md`, native-first mimari, bağımsız verifier ve mevcut fail-closed
  kontrolleriyle çelişebilir. Risklerin ve hangi işlemlerin yalnızca push
  kontrolüyle yönetilebileceğinin analizi yapılacak; uyumsuzluk kullanıcıya
  sunulmadan güvenlik sınırları otomatik kaldırılmayacak. Bu satır kullanıcının
  hedefini değiştiren bir onay değil, açık teknik karar konusudur.
- **Kabul önerileri:** Çoklu ajan shell çalıştırması; CLI bazında yetki davranışı;
  shell komutunun sessizce başka hostta farklı yetkiyle çalışmaması; commit'in
  Türkçe ASCII başlık+gövdesi ve diff/test kanıtı; `push` denemesinde açık onay
  yokken kesin ret; commit ile push'un birbirinden ayrı değerlendirilmesi;
  güvenlik/secret/araç sınırlarının değiştiği durumlarda görünür raporlama.
- **Durum:** `taslak — yalnız gereksinim kaydı; araştırma ve uygulama durduruldu`
- **Araştırma/uygulama yetkisi:** Yeni **"başla"** talimatı yok; verilmedi.

### ZKM-REQ-005 — Projeye Özgü Belge, Klasör Ve Teknik Artifact İçeri Aktarımı

- **Orijinal kullanıcı isteği:** "Modele klasör, dosya verdiğimde bunu GPU için
  güncelle dediğimde embed etmesi gerek. GPU'nun bir ODD veya GTD'si olur;
  talep, defect veya 11g export XML olabilir."
- **Düzeltilmiş mühendislik promptu:** Kullanıcı bir dosya, klasör, arşiv veya
  yetkili iş kaydı referansı verdiğinde ve "GPU için güncelle", "SKY'ye ekle",
  "bu projenin bilgisini yenile" benzeri komutlar kullandığında sistem önce
  hedef projeyi registry/alias/Jira/çalışma alanı bağlamıyla **kesin olarak
  çözümlemelidir**. Girdiyi yalnız doğrulanmış proje kapsamında ve dosya/kayıt
  türüne uygun parser ile güvenli şekilde içeri almalı; kaynak kimliğini, kullanıcı
  sahipliğini, sürümü, içerik digestini, locator'ları ve erişim sınıfını korumalıdır.
  Seçili/güvenli metin parçalarını artımlı şekilde projeye ait exact/lexical/dense
  RAG indekslerine işlemeli; mevcut indeksin çalışan nesillerini ve önceki kaynak
  sürümlerini koruyarak yeni generation'ı atomik olarak etkinleştirmelidir.
- **Girdi sınıfları:** Tek dosya; seçili klasör ve tüm alt dosyaları; çoklu dosya;
  ZIP gibi arşivler; ODD/GTD belgeleri (format, şablon ve uzantı çalışma sırasında
  teyit edilir); iş talebi ve defect dokümanları; Jira'dan yetkili okunmuş issue
  açıklaması/ekleri; ODI 11g Smart Export XML/ZIP ve ilişkili manifestler;
  düz metin, Markdown, PDF, DOCX ve kaynak kod. **Her sınıfın parser'ı ve
  güvenlik sözleşmesi ayrıdır**; tüm içerikler ham metin gibi işlenmeyecek.
- **Girdi türü ayrımı:** Kullanıcının ilettiği dosya ile gerçek proje kaynak kodu,
  kullanıcının kişisel dokümanı, Jira issue'su ve ODI exportu farklı provenance
  ve yaşam döngüsü taşır. Başka projeden otomatik içerik kopyalanmaz, kaynağı
  bulunamayan istekler atanmamış/belirsiz kalır. Kaynak belgeler üzerinde yazma,
  veri tabanı DDL yürütme veya Jira issue güncelleme **embedding/güncelleme**
  talebinden otomatik türetilmez.
- **Envanter ve embedding:** Klasör seçildiğinde bütün alt klasör/dosyaların
  metadata envanteri tutulur (gizli/.env, binary, derlenmiş ve desteklenmeyenler
  dahil; bunların içerikleri otomatik embedding'e dahil değildir).
  Erişilemeyen/şifreli/uyumsuz dosyaların nedeni kayıt altına alınır. Duyarlı
  dosyaların içeriği ve secret değerleri uzak sağlayıcıya gönderilmez. Destekli
  ODD/GTD/PDF/DOCX içeriği paragraf, başlık, tablo ve kaynak sayfa/satır
  locator'larıyla indekslenir. ODI XML için object/column/mapping/flow,
  bağlantı ve referans ilişki çıkarımı gerçek exporta göre doğrulanır; kaynak
  kod veya üretim sisteminde herhangi bir işlem çalıştırılmaz.
- **Artımlı güncelleme:** `source_kind + project_id + logical_path + version +
  content_digest + parser_version + chunk_profile + model/profile_digest`
  üzerinden değişen/new/removed/unchanged ayrılır. Yalnız etkilenen birimler
  yeniden parse/chunk/embed edilir; cache hit ve reindex nedeni raporlanır.
  Content cache gizlilik sınırlarını aşarak projeler arasında kaynak sızıntısı
  oluşturamaz. Tek dosyalı ve klasörlü güncellemeler aynı temel pipeline'ı
  paylaşır; kullanıcı yeniden aynı belgeyi gönderdiğinde gereksiz provider
  çağrısı yapılmaz.
- **Kullanıcı geri bildirimi:** `hangi-proje`, `hangi-kaynaklar`, `new/changed/
  unchanged/deleted`, `parsed`, `indexed`, `skipped-with-reason`, `source_revision`,
  `generation_digest`, `cache hits`, `provider calls`, `source evidence` ve
  `next_safe_action` alanlarıyla doğrulanabilir durum raporlanır. Parser olmayan
  dosyalar için "indekslendi" iddiası kurulmaz.
- **Araştırma soruları:** ODD/GTD şablon türlerine göre yapısal parser mümkün mü?
  PDF/DOCX tablolarının yapısı bozulmadan indekslenebilir mi? XML 11g exportundaki
  kaynak bağı ve ilişkileri hangi parser ile çıkarılır? Değişen kaynak parçası ve
  güncel Jira eki nasıl saptanır? Üretim boyutunda cache/indeks hız ve retrieval
  doğruluğu hangisini öne çıkarır? Bu sorular için henüz araştırma yapılmayacak.
- **Kabul senaryoları (öneri):** Tek ODD GPU'ya eklenir ve sadece GPU sorgusunda
  bulunur; GTD değiştirilince eski/yeni sürüm izlenir; aynı belge ikinci kez
  işlendiğinde gereksiz embedding olmaz; klasörde farklı türde dosyalar doğru
  sınıflandırılır; GPU defekti ile SKY defekti karışmaz; ODI 11g XML'den doğrulanmış
  nesne adı ve ilişki citation ile bulunur; hatalı XML/şifreli ZIP/büyük arşiv
  fail-closed olur; index yarıda kesilirse önceki nesil korunur; aynı isimli
  farklı belgeler yanlış birleştirilmez; secret içerik dışarı sızmaz.
- **Bağımlılıklar:** ZKM-REQ-002 (seçici/artımlı embedding), ZKM-REQ-003
  (proje çözümleme), kaynak envanteri, document parser, project RAG, ODI metadata,
  Jira MCP ve SQLite/CAS kanıt zinciri.
- **Durum:** `taslak — örnek/araştırma/uygulama bekliyor`
- **Araştırma/uygulama yetkisi:** Kullanıcı yeniden **"başla"** demeden yok.

### ZKM-REQ-006 — Graft Ve Açık Kaynak Kod İndeksleme Alternatiflerinin Karşılaştırılması

- **Orijinal kullanıcı isteği:** "https://github.com/trailhq/graft ... bu projede
  çok işe yarıyordu; bunun daha iyisini embed edip hızlı getirebiliriz.
  Bunları da plan listene al."
- **Düzeltilmiş mühendislik promptu:** `https://github.com/trailhq/graft`
  repository'sini ve güncel uygun açık kaynak alternatiflerini **başla izninden
  sonra** incele. Graft'ın gerçekten uyguladığı keşif, AST/Tree-sitter, sembol
  çıkarımı, kod grafiği, doküman indeksleme, retrieval, embedding önbelleği ve
  artımlı güncelleme gibi özellikleri varsayım olmaksızın kaynak kod/testlerle
  doğrula. Zekam'ın mevcut RAG/graph/research katmanlarıyla fonksiyonel ve
  performans karşılaştırması yap; gerekiyorsa Graft yerine daha kaliteli,
  hızlı ve sürdürülebilir açık kaynak bir yöntemi öner. Hazır kaynağı körlemesine
  `embed` veya vendörleme: lisans, sürüm, bağımlılık, OS, gizlilik ve maliyet
  uyumunu ayrı değerlendir.
- **Kıyas kriterleri:** Soğuk indeksleme ve tek dosya değişim süresi; yeniden
  embedding istek sayısı; 1000/10000+ dosyada bellek/disk kullanımı; exact
  sembol, call/reference relation, Türkçe teknik soru, ODD/GTD/PDF ve ODI XML
  kalitesi; hata/kesinti sonrası devam; kullanıcı verisi izolasyonu; model
  bağımsızlığı; okunur evidence/citation ve kurulum zorluğu.
- **Kaynaklar (gelecekte):** GitHub repo + commit history + README/tests,
  Hugging Face model/embedding araştırması, Context7 sürüm dokümanları,
  gerekirse Exa/Firecrawl/GitBook referansları. **Şu an doğrulanmış bir
  Graft mimari üstünlüğü veya ölçüm sonucu yoktur.** Kullanıcı sadece
  araştırma adayı olarak bildirmiştir.
- **Önerilen karar çıktısı:** `kullan`, `uyarla`, `yalnız belirli bileşeni al`,
  `referans al ama entegre etme`, `reddet` seçeneklerinden birini ölçümler
  ve gerekçeyle seç; mevcut güvenlik, proof ve proje izolasyonu bozulmasın.
- **Durum:** `taslak — araştırma bekliyor`
- **Araştırma/uygulama yetkisi:** Kullanıcı yeniden **"başla"** demeden yok.

### ZKM-REQ-007 — Komutsuz, Doğal Dil Öncelikli Evrensel İş Akışı

- **Orijinal kullanıcı isteği:** "Ben doğal dil ile iş yaptırabileyim ekstra uğraşmak istemiyorum. zekam ask filan uğraşmıyayım." Kullanıcı ayrıca Zekam'ın bütün yetkinliklerini gerçek kullanım örnekleriyle görmek, eksikleri kendisi seçip planı buna göre güncellemek istiyor.
- **Düzeltilmiş mühendislik promptu:** Desteklenen native coding istemcilerinden herhangi biri Zekam klasöründe açıldığında, kullanıcıya `zekam ask`, `zekam route preview`, `zekam research run`, `zekam project index` gibi Zekam CLI komutları ezberletmeden serbest Türkçe/doğal dil ile yetenekleri sun. Kullanıcının tek cümlesini güvenilir **intent → proje çözümleme → uygun yetenek/araç → mevcut kanıt veya gerekli indeksleme → çıktı/geri bildirim** zincirine yönlendir. CLI/MCP/programatik API alt katman araçları olarak kalabilir; insan etkileşiminde zorunlu giriş yolu olmamalıdır. İstemci/yetenek desteği gerçekte yoksa otomatik işlendi iddiasında bulunma.
- **Kullanıcı deneyimi örnekleri ve beklenen davranışlar:**
  1. "GPU'da PCK_HAKEDIS'i açıkla" → GPU projesini belirle, erişilebilir kaynakları kontrol et, proje-izole RAG ve gerekirse kod grafiği ile locator'lı açıklama üret.
  2. "Bu GTD'yi GPU bilgisine ekle/güncelle" → dosyanın türünü, kaynağını, sürümünü ve hedef projeyi belirle; güvenli yerel içerik analizi ile projeye bağla; yalnız gerekli güvenli parçaları artımlı indeksle; indekslemenin gerçekten tamamlandığını bildir.
  3. "SKY 11306 talebinde ne yapılmış?" → proje/Jira anahtarını çöz, gerçek kaydı yalnız yetkili entegrasyonla getir, uygun repository ve kaynak geçmişiyle ilişkilendir, kaynaklı raporla; erişim yoksa açıklayıcı durum döndür.
  4. "GPU'nun şu klasörünü analiz et" → tüm dosyaların metadata envanterini al, dosya türlerini ayır, source-only/allowlisted embedding uygula, teknik bağıntıları çıkar, atlananları gerekçelendir.
  5. "Bu Java sınıfının testlerini iyileştir" → doğru proje/modül ve test kapsamını çöz, mevcut testleri ölç, test-only güvenli akışı ve gerçek coverage kanıtını kullan; yetkilendirme ve güvenlik kurallarını koru.
  6. "Kaldığın yerden devam et" → kanonik Work/checkpoint/source bağını doğrula; yanlış veya değişmiş kaynağı yürütme, güvenli devam yolunu belirle.
  7. "Git'e commit at, ama push yapma" → yalnız onaylı görev/değişiklik kapsamındaki işi doğrula, ayrıntılı Türkçe ASCII mesajla commit et; push uygulama öncesi ayrıca onay iste.
- **Mimari kapsam:** Native giriş talimatları ve istemci adaptörleri; kullanıcı isteği anlayıcısı; mevcut `request_routing` ve proje ailesi/katalog; `jira_issue_routing`; RAG, code graph, Oracle/ODI parser, bilgi güncelleme, araştırma, test, model seçimi, MCP, skill, kalıcı Work ve continuity; plan/yetki/receipt. Gerektiğinde ayrı `natural-language-dispatch` tasarımı oluştur, ancak istemcinin native yeteneklerini kopyalayarak zorunlu bir ikinci ajan katmanı üretme.
- **Açık UX kuralları:** Bilinen proje/niyet için kullanıcıyı komut çalıştırmaya yönlendirme; kısa açıklama ve asıl sonucu dön. Gerçekten belirsiz iki proje, farklı kullanıcı dosyasına yazma, güvenli içerik işleme veya yetki eşiği gibi durumlarda yalnız gerekli açıklama/onayı iste. "GTD'yi projede güncelle" ifadesi, varsayılan olarak teknik bilgi indeksini güncelleme anlamında değerlendirilebilir; fiziksel belgeyi değiştirme veya kaynak kodu düzenleme yalnız açık niyetle yapılır. Hata durumunda araç komutu değil net `neden + gereken kullanıcı kararı` göster.
- **Kaynak güvenliği:** Zekam klasörüne girilmesi veya ajan Shell `allow` ayarı tek başına herhangi bir harici aktarım veya geri dönüşsüz etki için yetki sayılmaz. Önceki `push` onayı talebi korunur; modelin shell üzerinden yetki kapısını dolanmaması ayrıca analiz/test konusudur.
- **Kabul/golden test:** `zekam` komutu yazmadan en az 12 gerçek Türkçe talep türü; farklı native istemci, yeni oturum, gizli bağlam, belirsiz proje, GPU/SKY ayrımı, indeks hazır/değil, remote embedding izni yok, değişmeyen belge, Jira/MCP offline, source drift, eksik parser, secret `.env`, çalışan Work'u geri yükleme ve push-only onay negatif senaryoları. Sonuçlarda seçilen yetenek, proje kimliği, evidence, gerçek işlem durumu ve kullanıcıya gösterilen açıklama izlenebilir olsun. Gerçek kullanıcı oturumu doğrulaması olmadan `tamamlandı` denmesin.
- **Kullanıcı değerlendirme yöntemi:** Aşağıdaki **31 yetkinlik inceleme listesi** üzerinden kullanıcı `koru`, `iyileştir`, `yeniden tasarla`, `istemiyorum` kararlarını ve istediği ek iş akışını belirtebilir. Henüz görüş bildirilmemiş satırlar karara bağlanmış sayılmaz.
- **Bağımlılıklar:** ZKM-REQ-001, 002, 003, 004, 005, 006. Önce 001/003/007 ile kullanıcıdan gizlenen yönlendirme sözleşmesi, ardından ihtiyaca göre indeksleme/araştırma/test/iş yaşam döngüsü.
- **Durum:** `taslak — kullanıcı gereksinimi kaydedildi; araştırma/uygulama başla talimatını bekliyor`.
- **Uygulama yetkisi:** Verilmedi.

### V2 Mimari Paketinden Kullanıcı Tarafında Sınanacak Örnekler

Aşağıdaki örnekler taslak/golden test girdileridir; **koşuldu veya geçti
anlamına gelmez**. Beklentiler mevcut davranışla hedef davranışı karıştırmaz.
Referans paket: `ZEKAM_MIMARI_PAKETI_v2.zip`.

| Kullanıcı ifadesi | Ölçülecek sonuç | Kaynak / durum |
|---|---|---|
| `GPU 123 numaralı defecti incele` | `SKYRSM-123` anahtarı; issue varlığı ayrıca doğrulanır | ARC-09; anahtar çözümü belgeli |
| `GPU projesinde PCK_HAKEDIS ne yapıyor?` | GPU indeksinden locator'lı retrieval, başka projeye sızıntı yok | ARC-08, ARC-11; canlı kabul yok |
| `SKY ekranına ait müşteri akışını göster` | SKY ailesi ve UI repo rolü, çoklu eşleşme gerekirse açık karar | ARC-08; canlı kabul yok |
| `Bu GTD dosyasını GPU için güncelle` | Girdi türü/versiyon saptanır ve GPU knowledge generation güncellenir | ZKM-REQ-005; yeni hedef |
| `Bu klasördeki ODD belgelerini GPU'ya ekle` | Klasör tam metadata envanteri, güvenli parser, seçili içerik ve artımlı embedding | ZKM-REQ-002/005; yeni hedef |
| `Bu ODI 11g export XML'ini GPU için analiz et` | Object/flow/lineage ve citation; kaynak veri üzerinde effect yok | ZKM-REQ-005; kapsamlı parser doğrulanmamış |

### Zekam Yetkinliklerinin Kullanıcı Karar Matrisi — 31 Başlık

Bu matris mevcut **v2 mimari paketinden** çıkarılmış başlık listesidir; yeni araştırma veya canlı ürün kabulü değildir. `ready/partial/scaffold` ürünün kendi beyanları; `technical` ve `historical-*` destekleyici/dönemsel kayıtların türüdür. Kullanıcı bir satırda karar belirtince yalnız o karar ilgili istek maddesine işlenir.

| Kimlik | Yetkinlik | v2 belgesindeki sınıf | Kullanıcı kararı |
|---|---|---|---|
| CAP-01 | Native CLI Girişi | `partial` | Açık: değerlendirilmedi |
| CAP-02 | Native Test Ve Süreklilik Yardımcıları | `partial` | Açık: değerlendirilmedi |
| CAP-03 | Proje RAG Ve Alıntılar | `ready` | Açık: değerlendirilmedi |
| CAP-04 | İstek, Proje Ailesi Ve Jira Yönlendirmesi | `ready` | Açık: değerlendirilmedi |
| CAP-05 | OpenCode Sürekliliği | `ready` | Açık: değerlendirilmedi |
| CAP-06 | İş Ve Proje Kayıtları | `partial` | Açık: değerlendirilmedi |
| CAP-07 | Markdown Bilgi Yaşam Döngüsü | `ready` | Açık: değerlendirilmedi |
| CAP-08 | ODI 11g Ön Kontrol Ve Bağlama | `partial` | Açık: değerlendirilmedi |
| CAP-09 | Kanıtlı Proje Araştırması | `ready` | Açık: değerlendirilmedi |
| CAP-10 | Fikir Üretimi Ve Terfi | `scaffold` | Açık: değerlendirilmedi |
| CAP-11 | Analitik Raporlar | `partial` | Açık: değerlendirilmedi |
| CAP-12 | Model Benchmark Ve Yönlendirme Kanıtı | `partial` | Açık: değerlendirilmedi |
| CAP-13 | Unit Test Mühendisliği | `partial` | Açık: değerlendirilmedi |
| CAP-14 | Semantik Bellek | `partial` | Açık: değerlendirilmedi |
| CAP-15 | Jira Anahtarı Çözümü | `partial` | Açık: değerlendirilmedi |
| CAP-16 | Yedek Ve Kurtarma | `ready` | Açık: değerlendirilmedi |
| CAP-17 | Ölçülü Otonom Gelişim | `ready` | Açık: değerlendirilmedi |
| CAP-18 | Kişisel Skill Yaşam Döngüsü | `documented-partial` | Açık: değerlendirilmedi |
| CAP-19 | Yapısal Kod Grafiği | `documented-partial` | Açık: değerlendirilmedi |
| CAP-20 | Proje Keşfi Ve Teknoloji Profili | `technical` | Açık: değerlendirilmedi |
| CAP-21 | Yetki, Onay Ve Kanıt Kapıları | `technical` | Açık: değerlendirilmedi |
| CAP-22 | Kuyruk, Lease, Fencing Ve Outbox | `technical` | Açık: değerlendirilmedi |
| CAP-23 | Ortak MCP Registry | `technical` | Açık: değerlendirilmedi |
| CAP-24 | Jira Ve Confluence Okuma | `technical` | Açık: değerlendirilmedi |
| CAP-25 | Model Kimliği Ve Provider Taşıması | `technical` | Açık: değerlendirilmedi |
| CAP-26 | Oracle Metadata Ve PL/SQL Bağlamı | `technical` | Açık: değerlendirilmedi |
| CAP-27 | CAS, Ham Olay Ve Analitik | `technical` | Açık: değerlendirilmedi |
| CAP-28 | Kurulum, Paketleme Ve Doctor | `technical` | Açık: değerlendirilmedi |
| CAP-29 | Kaynak Sahipliği, Sandbox Ve Git | `technical` | Açık: değerlendirilmedi |
| CAP-30 | Ajan Rolleri Ve Bağımsız Doğrulama | `historical-and-retained` | Açık: değerlendirilmedi |
| CAP-31 | Ölçülü Döngü, Radar Ve Tournament Sözleşmeleri | `historical-or-contract` | Açık: değerlendirilmedi |

**Kullanıcının karar sözlüğü:** `koru / iyileştir / yeniden tasarla / istemiyorum / önce açıkla`. Açık kalan her satır `değerlendirilmedi` olarak saklanır.

### Ortak Gereksinimler Ve Bağımlılıklar

- Öncelik: ZKM-REQ-001 (başlangıç/bağlam), ZKM-REQ-003 (doğru hedef),
  ZKM-REQ-002 (indeksleme hızı), ZKM-REQ-005 (proje belgesi alımı),
  ZKM-REQ-006 (açık kaynak kıyası), ZKM-REQ-007 (doğal dil UX); gerçek performans ve dependency sonuçlarına
  göre uygulama sırası güncellenebilir.
- Değişiklikten önce ve sonra gerçek test/evidence alınır; hata ayıklama ve
  mimari araştırma, Zekam'ın eski yeteneklerini sessizce kaldırmamalıdır.
- Her madde ayrı bir davranış sözleşmesidir; birinin geçmesi diğerine kabul
  kanıtı oluşturmaz. Provider kullanım onayı, repository mutasyon onayı değildir.
- ZKM-REQ-005, kullanıcıya ait ODD/GTD, talep/defect ve ODI XML içeriklerinin
  proje-özel bilgi varlıkları olarak yönetilmesini gerektirir; yalnız uzantı
  filtresi bu gereksinimi karşılamaz.
- ZKM-REQ-006 için açık kaynak yöntemleri araştırma kararı, mevcut kodu
  kopyalama veya Zekam repositorysine entegrasyon izni değildir.
- ZKM-REQ-004 ayrı bir **yetki politikası** kararıdır; diğer maddelerin
  araştırılması bu tercihin otomatik uygulandığı anlamına gelmez.
- **Şu an planlama safhasındayız. Kullanıcı açıkça yeniden "başla" deyinceye
  kadar yalnız `AKTIF_GOREV.md` revize edilir.**
- `ZEKAM_MIMARI_PAKETI_v2.zip` içeriğindeki örnekler bu sohbet için
  salt-okunur olarak incelenmiştir; ürün testi veya dış repo kaynak
  incelemesi yapılmış sayılmaz.

## 7. Güncelleme Ve Teslim Kuralları

- Kullanıcı her yeni madde ilettiğinde, onun kapsamını koruyarak profesyonel
  bir mühendislik promptuna dönüştür ve **aynı `AKTIF_GOREV.md` dosyasını**
  yeni revizyon olarak teslim et.
- İndirilebilir dosya veriliyorsa içerik konuşmada tekrar gösterilmez; yalnız
  kısa değişiklik özeti ve indirme bağlantısı verilir.
- Önceki istek, karar, kabul kriteri ve referanslar yeni revizyonda korunur.
- Her revizyonda belge sürümü ve değişiklik günlüğü artırılır.
- Dosyanın sonraki sohbetlerde güncellenmesi için güncel kopyasının erişilebilir
  olması gerekir; bellekten dosya içeriği uydurulmaz.

## 8. Değişiklik Günlüğü

| Sürüm | Tarih | Değişiklik |
|---|---|---|
| 0.1 | 2026-10-12 | Başlangıç çalışma sözleşmesi, prompt şablonu ve inceleme alanları oluşturuldu. |
| 0.2 | 2026-10-12 | ZKM-REQ-001/002/003 teknik promptları, kabul kriterleri, bağımlılıklar ve açık kararlar eklendi. |
| 0.3 | 2026-10-12 | Embedding için GitHub/GitBook/Hugging Face ve ekli ZIP referansı, `.env`/`.properties` gizli veri kontrolleri; ZKM-REQ-004 ajan shell `allow`, push onayı ve ayrıntılı Türkçe ASCII commit isteği eklendi. Araştırma yalnız yeni `başla` sonrasında. |
| 0.4 | 2026-10-12 | ZKM-REQ-005 proje-özel ODD/GTD, talep/defect ve ODI 11g XML içeri aktarımı; ZKM-REQ-006 Graft/açık kaynak araştırma adayı; v2 paketten golden örnekler ve ortak bağımlılıklar eklendi. |
| 0.5 | 2026-10-12 | ZKM-REQ-007 doğal dil öncelikli, komutsuz kullanıcı deneyimi ve 31 yetkinlik için kullanıcı değerlendirme matrisi eklendi; araştırma/uygulama bekletildi. |
```
<!-- HISTORICAL_V05_END -->
