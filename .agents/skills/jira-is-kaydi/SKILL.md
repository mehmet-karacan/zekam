---
name: jira-is-kaydi
description: Jira iş kayıtlarını kanıta bağlı, Türkçe ve güvenli biçimde okumak; taslak
  açıklama, özet, Epic Name ve yorum hazırlamak; yalnız açık yazma isteğinde hedef
  alanı değiştirmek için kullanılır.
license: Proprietary
metadata:
  author: Zekam
  compatibility: Codex, OpenCode ve Claude Code Agent Skills
  lifecycle: candidate
  version: '1'
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
   uzlaştır. Terminal readback olmadan “eklendi” veya “oluşturuldu” deme.

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
