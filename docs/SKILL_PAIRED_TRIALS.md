# Gercek paired skill trial

WP-10.03 icin `skill trial-run` instruction-only olcum yoludur. Native OpenCode
V2 batch adapter'ini kullanir; yeni bir model protokolu veya shell wrapper'i yoktur.
Bu belge calistirilmis test veya canli kabul kaniti degildir.

## Plan ve yurutme

`zekam skill trial-run <revision> --package-root <paket> --case-file <vaka>
--model <provider/model> --project <proje>` provider-free plan verir. Optional
`--provider-profile` mevcut bounded custom/loopback profilidir; paket ve vaka
exact hash ile plana baglanir. Binary sadece dosya olarak cozulur; dry-run'da
version probe veya model cagrisi yapilmaz. Plan en fazla iki agent cagrisi,
her cagri icin 10..600 saniye, deny-all tools ve exact model/config binding tasir.

Apply `--uygula --plan-digest`, canonical `--work-run`, `--work-revision`, optional
`--work-step`, dry-run `--work-binding-digest` ve assignment metadata'si icin
`--realm-id`/`--coordinator-assignment-id` ister. Assistant bu referanslari mevcut
canonical kayitlardan cozer; kullanicidan UUID veya JSON yazmasi beklenmez.
Remote profile ayrica bu exact vaka/paket/model icin `--authorize-remote` ister.
Kullanici-geneli client ayarlari degismez. Yeni arac kurulmaz.

`skill.trial` reserved canonical job olarak ayrildi. Work/Run/config/plan kontrolu,
lease heartbeat, claim-before-effect, CAS result, checkpoint ve terminal receipt
mevcut canonical job runtime'indan gelir. Generic worker bu isi alamaz. Child
assignment ve invocation her native dispatch'ten once kalici CAS'a yazilir.
Programatik iptal version probe ve native run sureclerine iletilir; process runner
owned tree temizligini uygular. Baslamis belirsiz job otomatik yeniden dispatch edilmez.

Paket current scoped candidate revision'in package_digest'iyle eslesmelidir;
active olma sarti yoktur. Her cagri oncesi ve terminal publication oncesi paket,
vaka, provider profili ve registered source root tekrar okunur. Plan model,
timeout, tools/config, vaka/oracle digest'i ve kaynak kokunun cihaz-yerel digest'ini
baglar. Yeni root ayni Git revision diye esdeger kabul edilmez.

## Vaka sozlesmesi ve olcum

Vaka dosyasi strict UTF-8/JSON, regular/link-free ve en fazla 24 KiB'dir:

```json
{
  "schema": "zekam-skill-trial-case/v1",
  "case_id": "case-one",
  "partition": "holdout",
  "input": {"question": "Task input"},
  "expected_output": {"answer": "Expected structured result"}
}
```

`partition` development veya holdout olur. Duplicate key, nonfinite number,
unknown field ve secret/PII reddedilir. Holdout etiketi tek basina bagimsiz
dataset provenance kaniti sayilmaz; plan bunu caller-supplied olarak aciklar.
Vaka dosyasi ve runtime artifact'lari kaynak repo disinda tutulur.

Her vaka iki fresh native session'da calisir: baseline skill instructions olmadan,
candidate exact SKILL.md instructions ile. Sira vaka digest'inden secilir. Model,
harness, tools ve sure butcesi iki kosulda aynidir. Beklenen sonuc ve oracle
digest'i builder context'ine konulmaz. Paket script/reference/asset dosyalari
yurutulmez; sonuc yalniz instruction-only profile kapsamindadir.

Native sonuc `{"result":{},"body":{"output":<JSON>}}` olur. Adapter modelin
uydurdugu agent_id yerine observed native session/message kimligini yazar.
Ayni native kimlik iki kosulda kullanilirsa receipt yayinlanmaz, recovery gerekir.
Yerel grader canonical JSON digest'lerini exact karsilastirir. True/1 ve 1/1.0
birbirine esdeger sayilmaz. Eksik/yanlis output shape basari olarak sayilmaz.

