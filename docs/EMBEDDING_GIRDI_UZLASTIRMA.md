# Document Embedding Girdi Uzlastirmasi

WP-05.03/04 gelistirmesi provider ledger'indeki batch effect kapisini girdi
digest'leriyle tamamlar. Bu belge yetki veya basarili kabul receipt'i degildir.

## Claim ve kalici bag

Production runtime document job'unu kaydederken exact prepared payload,
fixture/profile ve call kimligini dogrular. Append-only manifest/slot tablolari
yalniz job/effect/operation/profile ve input digest'lerini saklar. Kaynak metin,
credential veya ham provider response saklanmaz. Input kimligi metin digest'i,
qualified profil, endpoint binding ve disclosure classification'a baglidir;
realm/proje claim sorgusunda ayrica sinirlanir.

Overlap sorgusu ve claim insert ayni `BEGIN IMMEDIATE` icindedir. Ayni
girdiler farkli sira veya batch'e konulsa da onceki receipt'siz, completed veya
unknown-effect claim yeni cagridan once uzlastirma ister. Kanitli normal failed
receipt mevcut retry politikasini korur. Farkli proje veya yeni disjoint
girdiler ayni engeli miras almaz. Duplicate girdi slotlari sira kaybetmez.

## Completed sonucun yeniden kullanimi

Journal exact batch sonucuna ek olarak slot manifest'i uzerinden onceki
completed batch'in alt kumelerini ve yeni siralarini cozer. Her parent icin
realm/proje/effect/operation, terminal receipt, vector digest, profile ve slot
binding yeniden denetlenir. Recovery receipt'i sifir provider cagrisi ve
reconciliation evidence digest'i tasir; yeni basari receipt'i uydurulmaz.

Runtime cache miss'lerinde yeni batch kurmadan once tekil completed slotlari
uzlastirir. Cache yazimi kesilmis eski slotlar cache'e geri alinir; kalan yeni
girdiler normal batch planlayicisina gider. Cache sayaci, uzlastirilan chunk
sayaci ve yeni provider batch sayaci ayri korunur. Iptal/deadline kontrolleri
recovery giris/cikisinda devam eder; her recovery provider-free kalir.

## Eski kanit ve sinirlar

Onceki ledger satirlari silinmez veya tahmini input bagiyla doldurulmaz. Eski
exact batch ve vector journal'i hala exact kaynak girdisiyle okunabilir.
Slot metadata'siz eski receipt'siz/unknown/completed claim disjoint girdileri
kanitlayamadigindan yeni manifest'li cagri fail-closed uzlastirma ister.
`bind_completed_inputs` eski completed batch icin orijinal sirali girdiler ve
qualified profile yeniden saglandiginda exact effect/operation ve kalici
receipt/vector kanitini ayni write transaction'inda dogrular. Sonrasinda
manifest/slot metadata'si append edilir; mevcut claim/receipt/vector satirlari
degismez. Exact tekrar ayni manifest digest'iyle idempotenttir; farkli batch,
foreign scope, bozuk vector veya eksik terminal kanitta bag uydurulmaz.

Runtime yalniz cache miss ve manifest'siz completed sonuc varsa mevcut tum
chunk'larin bounded batch adaylarini bu yola verir; sonra tekil recovery yapar.
Guncel batch exact eski batch'i yeniden kurmuyorsa otomatik tahmin yoktur.
Orijinal girdileri/profili artik bulunamayan veya sonucu belirsiz eski
cagrilar icin harici kanitla recovery kapisi acik kalir. Manifest/receipt olmadan
otomatik retry veya harici provider sonucu varmis gibi kabul yoktur.

Yeni tablolar mevcut private provider ledger schema'sina additive eklenir;
operational schema v1..v9'a dokunulmaz. Bu dilimde kullanici ledger'i acilmadi,
migration veya canli provider/model cagrisi yapilmadi. SQLite lookup'larina
input digest index'i eklendi; recovery maliyeti final performans kabulunde
olculecek. Concurrency 1 ve yerel cagri kotasi mevcut sinirlarini korur.

Regresyon kaynaklari: `tests/unit/test_opencode_remote_embedding.py` ve
`tests/integration/test_project_rag_pending_publish.py`. Regroup/reorder,
receiptless/unknown/completed/failed gate, proje scope'u, payload drift'i ve
cache crash sonrasi eski+yeni girdi senaryolari hazir, CALISTIRILMADI.
Eski completed ledger'in exact input baglama, idempotent tekrar, foreign scope,
yanlis sira ve bozuk vector negatifleri de ayni final test asamasini bekler.
