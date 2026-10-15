# Index ve test islerinin canonical bagi

Bu yuzeyler mevcut Work/Run kayitlarini kullanir; Work, grant veya migration
olusturmaz. Operational v7 acik migration ile hazirlanmis olmalidir. Eski
baglanmamis cagri yollarinin davranisi korunur. Native ajan mevcut Work baglamini
kendi araclariyla cozumler; bu kimlikleri son kullaniciya yazdirmasi gerekmez.

## Index

`zekam project index <proje> --dry-run --json` provider cagrisi yapmadan exact
execution contract verir. Canonical Run'in `plan_digest` alani bu ciktidaki
`plan_digest` olur. Source snapshot'in revision/tree alanlari ciktidaki
`source_revision` ve `tree_digest` ile ayni olmalidir. ODI kaynak plani ve Oracle
baglanti kimligi de execution contract'a dahildir; Oracle'a plan sirasinda
baglanti yapilmaz.

Mevcut Run bagiyla dry-run icin `--work-run`, `--work-revision` ve gerekiyorsa
`--work-step` verilir. Apply ayni seceneklere ek olarak `--plan-digest` ve
dry-run'daki `--work-binding-digest` degerlerini ister. Remote source, Oracle ve
ODI icin mevcut explicit disclosure secenekleri devam eder. CLI baglantisi sadece
izin verilmis effect'i canonical job'a baglar.

## Native test

`zekam test plan --native --canonical-source` mevcut native plan secenekleriyle
birlikte kullanildiginda `source_content_digest` verir. Bu digest secili uretim
dosyalarinin byte digest'lerini, build config digest'ini ve revision'i baglar.
Canonical source snapshot'in `content_digest` alani bu deger olur. Test dosyalari
candidate girdisi olarak kalir; test eklenmesi uretim snapshot'ini degistirmez.
Run'in `plan_digest` alani native planin `execution.plan_digest` degeridir.

Native `plan` icin mevcut `--run-id` yaninda `--canonical-work-revision` ve
istege bagli `--canonical-step` verildiginde `work_binding.binding_digest`
salt-okunur gosterilir. Request'in Work ve snapshot kimlikleri Run ile eslesir.
`test measure` ayni seceneklere ek olarak `--canonical-binding-digest` ister.
Mevcut exact plan/Maven digest, realm/project/graph/source, build ve network
kapilari yeni olcum icin uygulanmaya devam eder. Uretim dosyasi HEAD degismeden
edit edilse bile snapshot content digest kontrolu olcumu reddeder.

## Explicit model ile test uretimi

Ayni source content ve Work/Run baglama secenekleri `test plan` ve `test run`
yuzeylerinde de vardir. Bu modda `--native` verilmez. Model/assignment kimligi,
Maven plan, onaylar, budget/limit ve resume niyeti canonical input digest'ine
katilir; mevcut write-tests/build/network/remote-model izinleri korunur.
Canonical test run kisa transaction'li ledger ve artifact registrar kullanir;
OpenCode/model veya Maven sureci boyunca operational write lock tutulmaz.

`--resume` onceki paused sonucundan ayri job kimligi uretir. Birden fazla pause
sonrasi yeni kontrollu devam turu icin yeni execution key kullanilir; ayni exact
key'in tekrari onceki CAS sonucunu okur. Lease kaybi kullanici iptali sayilmaz:
loop terminali nonfinal `recovery-required`, outer job ise unknown/recovery olur.
Gercek kullanici iptali ve durable pause kapilari korunur.

## Sureklilik ve kanit

Index, native olcum ve explicit model batch ayni operational local_job_binding/lease/effect claim/receipt ve
Run checkpoint tablolarini kullanir. Sonuc digest dogrulamali mevcut CAS'a
kaydedilir. Native attempt ledger'i ve Maven/JaCoCo kaniti korunur; canonical
job tamamlanmasi coverage hedefi veya Work kapanisi anlamina gelmez.

Ayni binding/girdi/`--execution-key` tekrarinda tamamlanmis sonuc CAS'tan okunur;
index/provider veya Maven yeniden calistirilmaz. Kontrollu yeni olcum veya index
kosusu icin yeni execution key ve halen gecerli canonical baglar gerekir. Key'in
ham degeri kuyruga yazilmaz, idempotency digest'ine katilir. Baslamis veya belirsiz
job otomatik retry yapmaz; explicit recovery gerekir.

Lease uzun islemler ve son kaynak dogrulamasi boyunca yenilenir. Provider/Maven
calisirken SQLite write transaction'i acik tutulmaz. Sonuc checkpoint'i guncel
binding kontroluyle ayni kisa transaction'da yazilir; terminal receipt ayrica
kaydedilir. Bu iki kayit arasindaki kesinti basari iddiasi uretmez ve recovery'ye
gider. Ilk sonuc ve replay ayni kanit zincirini yeniden okur: tek effect'in
fingerprint'i ve completed receipt'i, ayni Run/job icin tekil checkpoint,
checkpoint payload digest'i, binding/input/source/claim kimlikleri ve artifact
metadata'si CAS byte boyutu ve digest'iyle eslesmelidir. Eksik, fazla, 16 KiB
uzeri veya uyusmayan checkpoint basari sayilmaz; bozuk kanit yeni effect
baslatmaz. Checkpoint ve resume projection'lari grant veya lease devretmez.
