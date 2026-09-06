# GPU, SKY ve ODI domain bağlamı

## Kullanıcı tarafından doğrulanan bilgiler

- GPU ve SKY tarafında `INNOVA_ODI`, ODI 11g tarafından kaynak sistemlerden beslenen
  entegrasyon şemasıdır.
- GPU uygulamasının kullandığı uygulama şeması `GPU_USER`dır.
- SKY uygulamasının kullandığı uygulama şeması `TTBP`dir.
- GPU projesi, SKY projesinin replacement/yeni nesil karşılığıdır.
- SKY; bayi portal ve OSB kapsamındaki hakedişleri hesaplar.
- GPU; partner kapsamındaki hakedişleri hesaplar.
- İki sistemdeki tablo benzerlikleri, eski SKY yapılarıyla GPU karşılıklarını belirlemekte
  kullanılabilecek önemli bir sinyaldir.

## Doğrulama gerektiren lineage yaklaşımı

Tablo adı veya kolon benzerliği tek başına kesin eşleşme sayılmayacaktır. ODI Interface,
Scenario ve Load Plan ilişkileri; Oracle tablo/kolon metadata'sı ve uygulama kodundaki
kullanımlar birlikte değerlendirilerek aşağıdaki aday ilişkiler kanıtlanacaktır:

```text
kaynak sistem -> ODI 11g -> INNOVA_ODI -> GPU_USER -> GPU uygulama kodu
kaynak sistem -> ODI 11g -> INNOVA_ODI -> TTBP     -> SKY uygulama kodu

SKY/TTBP tablo-kolon adayı -> GPU/GPU_USER tablo-kolon karşılığı
```

Bir eşleşme ancak exact ODI nesne kimliği, SQL/table-column referansı veya iki bağımsız
kanıtla doğrulandığında kanonik lineage kenarı olacaktır. Yalnız embedding benzerliğiyle
lineage üretilmeyecektir.

## GPU kabul durumu

GPU Smart Export, `C:\innova\odi\gpu\exports\<sha256>\design\SmartExport.xml` altında
içerik adresli tutulur. Aktif indeks; GPU kaynak kodu, `GPU_USER` Oracle metadata'sı ve
sanitize ODI tasarım nesnelerini aynı generation içinde arar. İlk gerçek sorgularda
`I_ET_UDR_LT`, `I_ET_UDR_LT_MVNO`, `I_ET_UDR_LT_NONMVNO` ve hedef `INNOVA_ODI`
tabloları kanıtlı olarak bulundu. Buna karşılık `INNOVA_ODI -> GPU_USER` hakediş zincirinin
tamamı tek sorguda kanıtlanamadı; sistem bunu kesin ilişki gibi kaydetmez.

## DB_SCRIPTS ek bilgi kaynağı

`C:\Users\mkaracan\ownCloud\DB_SCRIPTS` yalnız GPU projesine aittir; SKY/TTBP kapsamına
dahil edilmez ve iki proje arasında benzerlik kaynağı olarak kullanılmaz. Dizin doğrudan
indeks kaynağı değildir. İncelemede tüm
öğelerin cloud/reparse placeholder olduğu, arşiv ve büyük CSV'lerle birlikte kişisel veri
adayı içerik taşıdığı görülmüştür. Kullanılacak defect/talep kayıtları önce yerel,
salt-okunur ve digest-bound bir snapshot'a alınmalı; arşivler karantinada açılmalı, secret
ve kişisel veri taramasından geçirilmeli, büyük CSV/XML için varsayılan metadata-only
politikası uygulanmalıdır. Güvenli çıktı ileride
`C:\innova\knowledge\supplemental\gpu-db-scripts\<snapshot>` altında GPU kapsamıyla
bağlanacaktır; ownCloud ağacının ham hali vektörlenmez.
