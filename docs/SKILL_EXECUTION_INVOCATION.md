# Actual execution receipt -> invocation ledger

WP-10.03 icin `skill invocation-from-execution <execution-ref>` eklendi.
Kaynak herhangi bir rapor JSON'u veya model beyanı degil; existing
skill.native.execute job'unun terminal claim/checkpoint/receipt/CAS zinciridir.
Mevcut invocation-report hem native hook recording hem bu actual execution
recording operasyonunu taniyarak exact source proof ve ledger row'larini okur.

## Source proof

Actual execution report assignment/context/invocation/dispatch/native artifact
zincirini yeniden kontrol eder. Native process/tool/protocol sonucu failed ise
completed skill invocation uretilmez. Local oracle mismatch varsa gercek tool
cagrisi kaydedilebilir; sonuc verified-success'e cevrilmez.

Yeni plan source execution job UUID'sini, terminal evidence digest'ini, native
artifact'i, requested model/harness'i ve original typed Work binding'ini pin eder.
Native session/call hash kimligi duplicate delivery'yi ayirir. Kalici row zamani
execution terminal receipt created_at alanindan gelir; bunun native tool'un
duvar saati oldugu veya alti bagimsiz olcum yapildigi iddia edilmez.

Yeni ingestion guncel active pointer'i source execution active revision'iyla
eslestirir. Host metadata'si ayri kriptografik attestation kazanmamis olarak
host_attested=false kalir. Requested model/profil de upstream bagimsizlik kaniti
degildir; provenance canonical-dispatch-model-request olarak saklanir.

## Recording authority ve original Work

Ingestion kendi reserved skill.invocation.from-execution canonical Work job'u
ve live claim/fence'i altinda yapilir. Source execution receipt'i bu yeni effect
icin claim yerine gecmez. Paket, active scope/revision ve source evidence write
oncesinde tekrar okunur. Tum invocation satirlari ayni learning transaction'inda
yazilir; exact completed replay yalniz immutable row/CAS proof okur.

Recording run yeni bir plan icin ayri olabilir. Buna karsin recording project,
Work item, config revision/digest ve source snapshot original execution ile ayni
olmalidir. Work revision geriye gidemez. Invocation work_ref/run_ref alanlari
recording run'a degistirilmez; original execution Work/run kimligini tasir.
Bu sayede recording isi native tool'u baska bir Work'te calismis gibi gostermez.

Ayni native call baska recording run veya duplicate source ref ile yeniden
kaydedilemez. Source execution metadata ve actual hash subject'i ortak atomic
ledger writer'a yalniz exact source plan yeniden derlendikten sonra girer.
Var olan hook event ingestion'i kendi source semantigini korur; hook beyanı
actual claimed execution diye yeniden adlandirilmaz.

## Tarihsel okuma ve kalan kapilar

Report source execution receipt ve her invocation predecessor/hash/Work/run
bagini yeniden okur. Sonraki deprecation tarihsel gozlemi silmez; yeni ingestion
guncel active pointer kapisini gecemez. Readback yeni execution, row veya model
cagrisi uretmez. State zinciri unverified ile biter; skill_outcome_v2'ye sonuc
yazilmaz ve skill effect basarisi iddia edilmez.

Offline fixture kaynaklari original run preservation, own receipt/replay,
foreign Work reddi, duplicate native call, failed process, mismatch ve historical
deactivation senaryolarini kapsar. Native transport/activation port doubles
bagimsiz verifier veya host kabul kaniti sayilmaz. Testler CALISTIRILMADI;
tum gelistirmelerden sonraki son toplu asamayi bekler.

Sirada actual execution artifact'ini farkli verifier'a baglayan receipt producer
ve skill outcome recording vardir. Gercek host, passing kalite/provenance,
bagimsiz review ve WP-02..12 genel kabul kapilari aciktir.
