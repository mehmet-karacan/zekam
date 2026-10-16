# Belge provider locator ve cikti sozlesmesi

## Kaynakta bulunan sorun

Generic `OcrParser` page/confidence/bbox alanlarinda `int`/`float`, text alaninda
`str` donusumu yapiyordu. Ornegin kesirli page sessizce baska bir sayfa numarasina,
None/sayisal text belge metnine donusebiliyordu. Dusuk confidence filtresi locator
ve confidence dogrulamasindan onceydi. `StructuredDocumentParser` metin olmayan
text'i de stringe ceviriyor ve verilen PDF bbox'ini kaybediyordu.

## Kaynak duzeltmesi

- Domain `Locator` page/line/block/timestamp icin bool olmayan gercek int ister;
  bbox finite 0..1 sayilar, heading path metin bilesenleri olmalidir. Degerler
  kirpilmaz, yuvarlanmaz veya uydurulmaz. `ContentUnit` gercek text, int order ve
  finite 0..1 confidence denetler. Gecerli body/as_dict bicimi degismez.
- Generic OCR provider confidence ve locator once dogrulanir, sonra threshold
  filtresi uygulanir. Threshold config'i de finite 0..1 sayi olmalidir. Metin ve
  sayisal metadata otomatik donusturulmez.
- Generic structured provider exact DOCX/PDF enum ister. PDF page zorunlulugu,
  DOCX page uretmeme kurali ve verilen PDF bbox korunur. Heading path 64 bilesen,
  bilesen basina 512 karakter sinirindadir. Unknown kind ham degeri hatada yazilmaz.
- Provider blok akisi 16384 blok ve normalize unit govdelerinde toplam 16 MiB ile
  bounded'dir; dusuk confidence ile elenen bloklar da butceden duser. Cap+1 girdide
  islem reddedilir, partial sonuc belge sayilmaz. Girdi mevcut MAX_DOCUMENT_BYTES
  sinirini korur. Bu sinirlar callback'in kendi icinde yaptigi unbounded allocation
  veya dis servis cagrilarini sandbox ettigi anlamina gelmez.
- Generic OCR/structured parser_version 2'dir; profile cikti sinirlarini tasir.
  Mevcut offline OOXML/PDFium/Tesseract adapterlerinin profilleri aynen kalir;
  ortak domain locator kabul kurallari onlara da uygulanir.

Bu gelistirmede model/OCR/dis servis cagrisi veya kullanici ingestion'i yapilmadi.
Regresyon kaynaklari provider fixture'lariyla hazirlandi, CALISTIRILMADI.
Native kaynak/secret/provenance ve tum WP kabulleri final test asamasinda
dogrulanacaktir; kaynak duzeltmesi tek basina WP/Work kapanisi degildir.
