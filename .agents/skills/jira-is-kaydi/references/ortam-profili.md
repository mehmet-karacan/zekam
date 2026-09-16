# Zekam Jira ortam profili

Bu dosya genel içerik standardını Zekam'ın mevcut Jira çözümleme sınırına bağlar.

## Hedef çözümleme

1. Kullanıcının exact sorusunu koruyarak `zekam jira resolve "<exact soru>" --json` çalıştır.
2. Yalnız başarılı yanıttaki `issue_key` alanını Jira aracına geçir.
3. GPU sayısal görevleri `SKYRSM-<sayı>`, SKY sayısal görevleri `TLCSKY-<sayı>` olarak ancak
   resolver sonucu bunu doğruluyorsa kullan. Belirsizlikte key üretme.
4. Kullanıcının verdiği Jira anahtarı, Talep ID, Defect ID, Epic bağlantısı ve kaynak kimliği
   birbirinden ayrı alanlardır. Birini diğerinin yerine yazma.

## İşlem sınırı

- Okuma, taslak hazırlama ve doğrulama salt okunurdur.
- `hazırla`, `öner`, `taslakla`, `nasıl yazalım` ifadeleri create/comment/update çağrısı değildir.
- Canlı yazma yalnız açık fiil + kesin hedef + yeterli araç/yetki birlikte varsa yapılır.
- Önce hedef kaydı oku ve mükerrer kapsamı denetle.
- Yorum isteği yalnız yorumdur; açıklama, durum, Resolution, Epic Status, atanan, worklog veya
  Security alanını değiştirmez.
- Güvenlik alanı hatasını alanı kaldırarak aşma.
- Kısmi başarıda oluşan kayıtları sakla, eksikleri bildir ve kör tekrar yapma.

## Çıktı taşıması

- Summary ve Epic Name: düz metin.
- Jira Wiki alanı: `references/JIRA_FORMAT_REFERENCE.md` içindeki doğrulanmış güvenli alt küme.
- Markdown: yalnız kullanıcı Markdown istediğinde; Jira native biçimi olarak tanıtılmaz.
- Cloud ADF: yapılandırılmış ADF belgesi; Wiki string'i ADF gövdesine gönderilmez.

Bu profildeki örnekler yetki üretmez ve kurulum sırasında canlı Jira yazması yapılmaz.
