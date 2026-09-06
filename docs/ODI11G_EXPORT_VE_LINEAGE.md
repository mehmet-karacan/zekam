# ODI 11g export ve uçtan uca lineage

GPU ve SKY için ODI 11g tasarım nesneleri ayrı export bundle olarak alınır. Bundle'ın amacı
kaynak kod, Oracle metadata ve ODI akışını aynı proje kimliği altında birleştirmektir. Ham
ODI XML doğrudan genel XML parserına veya embedding sağlayıcısına gönderilmez.

## Kanonik güncelleme girdisi

ODI 11g için kanonik girdi **Smart Export XML** dosyasıdır. Normal Export aynı tasarım
nesnelerini içerir ancak Smart Export bağımlı model, datastore, kolon, key ve logical
schema nesnelerini de taşıdığı için lineage açısından daha tamdır. Smart Export Report
yalnız insan/audit raporudur; indekslenmez. Bundan sonraki güncellemelerde proje başına
yalnız yeni `SmartExport.xml` verilmesi yeterlidir.

Kabul edilen dosya içerik adresli kütüphaneye kopyalanır:

```text
C:\innova\odi\<proje>\exports\<sha256>\design\SmartExport.xml
```

GPU'nun ilk kanonik girdisi `sha256:cfa3ff189ef7d1ac58067c77293c5f3fa5e465e94819bd82c5375da3ebe8a0ce`
digest'iyle kabul edilmiştir.

## ODI Studio adımları

1. Development Work Repository bağlantısını aç.
2. `Export > Smart Export` ile ilgili Project/folder'ları ve kullandığı Model/model
   folder'larını seç.
3. Shortcut materialization seçeneğini `No` bırak ve ZIP yerine XML üret.
4. `Export All Scenarios` ile scenario sürümlerini `scenarios/` altına ayrıca al.
5. Load Plan'ları child component'leriyle `loadplans/` altına al.
6. Topology'den yalnız Logical Topology export et; Physical Topology alma.

Smart Export özelliği ODI 11.1.1.6 ve sonrasında kullanılabilir. Daha eski bir 11g sürümü
varsa Project'i recursive export edip referans verilen Model ve global bağımlılıkları ayrı
XML'ler olarak aynı `design/` dizinine koymak gerekir.

## Kesinlikle alınmayacak içerik

- Master/Work Repository dump veya repository bağlantı tanımları
- Physical Topology, data server kullanıcı/parola ve JDBC/JNDI değerleri
- Execution Environment, agent host/IP/port, Security kullanıcı/profil nesneleri
- Session/operator logları, variable runtime değer/geçmişleri, keystore/wallet yolları
- Şifre çözülmüş scenario, KM veya procedure metni

Şifreli nesneler çözülmez; ilerideki parser yalnız `encrypted=true` ve nesne kimliğini
kaydeder, gövdeyi opaque tutar.

## Zekam kabul ve indeks akışı

```powershell
zekam project odi-smart-import gpu C:\export\SmartExport.xml `
  --library-root C:\innova\odi --library-name gpu --json

zekam project odi-smart-import gpu C:\export\SmartExport.xml `
  --library-root C:\innova\odi --library-name gpu `
  --plan-digest <plan-digest> --uygula --json

zekam project index gpu --oracle-config <gpu-oracle-config> `
  --authorize-remote-source --authorize-database-metadata `
  --authorize-odi-metadata --json
```

Preflight; bounded dosya/boyut, regular-file/reparse, strict UTF-8, XML parse, DTD/entity,
secret ve yasak repository/topology nesnesi kontrollerini yapar. Bağlantı makineye özeldir;
mutlak export yolu yalnız private local binding içinde tutulur. Çıktı ve portable kayıtlar
relative ref ve digest taşır.

Object-aware sanitizer yalnız Interface, Datastore, Package ve Procedure gibi tasarım
nesnelerinin allowlist alanlarını üretir. Connection, Physical Schema, Context, Agent,
credential/topology değerleri, variable runtime değerleri ve ham rapor dışarıda kalır.
Lineage kenarları XML içindeki exact ODI kimliklerinden kurulur; vector benzerliğinden
tahmin edilmez. Kaynak dosyanın tamamı embedding sağlayıcısına gönderilmez.

İlk GPU kabulünde 793 sanitize chunk ve 1.136 exact lineage kenarı üretilmiş; bunlar
4.432 kaynak kod chunk'ı ve 4.064 `GPU_USER` metadata chunk'ıyla aynı 9.289 kayıtlık aktif
generation içinde birleştirilmiştir.
