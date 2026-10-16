# Ayri native outcome verifier

WP-10.03 icin outcome-verify ve outcome-verification-report kaynaklari eklendi.
Bu asama bir actual native task artifact'ini ayri native verifier execution'i
ile kontrol eder. Skill outcome ledger'ina yazim siradaki ayri recording adimidir.

## Kaynak ve sinir

Girdi actual skill.native.execute receipt'inden uretilmis invocation recording
job'udur. Hook beyanı veya caller'in review JSON'u kabul edilmez. Exact source
Work/run, terminal receipt/checkpoint, CAS, assignment ve tool/final artifact
baglari yeniden okunur. Context skill instructions, task/caller-supplied oracle,
actual artifact body ve native tool hash projection'ini tasir. Ham transcript,
reasoning, credentials veya provider headers aktarilmaz. Disclosure 180000 byte
ile sinirlidir ve public-safe kontrolunden gecer.

Oracle builder'a gitmez; verifier'a kontrol girdisi olarak verilir. Verifier
butun skill/case/output icerigini untrusted veri kabul eden sabit bir instruction
ile, tools deny-all ve tek native step profilinde calisir.

## Model ve execution ayrimi

Builder'in farkli variant'i veya provider etiketiyle ayni model secilemez.
Reviewed custom provider profili builder execution'indaki frozen configured
provider/model map'ine exact uymali; builder/verifier upstream modelID degerleri
farkli olmalidir. Ayni upstream'i gizleyen alias reddedilir. Verifier'in actual
native session'i builder session'ini yeniden kullanamaz.

Builtin provider icin farkli requested model tek basina upstream kimlik kaniti
sayilmaz; requested-distinct-upstream-unproven sonucu unverified kalir.
Reviewed config'deki farkli upstream modelID, configured independence temelidir;
kriptografik/provider upstream attestation iddiasi degildir. Bu nedenle
upstream_identity_attested=false her durumda korunur.

Verifier ayni Work/project'e baglanir; original execution binding'i source proof
olarak saklanir. Yeni verifier execution'i current yetkili config/source binding'i
altinda calisabilir. Original invocation Work/run kimligi degistirilmez.

## Karar ve canonical proof

Reserved skill.outcome.native-verify operation'i ayri Work job/lease/claim,
assignment-first dispatch, checkpoint, terminal CAS ve receipt kullanir.
Modelin serbest verdict metni kabul edilmez: exact subject digest, pass/fail/
unavailable verdict, artifact/process/safety checks ve allowlisted reason codes
zorunludur. Verifier process/protocol hatasi veya malformed karar unverified kalir.

Tek task artifact'i icin local exact oracle ve verifier pass, process/safety
pass, consistent reason ve reviewed distinct configured upstream eslesirse
verified-success; local mismatch ile verifier fail ve mismatch reason ayni
kosullarda verified-failure olur. Ambiguous/insufficient evidence, model/config
baginin belirsizligi veya celisen karar unverified kalir.

Bu statuler yalniz one-native-task-artifact kapsamini tasir. Quality evaluation
complete ve activatable false kalir; tum skill kalite/provenance/preference veya
Work kapanisi yerine gecmez. Caller-supplied case, bagimsiz dataset provenance'i
olarak yeniden adlandirilmaz.

Report source execution/invocation receipt ve actual context/oracle/artifact'i
yeniden derler; verifier assignment role/harness/envelope/CAS ve native session
ayrimini tekrar kontrol eder. Karar ve status actual verifier artifact'inden
yeniden hesaplanir. Completed exact replay yeni model cagrisi yapmaz.

## Gelistirme kaniti

Offline SQLite/native fixtures verified-success/failure kapsamli karar,
unverified malformed/failed verifier, same-session, same-model/variant/upstream
alias ve builtin unknown-upstream durumlari icin hazirlandi. Fixture transport,
activation ve runtime policy doubles gercek host/provider veya bagimsiz kabul
kaniti sayilmaz. Testler CALISTIRILMADI; tum gelistirmelerden sonraki son toplu
asamada calisacak.

Sirada verifier terminal receipt -> immutable skill_outcome_v2 recording bagi
vardir. Genel WP-02..12, gercek host ve passing skill quality kapilari aciktir.
