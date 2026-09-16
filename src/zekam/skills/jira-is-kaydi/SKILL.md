---
name: jira-is-kaydi
description: Jira iş kayıtlarını kanıta bağlı, Türkçe ve güvenli biçimde okumak; geliştirme taskı için özet ve açıklama taslağı hazırlamak; yalnız açık yazma isteğinde doğrulanmış hedefte kayıt veya yorum oluşturmak için kullanılır.
license: Proprietary
metadata:
  author: Zekam
  compatibility: Codex, OpenCode ve Claude Code Agent Skills
  lifecycle: candidate
  version: "1"
---

# Jira iş kaydı

Bu beceriyi Jira kaydı okuma, mühendislik içeriği hazırlama veya kullanıcının açıkça istediği
sınırlı Jira yazma işlemi için kullan.

1. İşlemi, içerik profilini ve çıktı biçimini ayrı belirle. `hazırla`, `taslak` ve benzeri
   ifadeler canlı yazma yetkisi vermez.
2. Kullanıcı bir Jira anahtarı verdiyse onu değiştirme. Zekam ortamında Jira ayrıntısı için önce
   `zekam jira resolve "<kullanıcının exact sorusu>" --json` çalıştır; yalnız dönen
   `issue_key` ile mevcut Jira aracını kullan. Belirsizse anahtar uydurma.
3. Kaynakları iddia bazında değerlendir. Görülmeyen dosyadan, planlanan işten veya belirsiz
   işlem sonucundan tamamlanma iddiası üretme.
4. Özet, Epic Name ve düz metin alanlarına Wiki/Markdown/ADF işaretleri koyma. Jira Wiki,
   Markdown ve ADF gövdelerini birbirine dönüştürülmüş gibi sunma.
5. Canlı yazmadan hemen önce hedef kaydı oku; yalnız kullanıcının açıkça istediği alanı veya
   yorumu değiştir. Durum, atanan, worklog, güvenlik seviyesi ve başka alanlara dokunma.
6. Timeout veya belirsiz sonuçta kör yazma tekrarı yapma; önce kaydı yeniden okuyup sonucu
   uzlaştır. Terminal readback olmadan "eklendi" veya "oluşturuldu" deme.

## Geliştirme taskı oluşturma

`task oluştur`, `Jira'ya ekle` veya eşdeğer açık bir fiil yoksa yalnız taslak üret. Oluşturma
isteğinde aşağıdaki alanları birbirinden ayrı tut:

- hedef proje ve issue type (ör. `SKYRSM` ve `Task`);
- düz metin Summary;
- Jira Wiki, Markdown veya ADF olarak açıkça seçilmiş Description;
- kullanıcı tarafından verilen veya Jira'dan doğrulanan parent/Epic, priority, labels ve
  diğer alanlar;
- Assignee belirtilmemişse varsayılan olarak isteği yapan/doğrulanmış mevcut Jira kullanıcısı;
  başka kullanıcıya atama yalnız açık kullanıcı talebiyle yapılır.

Proje, issue type veya zorunlu alan doğrulanamıyorsa değer uydurma; eksik alanı belirt ve
oluşturma yerine tamamlanabilir taslak ver. Aynı kapsam için mevcut kayıtları önce ara; açık
ilişki verilmedikçe yeni kaydı başka bir Epic'e bağlama.

Geliştirme profili açıklamasını `Amaç`, `Yapılacak Değişiklik`, `Kapsam Dışı`, `Uygulama
Notları`, `Test` ve `Kabul` başlıklarıyla kur. Planlanan işi tamamlanmış gibi yazma; kaynakta
doğrulanmayan tablo, kolon, işlem tipi, tarih veya kuralı "doğrulanacak/değerlendirilecek"
olarak işaretle.

Talep kaynaklı Epic ve task'larda önce talep kaynağı bloğunu ekle, ardından `----` ayıracı koy.
Ayraçtan sonra task'ın aşamasına göre yalnız kısa çalışma açıklaması yaz: Analiz için analiz
edileceğini, Geliştirme için geliştirileceğini, Kontrol için uygunluğun kontrol edileceğini,
Test için senaryolarla doğrulanacağını belirt. Ek kapsam, kabul kriteri, teknik detay veya
karar bölümü ancak kullanıcı isterse eklenir; karar ve ilerleme gibi sonradan oluşan bilgiler
yorum olarak tutulur.

