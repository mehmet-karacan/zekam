# ODI Smart XML ayni akis sozlesmesi

DTD/entity kontrolu, XML pull parser ve SHA256 ayni acik dosyanin ayni byte bloklarini kullanir. Expat guard bloklari ElementTree oncesinde denetler; DTD, entity veya external entity reddedilir. UTF-16 dahil encoding-aware kontrol korunur.

Sinirlar: 128 MiB kaynak, 1.000.000 element, 256 XML derinligi, 200.000 Object ve 64 KiB okuma blogu. Bitmis Object ve dis kapsayici node lar parent tan ayrilir; bos XML iskeleti birikmez. Yalniz Field etiketlerinin mevcut sinif/alan allowlist i okunur.

Dosya descriptor kimligi acilista, descriptor ve path kimligi/boyutu/mtime sonunda denetlenir. Sanitized manifest donmeden kaynak hash tekrar okunur. Bu denetim atomik filesystem snapshot veya OS sandbox iddiasi tasimaz.

Yeni import planlarina xml_profile eklenir. Sanitizer profili odi11g-smart-sanitized-v2 olur; eski receipt ler yeni parser kabulune yukseltilmez. Native canonical import, secret sanitizer ve metadata disclosure kapilari korunur.

## Bekleyen dogrulama

Tek descriptor/hash, tek byte UTF-8/UTF-16 declaration, element/depth/object/byte cap, cok nesneli field korunumu ve kaynak mutation regresyon kaynaklari hazirlandi. Testler kullanici talimatiyla CALISTIRILMADI; tum gelistirmeler sonrasi toplu asamada kosulacak. Gercek native ODI ve WP kabulu aciktir.

## Lineage kimlik butunlugu

Smart XML tek-kayit indekslerinde ayni sinif/kimlige ait celisen allowlist kayitlari lineage oncesinde reddedilir; celisen tekrar Field de reddedilir. Ozdes kayit ve composite relation parent tekrarlarini engellemez. Chunk profile sanitizer v2 ile eslendi. Regresyon kaynaklari hazir, CALISTIRILMADI; native/WP kabulu acik.