Receipt-bound rapor her kosul icin dispatch sonucu/artifact/output digest'i,
native kimligi, outcome, valid_output, exact_match ve monotonic elapsed_ns tasir.
Sure native version/config/process hazirligini da kapsar; saf inference suresi
veya token olcumu diye sunulmaz. `agent_calls` gercek dispatch sayisidir;
provider_calls ve actual_tokens olculmedigi icin null kalir. Loopback'te
remote_calls=0 olabilir; bu yerel inference cagrisi sayisi degildir.

Bu rapor tek paired trial'dir. Bes tekrar/holdout aggregate, farkli modelle
bagimsiz verifier, ayrica reviewer ve SkillEvaluationV2 producer baglantisi
halen aciktir. Trial receipt'i evaluator/verifier/reviewer receipt'i yerine
gecmez; `independent_verification=false`, `activatable=false` korunur.

Regresyon kaynaklari canonical DB claim/receipt, native event/CAS adapter,
oracle isolation, paired result/replay, plan drift, remote authorization,
duplicate session, reserved worker ve cancel forwarding yollarini kapsar.
Testler CALISTIRILMADI; tum gelistirmelerden sonraki final asamayi bekler.

## Canonical toplama ve tekrarlar

`--repetition 0..99` ayni vakanin ayri yurutmesini plan/claim/assignment
kimligine baglar. Kosul sirasi tekrarlarla degisir; ayni tekrar cached replay'dir.
Plan/result v2, candidate instruction digest'ini ve gercek dispatch envelope'unu
tasir. V1 receipt'leri degistirilmez ve bu aggregate yoluna otomatik yukseltilmez.

`zekam skill trial-aggregate <revision> --trial <terminal-job-ref> ...
--case-file <vaka> ... --project <proje>` salt okunur calisir. Her benzersiz vaka
bir kez verilir. Liste en fazla 100 trial ve 100 vakadir. Ajan terminal job
referanslarini canonical ciktidan kendisi cozer. Kullanici report JSON'u veya
modelin yazdigi basari etiketi kanit kabul edilmez.

Job'un gercek Work/project binding'i, completion checkpoint/artifact/claim/receipt,
CAS result hash'i, exact v2 plan, assignment/invocation/result kayitlari ve native
output artifact'i tekrar okunur. Gercek prompt'ta vaka input'u ve condition'a
uygun instructions yeniden dogrulanir; oracle context'e sokulamaz. Grader
valid_output/output_digest/exact_match alanlarini tekrar hesaplar. Owned timing
ve diger sistem sayaclari PII taramasi diye rastgele reddedilmez; disaridan gelen
prompt/model artifact'lari ayrica secret/PII kapisindan yeniden gecer.

Ayni job'un UUID/idempotency alias'lari, ayni vaka/tekrar'in farkli Run'lardaki
kopyalari ve farkli message ID altinda bile ayni native session tekrar sayilmaz.
Project/revision/package, kaynak koku, model, config, harness, tools veya sure
butcesi farkli trial'lar birlestirilmez. Eksik/corrupt artifact ve degisen oracle
reddedilir. Siralama aggregate digest'ini degistirmez.

Sonuc development ve holdout partition'larini ayri hesaplar. Her vaka kendi
tekrarlarinin ortalamasiyla, partition icinde her vaka esit agirlikla raporlanir;
cok tekrar edilen tek vaka diger vakayi ezmez. En az bes holdout pair gerekir;
development ciftleri bu esigi doldurmaz. Herhangi bir holdout vakasinda exact
match gerilemesi `regressed` verir. Process/protocol basarisizligi `failed`
olarak korunur; kotu ornekler denominator'dan atilmaz.

`comparison_state` olculen exact JSON karsilastirmasidir. Unique vaka ve tekrar
sayilari aciktir; belirsizlik descriptive-only'dir, guven araligi veya genelleme
kalitesi uydurulmaz. Caller-supplied holdout etiketi bagimsiz dataset provenance'i
olmaz. Aggregate sifir provider cagrisi yapar, `independent_verification=false`
ve `activatable=false` tasir. Farkli verifier/reviewer yurutmesi ve lifecycle
producer baglantisi halen aciktir. Yeni regresyon kaynaklari da CALISTIRILMADI.