## Defect oluşturma ve güncelleme

Kullanıcı açıkça defect açılmasını istediğinde issue type olarak Jira'da doğrulanan `Defect`
veya `Bug` türünü kullan; `Task` seçme. Defect'ler Epic'e bağlanmaz. `SKYRSM-5111` yalnızca
sabit Jira Epic standardıdır ve defect alanlarına yazılmaz.

Defect Summary düz metin ve şu biçimde olmalıdır:

```text
Defect ID: <kaynakta doğrulanan ID> - <Sistem / Ürün> - <Modül veya Adım> - <Etkilenen Nesne> <Kısa Hata Özeti>
```

Kaynakta Defect ID yoksa `Defect ID: xxx -` öneki aynen kullanılır; Jira issue key'i kaynak
Defect ID yerine geçirilmez ve sonradan kimlik uydurulmaz.

Yeni defect açıklamasında yalnız defect bilgileri, hata açıklaması ve teknik bulgular bulunur;
çözüm/kapanış alanları otomatik eklenmez. Kaynakta olmayan değerler boş bırakılır veya bölüm
çıkarılır. Çözüm bilgisi ancak kullanıcı mevcut defect güncellemesi kapsamında açıkça isterse
eklenir.

Mevcut defect güncellemesinde kayıt önce okunur; yalnızca kullanıcının belirttiği alanlar
değiştirilir, belirtilmeyen alanlar korunur. İlerleme, test veya çözüm gelişmeleri alanı
değiştirmek yerine gerektiğinde yorum olarak eklenir. Yeni kayıt veya güncelleme sonrasında
issue key, Summary ve Description readback ile doğrulanmadan başarı bildirilmez.

Canlı oluşturma öncesi hedef proje/iş tipi ve olası mükerrerleri yeniden doğrula. Yazma aracı
yalnız okuma yetenekliyse hazır taslağı göster; Jira'ya yazılmış gibi raporlama. Oluşturma
sonrası dönen issue key, Summary, Description, durum ve ilişki alanlarını okuyarak doğrula ve
yalnız doğrulanan sonucu bildir.

## Epic açıklaması

Epic genel bir iş alanını temsil eder; Description yalnız tek bir Talep veya Defect'e
indirgenmez. Summary genel sonucu, Epic Name kısa temayı, Description ise kaynakları ve
iş kapsamını birlikte anlatır. Açıklamayı Jira Wiki biçiminde hiyerarşik başlıklar, kalın
etiketler, madde listeleri ve gerektiğinde tablolarla oluştur. Renk, doğrulanmamış makro veya
okunabilirliği azaltan biçim kullanma. Güvenli Epic şablonu ve alan sırası için
`references/kurum-icerik-standardi.md` içindeki "Epic açıklama formatı" bölümünü uygula.
Talep kaynaklı Epic'lerde Description'ın en üstüne, PDF veya gönderilen belgede bulunan talep
bilgilerini "Talep kaynağı bloğu" bölümündeki panelli yapıya aktar. Kaynak etiketlerini,
değerlerini ve bölüm sırasını koru; kaynakta bulunmayan bilgi veya ek yorum ekleme. Bu bloğu
`----` ayıracından sonra gelen Epic açıklamasından ayrı tut. Ayraçtan sonra ayrıca `Epic
Açıklaması` başlığı ekleme; doğrudan profil başlıklarıyla devam et. Aynı yapı Epic altındaki
task açıklamalarında da kullanılabilir.

İçerik, başlık ve kanıt kuralları için `references/kurum-icerik-standardi.md`; ortam çözümleme
kuralları için `references/ortam-profili.md`; Jira Wiki teknik kataloğu ve güncellik durumu için
`references/JIRA_FORMAT_REFERENCE.md` dosyasını yalnız ihtiyaç olduğunda yükle.

Yetenek çağrıldığında Jira teknik referansı için önce ağsız durum denetimi yap:

```text
python scripts/refresh_jira_format_reference.py status
```

Çıktı `refresh_required: true` ise, ağ erişimi uygunsa `refresh` komutunu çalıştır. Yenileme
başarısızsa sağlam yerel kopyayı kullan ve başarısızlığı açıkça bildir. Bu kontrol canlı Jira
kaydını önbelleğe almaz ve zamanlanmış görev oluşturmaz.
