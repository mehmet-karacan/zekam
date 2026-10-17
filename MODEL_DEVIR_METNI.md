# Guncel devir - Y-12 gercek kullanici recovery kaniti

Mevcut kullanici home'u icin gercek v2 full backup create/verify/restore GECTI.
Ilk repo-disi hedef private ACL olmadigi icin effect oncesi reddedildi;
sonra yalniz yeni private-backups dizinine ACL uygulanip yeni bundle olusturuldu.
Restore mevcut home'a degil ayri yeni hedefe yapildi. Anahtar icerigi okunup
loglanmadi. Kanit: user-backup-verify.log, user-backup-restore.log.

Regresyonlarda migration/replay/parite gectikten ve bagimsiz kaynak incelemesi
okunduktan sonra kullanici v2->v3 canonical memory prepare ile uygulandi.
Terminal receipt: sha256:d8e2082c07130dacdbcca347f4971fede5ffa7d7e0727f70accbaf2bf27b694d.
Readback: learning schema3, local core all_ready=true, tum eski tablo logical
icerigi backup ile ayni. user-migration-readback.json. Onceki devirdeki
kullanici DB halen v2 ifadesi artik TARIHSELdir. Migrationi yeniden baslatma.

Source-linked CLI ile synthetic index ve explicit/automatic project ask ve
plain source label kabul gecti. 39359a4 source fix, remote HEAD ile eslesti.
Tam Windows suite halen exec session 84317 altinda AKTIF (run1 ilk hatali
baseline); en son yuzde1 ilerleme goruldu. Aynisini poll et, yeniden baslatma.
Focused2 87 passed/3 platform skip. Son wheel rebuild/kabul, full suite hata
cozumleri, Maven raporu ve final teslim raporu halen bekliyor. Goal aktif;
tamamlama veya yeni onay bekleme yok. Korunan dort untracked girdiyi koru.

---

# Guncel devir - Y-12 final dogrulama SURUYOR

Son kaynak dilimi e3741df (yerel bellek propose), oncesi 0072568 (v2/v3 uyum).
Tum planli gelistirmelerden sonra test asamasi acildi; Y-12 BITMEDI.
Final artifact alani repo disinda C:/temp/zekam-y12-e3741df.
Tam Windows suite run1 exec session 84317 ile halen calisiyor; ilk hatalar var.
Bu handle'i once poll et, timeout nedeniyle yeniden baslatma. Baseline run1
source_discovery Windows duzeltmesi oncesi basladi, final green kabul degildir.
Tam sonuclari ve hatalari al; sonra gerekli duzeltmeleri ve etkilenen kabul
kosularini ayni Y-12 final asamasinda tamamla.

Gercek E5 CPU kabul PASSED: 384/finite/L2; TR, EN, TR-EN semantic margin pozitif;
2 thread/batch4, load 4889ms, query medyan 8.55ms, batch4 medyan 34.36ms,
peak process RSS 462950400 byte. Ayrinti e5-acceptance.json (sentetik kaynak).
Source gates: package manifest/catalog/platform/reachability exit 0.
Temiz wheel e3741df functional kabul 11/11 PASSED; self verification,
bagimsiz Work/verifier attestation'i DEGIL. Ayrinti wheel-functional-acceptance.json.

Gercek RAG kabul Windows lstat/fstat ctime farkini ortaya cikardi. Ortak
kimlik dev/ino/mode/size/mtime ile eslenir, iki API kendi full before/after
ctime snapshot'ini korur; sadece cross-API ctime esitligi aranmaz. Descriptor
ctime drift ve ayni boyutlu kaynak degisimi reddi korunur. Source discovery ve
memory proposal hedefli tekrar: 87 passed, 3 platform skip (focused2-junit.xml).
Ilk focused: 64 passed/1 fail; test eski revision ilk satirini yeni saniyordu,
artik exact corrected revision digest'iyle revision=2 dogrulanir.
Synthetic local proje bind/index ve kaynakli ask (source-linked CLI) artik gecti;
remote provider kullanilmadi. Yeni wheel bu kaynak duzeltmesini icerecek sekilde
Y-12 sonunda yeniden uretilip temiz kurulumla dogrulanmali.

Maven fixture target'a yazmamak icin tmp copy izolasyonu bu final dilimdedir.
Kullanici learning DB halen v2; migration/gercek backup restore HENUZ YAPILMADI.
Once migration regresyonlari ve gerekli bagimsiz kaynak incelemesi kanitini
oku; ardindan canonical memory prepare ve yeni hedefe backup/restore kabulunu
bitir. Canli kurum provider kabulunu gercek kanit yoksa not-run olarak yaz.
Dort korunan untracked girdi degismeden/stage edilmeden kalir.
Y-12/goal tamamlandi isareti verme; tam suite, wheel ve restore halen acik.

---

# Guncel devir - Y-09 yerel aday olusturma gelistirmesi

memory propose yerel terminal memory.observe kanitini uretip memory.propose
claim/receipt altinda SQLite adayi kaydeder. Bos learning store'unda close
session gerektirmez; eski receipt kaynaklari mevcut WP-08 kapisini korur.
Gozlem content digest/key/class/author/candidate kimligine exact baglidir;
terminal olmayan/uydurma/kapsami farkli kanit kabul edilmez. Operational job
payload'ina ham bellek icerigi girmez. Plan salt okunur, exact apply zorunlu,
replay ayni candidate ve iki terminal job'u yeniden kullanir. Proje verilirse
kayitli proje kontrol edilir. Review/promote ve supersedes ayri adimdir.
Promote artik review sonrasi gercek zamani kullanir; +1 saniye gelecek zaman
uretip hizli ardil adayi stale saydirmaz.

Regresyon kaynaklari: tests/integration/test_memory_proposal_cli.py; fresh home,
plan/no-effect, wrong digest, apply/replay, raw-content-free job, reviewed
correction, forged observation ve invalid-input kontrolleri hazirlandi.
Testler HENUZ CALISTIRILMADI. Kullanici DB migrationi veya gercek memory adayi
yazilmadi. Kaynak incelemesindeki uc uygulama eksigi icin duzeltmeler kayitli;
siradaki asama Y-12: tum testler, Windows wheel/E5/Maven/backup restore kabul.
Onceden dirty Maven fixture izolasyonu final test harness'iyle alinacak;
dort korunan untracked girdi sahiplenilmeyecek.

---

# Guncel devir - Y-09/Y-10 schema uyumluluk duzeltmesi

Kaynak incelemesindeki v2 backup ve v3 skill prepare bulgulari duzeltildi.
Local core dogrulamasi learning v2/v3 icin ayni canonical fingerprint ve
zorunlu unique index kontrolunu kullanir. Saglam v2 otomatik migrate edilmez;
bozuk metadata/DDL/index reddi korunur. Skill prepare v3 icin exact planli
no-op doner; receipt veya migration basarisi uydurmaz, effect olusturmaz.
V1->v2 eski migration/replay semantigi degismedi.
V2/v3 tam bundle restore, index drift ve v3 prepare no-op regresyonlari hazir.
Hicbir test CALISTIRILMADI; kullanici DB/backup dosyalarina yazilmadi.
Siradaki gelistirme: Y-09 mevcut SQLite learning store icin kanita bagli
candidate olusturma yuzeyi. Sonra tum gelistirmeler bitince Y-12 final kabul.
Onceki Maven fixture dirty degisikligi ve dort untracked girdi korunur.

---

# Guncel devir - Y-03 kaynakli yanit ek duzeltmesi

Son kullanici talimati Y-03 uzerinden devamdir. Test kosusu BASLATILMADI.
Citation locator icinde traversal/absolute path, Locator tarafindan
PolicyViolation uretirken answer kapisi yalniz ValidationFailed yakaliyordu.
Bu nedenle guvensiz locator belirsizlik yaniti yerine exception uretebiliyordu.
PolicyViolation artik citation-invalid sonucuna doner; optional metin alanlari
canonical Locator cagrisi oncesi tip/bosluk kontrolunden gecer. Regresyon
ornekleri hazirlandi; runtime dogrulama tum gelistirmelerin sonundadir.

1739a5f..c4a1e5e bagimsiz kaynak incelemesinde Y-03 icin baska somut
Critical/Important bulgu yok. Diger kapsamda uc acik bulundu: Y-09 yeni aday
kaydetme CLI yolu eksik, v3 home'da skill prepare v2 planiyla uyumsuz,
Y-10 eski learning-v2 backup verify/restore ortak validator tarafindan reddediliyor.
Bunlar halen acik gelistirmedir; Y-12ye gecis notu bu eksikler giderilmeden
test baslatma veya tum teslim tamamlandi iddiasi olarak kullanilamaz.
Kullanici DB migrationi/backup restore bu tur yapilmadi.

Onceden dirty Maven fixture test izolasyonu bu Y-03 commitine alinmaz.
Korunan dort untracked girdi degismeden, stage edilmeden kalir.

---

# Guncel devir — Y-12 final asamasi sirada

Y-01..11 kaynak gelistirme teslimleri kayitli. Henuz test kosusu YAPILMADI.
Simdi Y-12ye gec: tam Windows suite, gerekli bagimsiz review, tracked wheel-only
kabul, gercek E5 CPU/384/normalization/TR-EN/performance, Maven ve backup/restore.
Kullanici learning DBsi halen v2; v3 migration once negatif/parite testleri ve
review gecmeden uygulanmaz. Yeni archive event eski revisionlari degistirmez.
Untracked RAG modulu ve iki testi, Java target dizini korunuyor. Pytest kaynak
kapsamini belirlerken unreviewed untracked iki testi sessizce sahiplenme; ayrimi
raporda acik yaz. Wheel scripts/build_tracked_wheel.py ile mevcut manifestten
repo disina uretilir; kaynak clone veya foreign dosya ekleme.
Son kaynak karari AKTIF_GOREV.md 0.15; guncel HEAD/remote ve artifact sureclerini
once oku. Test basarisizsa duzelt ve etkilenen kontrolleri ayni final asamada
tekrar kos. Y-12/goal tamamlanmasini kanit yokken isaretleme.

---

# Guncel devir — Y-10 kaynak teslimi

Y-01..10 gelistirme teslimleri kayitli. Siradaki Y-11, sonra tek final Y-12.
Test ve kullanici memory migrationi henuz yapilmadi; mevcut DB v2.
Aktif liste AKTIF_GOREV.md 0.3, son kayit 0.14. Untracked girdileri koru.

---

# Guncel devir — Y-09 gelistirme teslimi

Y-01..09 kaynak teslimleri kayitli; siradaki Y-10, sonra Y-11 ve Y-12.
Archive append-only learning v3, memory prepare explicit v2->v3 kaynagi eklendi.
Kullanici DB'si halen v2; test veya runtime migration YAPILMADI. Migration/rollback
ve archive/replay/regresyon kabul Y-12de. V1/V2 schema pinlerini veya append-only
head guardlarini gevsetme. Onceki notlar tarihsel; aktif liste AKTIF_GOREV.md 0.3,
son durum 0.13. Korunan dort untracked girdiyi otomatik stage etme.

---

# Guncel devir — Y-09 kismi gelistirme; hedef aktif

Kullanici hedefi Y-12ye kadar devamdir; bitmedi, duraklama veya yeni onay bekleme yok.
Y-01..08 gelistirme teslimleri kayitli. Y-09 --supersedes CLI bagi eklendi,
kalici semantik bellek arsivleme HENUZ YOK; Y-09 checkbox acik kalir.
Mevcut local_learning memory_revision append-only, unique(candidate_digest) ve
memory_head contiguous/no-delete guardlariyla korunur. Arsivleme icin bu
korumalari gevsetme, eski revision digest/body'yi yerinde degistirme veya
receipt'i sahte memory state gibi benimseme. Gerekli store/schema/CLI ve
read/search/is_active baglarini mevcut mimariyle tamamla; mevcut kullanici DB'ye
sessiz migration uygulama. Sonra Y-10 operasyon/backup, Y-11 paket/belge,
Y-12 TUM gelistirmeler bittikten sonra tek final test asamasi.
Henuz pytest, Maven, embedding veya integration kosusu yapilmadi; yalniz kaynak
incelemesi, py_compile, diff/projection. Korunan dort untracked girdiyi stage etme.
Son durum AKTIF_GOREV.md 0.3 ve 0.12; kaynakta commit ve remote HEAD'i yeniden oku.

---

# Guncel devir — Y-08 gelistirmesi

Y-01..08 gelistirme teslimleri kayitli. Siradaki Y-09. Testler Y-12de.
Son kayit AKTIF_GOREV.md 0.11; Maven veya test kosusu yapilmadi. Untracked girdileri koru.

---

# Guncel devir — Y-07 gelistirmesi

Y-01..07 gelistirme teslimleri kayitli. Siradaki Y-08. Testler Y-12de.
Aktif liste AKTIF_GOREV.md 0.3, son kayit 0.10. Untracked girdileri koru.

---

# Guncel devir — Y-06 kaynak baglari

Y-01..06 gelistirme teslimleri kayitli. Siradaki Y-07. Testler Y-12de.
Canli kurum servisleri cagrilmadi; kapsam/kanit AKTIF_GOREV.md 0.9.
Korunan untracked girdileri otomatik stage etme.

---

# Guncel devir — Y-05 gelistirmesi

Y-01..05 gelistirme teslimleri kayitli. Siradaki Y-06. Testler Y-12de.
Aktif liste AKTIF_GOREV.md 0.3; son kaynak kaydi 0.8. Untracked girdileri koru.

---

# Guncel devir — Y-04 gelistirmesi

Y-01..04 gelistirme teslimleri kayitli. Siradaki Y-05. Tek liste AKTIF_GOREV.md
0.3, son kayit 0.7. Testler Y-12de; korunan untracked girdileri stage etme.

---

# Guncel devir — 18 Ekim 2026 Y-03 kaynakli yanit

Y-01 kuruldu; Y-02 ve Y-03 gelistirme teslimleri kaydedildi. Siradaki is Y-04'tur.
Tek aktif liste AKTIF_GOREV.md 0.3, Y-03 kaydi 0.6'dadir. Isaretler test kabulunu
veya kanonik Work kapanisini gostermez. Pytest, CLI integration senaryolari ve
gercek embedding/semantic kabul Y-12 final asamasinda bekler.
Y-03: ask duz metninde proje/locator kaynaklari, citation/kapsam butunlugu ve
sanitize storage/config hatalari baglandi. Mevcut untracked RAG dosyalari ve Java
target dizini korunuyor; otomatik stage etme. Her anlamli asama Turkce ASCII
commit + origin/main push ile kaydedilir. Asagidaki devir notlari tarihseldir.

---

# Guncel devir — 18 Ekim 2026 Y-02 gelistirmesi

Y-01 kuruldu, Y-02 gelistirme teslimi kaydedildi. Siradaki is Y-03'tur.
Tek aktif liste AKTIF_GOREV.md 0.3; bu isaretler Work kapanisi veya native kabul
degildir. Y-02 gercek oturum/skill/inert davranis testleri final Y-12'de bekler.
AGENTS.md mevcut yerel executable fallback'ini tasir; root shim'ler ayni kaynaga
baglidir. Klasor acilisinda otomatik kurulum/init yapma. Onceki devir satirlari
asagida tarihseldir; gercek HEAD ve store durumu yeniden okunur.

---

# Guncel devir — 18 Ekim 2026 Y-01 kurulumu

Kullanici devam talimati verdi. Y-01 kurulum teslimi kaydedildi; siradaki adim Y-02.
Varsayilan `.zekam` artik layout/SQLite/config/CAS ve bootstrap receipt ile mevcut.
CLI `.zekam/yerel/python/Scripts/zekam.exe` ile kullanilir; kaynak repoya baglidir,
mevcut Python paketlerini kullanir. Model LOCALAPPDATA/Zekam/models altinda kalir;
yeniden indirme veya veri tasima yapilmadi. Dosya SHA256 kontrolu verified;
inference/pytest final Y-12'ye birakildi. User/global PATH ayari degismedi.
Gercek proje/Work kaydi henuz yok; sohbetten kayit veya authority uretme.
Tek guncel liste AKTIF_GOREV.md 0.3; kurulum kaniti 0.4. Init'i reset gibi tekrar
calistirma; mevcut kullanici verisini ve korunan untracked girdileri koru.

---

# Guncel devir — 18 Ekim 2026 kapsam sadelestirme

Tek guncel yapilacaklar listesi `AKTIF_GOREV.md` bolum 0.3, Y-01..12'dir.
Onceki 59 acik satir tarihsel referansa donustu; kapanmis/basarili sayilmadi.
Yerel veri hedefi `%USERPROFILE%\.zekam`, kaynak ayni gercek repository kokudur.
Bu revizyonda kurulum, test veya gelistirme baslatilmadi; hedef duraklamasi korunur.
Kullanici devam dediginde Y-01'den basla; testleri Y-12 final asamasinda calistir.
Her anlamli gelistirme sonunda Turkce ASCII commit + origin/main push yap.
Korunan untracked RAG dosyalari ve Java target dizinine bu plan degisikliginde dokunulmadi.
Eski devir notlarindaki sonraki gorev/HEAD talimatlari tarihsel referanstir; gercek
HEAD ve operational store yeni oturumda okunur. Sohbetten Work/lease yetkisi turetme.

---

# Model devir metni - 16 Ekim 2026 17:45 checkpoint

## Guncel devam notu

17:45 checkpoint: gelistirme durduruldu. Kullanici devam et demeden surdurme. Mevcut goal ve birikmis sureyi sifirlama; yeni goal olusturma. Son gelistirme HEAD 58cf14a, remote readback eslesti. Tam durum ve devam promptu docs/CHECKPOINT_2026-10-16_1745.md icindedir. Testler CALISTIRILMADI.

Devam sonrasinda WP-07 resume canonical readback gelistirmesi uygulandi: `work resume`
acik Work penceresindeki son eventleri, bounded Run ozetlerini ve guvenli bound job
referanslarini SQLite authority'den okur. Payload, owner token ve grant alanlari
disarida tutulur; cikti read-only/grants_authority=false olarak kalir. Regresyon
kaynagi eklendi, testler CALISTIRILMADI. Ayrinti docs/WORK_RESUME_CANONICAL_READBACK.md.

Aktif devam HEAD `701d2e5` ve `origin/main` ile eslesiyor. Sonraki model ayni
gercek kaynak kokunde devam etmeli; korunan untracked dosyalara dokunmamali ve
testleri tum gelistirmeler bitmeden calistirmamali.

WP-07 checkpoint kabul siniri eklendi: operational SQLite checkpoint payload'i
canonical JSON olarak en fazla 16 KiB ve bounded derinlik/alan sayisinda tutulur;
secret, credential, token, prompt, response, transcript, raw content ve private key
alan/degerleri reddedilir. `grants_authority` true kabul edilmez. Unit regresyon
kaynagi eklendi, testler CALISTIRILMADI. Ayrinti docs/WORK_CHECKPOINT_PAYLOAD.md.

Bound job resume readback'i source snapshot'in revision/tree/content/config digest
alanlarini da canonical SQLite'dan verir; snapshot ID varken eksik join fail-closed
reddedilir. Unit regresyon kaynagi eklendi, testler CALISTIRILMADI. Ayrinti
docs/WORK_RESUME_CANONICAL_READBACK.md.

Dogal dil Work status raporu da ayni bounded canonical bound job/source snapshot
okumasini proje basina donen acik Work penceresine baglar; scope ve truncation
alanlari acikca raporlanir. E2E regresyon kaynagi eklendi, testler CALISTIRILMADI.

Tekil dogal dil Work read de exact Work kapsaminda bound job/source snapshot
alanlarini verir; `exact-work` scope'u ve truncation durumu korunur. Unit/E2E
regresyon kaynaklari eklendi, testler CALISTIRILMADI.

`work show --json` dogrudan CLI yolunda da ayni exact-work bound job readback'ini
ve read-only/grants_authority false alanlarini verir. E2E regresyon kaynagi eklendi;
testler CALISTIRILMADI.

WP-06.05 dogal dil bagi eklendi: exact Jira anahtari ile degisiklik/gecmis sorusu
`zekam ask` icinde provider/RAG yerine bounded local Git history/diff okuyucusuna
yonlenir. Proje kapsami belirsizse clarification uretilir; E2E regresyon kaynagi
eklendi, testler CALISTIRILMADI.
History readback route ve scope digestleriyle deterministic kapsam kararina
baglandi.

Run ve checkpoint record'larinda, CLI/dogal dil readback'inde canonical
`source_snapshot_id` ve Run `config_revision_id` korunuyor; plan kaynak ve config
baglantisi projection katmaninda dusurulmuyor. E2E/unit regresyon kaynaklari
eklendi, testler CALISTIRILMADI.

`work show --json` exact Work icin bounded Run ozetlerini ve truncation bilgisini
aynı canonical SQLite readback'ine ekliyor; dependency ozeti de exact scope'ta
okunuyor ve salt-okunur kapsam korunuyor.

Salt-okunur Work/Run CLI yollarinda SQLite read-only transaction kullaniliyor;
provider veya Maven calisirken gereksiz `BEGIN IMMEDIATE` write lock tutulmuyor.
Mutasyon transactionlari degismedi; unit regresyon kaynagi eklendi, testler
CALISTIRILMADI.

`workspace_resume` ve bounded knowledge ref readback'i de store destekliyorsa ayni
read-only UoW yolunu kullanir; sahte/test store'lar icin eski okuma girisi korunur.

ODI preflight policy ile reddedilen topology/sensitive/secret dosyalarin raw byte hashini public plan files alanina tasimaz; content_digest null kalir. Kabul edilen dosya fingerprint ve rejected planin bind reddi korunur. CLI regresyon kaynagi hazir, CALISTIRILMADI.

ODI directory discovery limiti queue boyutu yerine toplam kesfedilen file/directory sayisina baglandi; islenmis bos dizinler artik butceden dusmez. Deep empty chain, mixed entry ve exact limit regresyon kaynaklari hazir, CALISTIRILMADI. Native/WP kabul acik.

Smart XML tek-kayit indekslerinde ayni sinif/kimlige ait celisen allowlist kayitlari lineage oncesinde reddedilir; celisen tekrar Field de reddedilir. Ozdes kayit ve composite relation parent tekrarlarini engellemez. Chunk profile sanitizer v2 ile eslendi. Regresyon kaynaklari hazir, CALISTIRILMADI; native/WP kabulu acik.

Smart XML DTD/entity guard, XML veri cikarimi ve SHA256 ayni bounded descriptor akisina baglandi. 128 MiB byte, 1 milyon element, 256 derinlik ve 200 bin object siniri; biten node detach ve kaynak identity/size/mtime readback eklendi. Sanitizer v2/xml_profile plan ve manifestte; yalniz Field allowlist korunur, final kaynak hash tekrar okunur. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek ODI/native kabul ve WP kapanisi acik. Ayrinti docs/ODI_SMART_XML_AKISI.md.

Smart Export canonical import/index source bagi eklendi: plan v2 exact original source identity, source snapshot contract ve destination library ref; typed admitted Work/Run/plan olmadan copy yok. Reserved project.odi-smart-import common claim/project lease/CAS/checkpoint/receipt ve pre-post source/private binding readback kullanir. Private bounded copy/fsync/hash/exclusive hard-link existing farkli/public raw dosyayi overwrite/adopt etmez; own temp identity ile cleanup yapar. Private binding history korunur, raw XML/absolute path CAS sonucuna girmez. Index load_smart_binding actual completed receipt/current mapping/source hash/private ownership ister; legacy/unbound veya missing receipt reddedilir, sanitizer/disclosure kapilari korunur. Completed replay original source yokken yeniden copy/parse/write yapmaz; unknown explicit recovery ister. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek native ODI/Work ve WP kabul acik. Ayrinti docs/ODI_SMART_CANONICAL_IMPORT.md.

ODI directory binding canonical Work/Run kaynak bagi eklendi: preflight plan v2 source snapshot contract manifest/tree + physical root digestini baglar. Typed admitted Run/Work revision/project/plan/snapshot olmadan yazma yok. Ortak project.odi-bind claim/project lease/CAS/checkpoint/receipt, source/destination pre-post readback ve no-write completed replay kullanir. Mevcut private binding exact bytes content-addressed private history olarak korunur; legacy receipt adoption yok. Salt okunur odi-binding-source current vs historical mappingi ayirir, absolute root/raw XML CASa alinmaz. Silent recovery rewrite kaldirildi; unknown explicit recovery ister. SQLite/CAS/XML/private file/CLI regresyon kaynaklari guncellendi, CALISTIRILMADI. Smart Export import/sanitized index canonical bagi ve gercek kabul sonraki dilimdir. Ayrinti docs/ODI_CANONICAL_BINDING.md.

Belge provider locator sozlesmesi sikilastirildi: OCR int/float/str ve structured text str donusumlerinin sahte citation/metin uretme yolu kaldirildi. Ortak Locator page/line/block/timestamp gercek int, bbox finite numeric ve ContentUnit text/order/confidence turleri dogrulanir. OCR threshold oncesi tam validation; structured exact DOCX/PDF enum, heading bounds ve PDF bbox korunumu eklendi. 16384 blok/16 MiB normalize cikti butcesi filtered bloklari da kapsar, cap+1 partial kabul edilmez. Generic parser surumu 2; valid serialized body bicimi degismedi. Regresyon kaynaklari hazir, CALISTIRILMADI. WP/native kaynak/secret/provenance kabul kapilari acik. Ayrinti docs/BELGE_PROVIDER_LOCATOR_SOZLESMESI.md.

Native publication host composition eklendi: actual evidence admission receipt -> tam plan/project/private root -> aktif ledger + exact transport + existing GitPublicationService. apply-admission preview approval istemez/uretmez; apply exact plan digest ve mevcut trusted authorization ister. Current admission/producer/review/native test source/raw report validator dispatch/claim, consumption sonrasi ve push sonrasi terminal oncesinde tekrar okunur; ignored tree/report drift recovery ister. Completed replay yeni Git/test/transport cagrisi yapmaz. Yeni apply yuzeyleri explicit LOCAL_EFFECT, preview read-only registry yolundadir. Canonical runtime/source fixture regresyon kaynaklari hazir, CALISTIRILMADI. Host key provisioning/principal/credential isolation ve gercek native cihaz kabulu halen acik. Ayrinti docs/PUBLICATION_NATIVE_HOST.md.

Publication test/verifier evidence bundle kaynak bagi eklendi: actual producer receipt -> ayni subject/binding/realm/native test job/receipt/Run -> tam evidence-bound plan. publication.evidence.bind claim ve project+global trust lease, current source/raw report/trust pre/post readback ile iki signed evidence kaydini tek SQLite transactioninda kabul eder; ikinci kayit hatasi ilk inserti rollback eder. Immutable canonical source scope/receipt/CAS, completed replay ve salt okunur evidence-admission reader baglandi. Gercek SQLite/CAS protokol regresyon kaynaklari hazir, CALISTIRILMADI; upstream producer fixture kabul sayilmaz. Approval/push, host signer confinement/provisioning ve native cihaz kapilari acik. Ayrinti docs/PUBLICATION_EVIDENCE_BUNDLE.md.

Trusted verification evidence producer kaynak bagi eklendi: actual completed publication.review accepted native session/upstream/commit scope -> current native test source/ham rapor/subject -> ayri verification-role host signer. publication.verification.attest claim-before-sign, project+global trust lease, admitted key signature ve source/trust post-effect readback; unknown recovery ve no-sign completed replay baglandi. verification-evidence salt okunur export ve importta actual producer receipt exact readback eklendi. Real SQLite/CAS/Ed25519 protokol regresyon kaynaklari hazir, CALISTIRILMADI; upstream review/current source fixture kabul sayilmaz. Host signer confinement/provisioning, native composition ve gercek cihaz/quality kabulu halen acik. Ayrinti docs/PUBLICATION_VERIFIER_PRODUCER.md.

Commit araligi unit-test scope bagi eklendi: historical source commit current HEAD atasi olmali; tum net Git farki yalniz accepted test A/M dosyalaridir. Baska dosya/silme/chmod/link/submodule/binary/invalid UTF-8 reddedilir. Bounded shell-free Git raw diff ve before/after blob OID/metni context ve plan digestine baglandi; immutable reader Git blob hashlerini tekrar hesaplar. Replace-object/inherited GIT override/external diff/textconv kullanilmaz; toplam Git okuma 30 sn, proof 150000 byte/512 dosya ve toplam context kapisi korunur. Yerel Git regresyon kaynaklari hazir, CALISTIRILMADI. Imzali verifier producer/native host confinement acik; WP kapanisi yok. Ayrinti docs/PUBLICATION_COMMIT_SONRASI_REVIEW.md.

Commit-sonrasi publication.review kaynak bagi eklendi: historical builder full tree/production/build/plan ile current native test subject eslesir; explicit reviewed profile tum builder upstreamlerinden farkli reviewer ister. Secili production ve kabul edilen test icerigi bounded secret-safe context ile exact budget/plan digestine baglanir. Ayri admitted Run, claim-before-model, native session ayrimi ve post-effect current source readback canonical receipt yolundadir. Salt okunur review-plan/review-source CLI eklendi; model veya signer cagrisi yapilmadi. Regresyon kaynaklari hazir, CALISTIRILMADI. Bu secili unit-change review scope'udur; quality/signing/publication authority degildir. Trusted verifier producer ve host signer confinement/native composition acik. Ayrinti docs/PUBLICATION_COMMIT_SONRASI_REVIEW.md.

Olculen full test-tree source bagi eklendi: actual Maven reviewed reactor src/test before/after fingerprint ve stable/drifted/unavailable projection; ignored resources kapsanir. Accepted/no-tests olcumde drift stale sayilir; failed/timeout sinifi korunur. Loop current tree portu verifier oncesi/sonrasi ve success oncesi reread yapar; missing port/okuma/drift success'i engeller, external dosya silinmez. Native ve batch CAS/registry/receipt refs ile source/verifier okuyucularina baglandi; eski ref yoksa rapor digest'i degismez. 5000 dosya/64 MiB aggregate/4 MiB tek dosya ve 128 modul limitleri; link/junction ve enumeration error silent omit yerine ret. Regresyon kaynaklari hazir, CALISTIRILMADI. Atomic filesystem veya quality/publication authority iddiasi yok; current HEAD/subject, trusted verifier producer ve host confinement/native composition kapilari acik. Ayrinti docs/OLCULEN_TAM_TEST_AGACI.md.

Canonical verifier-source okuyucusu eklendi: completed test.run input/binding/checkpoint/receipt -> gercek ledger final terminal/unique accepted attempt -> coverage re-evaluation -> actual CAS measurement/binding/source snapshot -> accepted builders ve final verifier context/model/profile/patch zinciri denetlenir. Loop measurement record/binding/report refs CAS'a, measurement/terminal/candidate/pre-postimage artifact registry'ye baglandi; existing input digest govdesi ayni algoritmayla outputta saklanir. Bounded no-scan reader legacy/missing/drift kaniti benimsemez; replay/model unknown bagimsiz kaliteye yukselmez. Regresyon kaynaklari hazir, CALISTIRILMADI. Tarihsel canonical anchor current HEAD/publication subject veya independent quality authority degildir; commit-sonrasi review, trusted verifier producer ve host signer/native composition kapilari acik. Ayrinti docs/CANONICAL_VERIFIER_KAYNAGI.md.

Verifier execution provenance kaynak bagi eklendi: canonical gateway gercek assignment/invocation/dispatch/artifact/context/instruction ve host configured model/upstream/profile baglarini direct CAS execution kaydinda tutar. Dependency artifact registry composition ve accepted builder list/final verifier refs terminal state/evidence'a tasindi; legacy completeness false kalir. Bounded no-scan reader deterministic request identity/digest/schema/role/context kontratini; comparator exact candidate/measurement, tum builder scope, distinct native execution ve configured upstream ayrimini denetler. Replay/model alias farki native/bagimsiz kalite kaniti sayilmaz; canonical terminal/quality authority false. Regresyon kaynaklari hazir, CALISTIRILMADI. Siradaki katman completed test.run/terminal/attempt -> publication candidate/subject -> trusted verifier producer. Host signer confinement/native composition kabul kapilari acik. Ayrinti docs/TEST_AGENT_EXECUTION_KANITI.md.

Host-owned test evidence producer kaynak bagi eklendi: typed signer capability, actual canonical measurement/current publication source, active role/realm/config/pin ve publication.test.attest claim-before-sign; project+global trust lease, exact public-key signature, source proof/CAS/checkpoint/receipt ve claim time readback. Completed replay yeniden imzalamaz; drift/unknown effect recovery ister. Salt okunur test-evidence export ve importta actual producer receipt yeniden okuma baglandi; CLI private key veya generic sign-JSON sunmaz. Regresyon kaynaklari hazir, CALISTIRILMADI. Gercek host key confinement/provisioning/native worker composition ve independent verifier producer halen acik; yayin/quality authority veya WP kapanisi uretilmedi. Ayrinti docs/PUBLICATION_TEST_PRODUCER.md.

Publication test-source current kaynak okuyucusu: exact canonical native receipt/measurement ve yeni report-facts hedef modul sirasi; ayni project/Work/source snapshot/config; current Work/dependency readback ve private project root, temiz exact Git identity/HEAD/remote, Maven plan/config/launcher, production/test tree/class ve ham Surefire/JaCoCo/exec hash+parser readback baglandi. Pre/post reread partial basariyi reddeder; raw bytes saklanmaz. JVM reprobe/HTML/independent verifier/signing authority iddiasi yoktur. Pending subject preview typed plan/evidence_bound=false tasir; mevcut evidence/approval kapisi korunur. Regresyon kaynaklari hazir, CALISTIRILMADI. Ayrinti docs/PUBLICATION_TEST_KAYNAK_DOGRULAMASI.md. Siradaki signing producer ve gercek bagimsiz verifier execution baglari acik.

Native Maven rapor projection gelistirmesi: mevcut Surefire/JaCoCo parser sonuclari tekrar parse edilmeden sayisal ve secret/PII kontrollu report-facts CAS artifactine baglandi. Ham properties/testcase/output/session metni saklanmaz. Native attempt receipt projection digestini tasir; source-evidence module/toplam sayac, measurement/binding ve JaCoCo XML/exec bagini yeniden dogrular; legacy not-captured, red capture-rejected, missing captured CAS ise ret olur. Gercek SQLite/CAS ve parser fixture regresyon kaynaklari hazir, CALISTIRILMADI. Bu semantic projection ham rapor arsivi, bagimsiz quality veya publication authority degildir; exact current source/HEAD, trusted signing ve verifier producer baglari acik. Ayrinti docs/NATIVE_TEST_KANIT_KAYNAGI.md.

Native test source-evidence okuyucusu canonical job/checkpoint/CAS -> request/attempt/receipt -> measurement/binding/candidate tree baglarini salt okunur dogrular. Nondefault execution key, failed sonuc, bounded strict JSON ve kolon/body drift regresyon kaynaklari eklendi; CALISTIRILMADI. Tarihsel freshness guncel kaynak veya ham rapor/quality kaniti degildir; signing/publication authority false kalir. Ayrinti docs/NATIVE_TEST_KANIT_KAYNAGI.md. Immutable raw report, exact publication subject, independent verifier ve signer ownership baglari acik.

Kullanici checkpoint sonrasinda devam et dedi; kaynak gelistirmesi suruyor.
Asagidaki 07:40 duraklama bolumu tarihsel snapshot'tir. Yeni canonical tournament
selection dilimi actual paired sources -> local regrade/no-progress -> tek
native selector -> canonical receipt/report yolunu ekledi. Ayrinti:
`docs/TOURNAMENT_CANONICAL_SELECTION.md`. Testler CALISTIRILMADI.
Native usage dilimi host step_finish token component/estimated USD olcumunu
optional CAS artifact'e bagladi; tournament historical/new selector ayridir.
Ayrinti: `docs/OPENCODE_NATIVE_USAGE.md`. Testler CALISTIRILMADI. Billed cost,
provider request/hard-cap ve gercek native kabul kapilari halen aciktir.
Salt okunur trial-aggregate --native-usage raporu exact receipt/source zincirinden
model/baseline/candidate coverage ve complete totals uretir; frozen aggregate
digest'i degismez, partial/legacy total null kalir. Testler CALISTIRILMADI.
WP-12.04/05 kaynak mimarisi catalog/export ureticisi eklendi: current task records,
AST interface/literal table/test declaration ve source hashes tek girdidir.
Yerel Markdown/HTML/Mermaid/manifest/ZIP yalniz repo-disi yeni hedefe uretilebilir;
runtime/kabul authority degildir. Ayrinti docs/KANONIK_MIMARI_EXPORT.md.
Mimari writer disk readback ve --verify current source/file/hash/ZIP dogrulamasi
eklendi; imported manifest authority olmaz, stale/tampered/incomplete paket ret.
WP-12.06 explicit task REQ/CAP->WP->test source traceability eklendi; exact satir,
tracked hash ve AST declaration bagli, missing/unmapped kayitlar gorunur. Ayri
traceability.json canonical catalog/manifest/ZIP/readback kapsamindadir. Gercek
run/evidence/execution commit null, release chain incomplete kalir.
WP-06.05 actual history root diff ve partial kapsam dogrulugu gelistirildi;
docs/ISSUE_GIT_GECMISI_KAPSAMI.md. Regresyonlar CALISTIRILMADI.
Publication registry trust pin'i typed Settings/sanitized active config/provenance
yoluna baglandi; CLI current settings/active registry drift'ini reddeder. Persistent
pin ve CLI preference icin dotted/nested session bypass reddi eklendi. Actual
key/receipt producer halen acik; testler CALISTIRILMADI.
Ilk public registry enrollment canonical producer/CLI eklendi: mevcut kalici/
aktif pin ve Work/plan claim altinda exclusive private yeni dosya, fsync/readback,
exact receipt ve no-write completed replay. Existing target/rotation benimsenmez;
gercek signing key ownership/rotation, actual test/verifier producer ve native
enforcement halen acik. Regresyonlar CALISTIRILMADI; kullanici home'una yazilmadi.
Enrollment'in ortak runtime post-effect callback uyumsuzlugu kaynak incelemesiyle
duzeltildi: pre-phase absent, process-local own-write sonrasi exact file readback.
Post-effect drift kabul edilmez; regresyonlar CALISTIRILMADI.
Signed public registry rotation producer/CLI eklendi: old canonical receipt head/
lineage, old approval timed signature, new admitted pin, global lease, private
atomic replace ve post-effect/historical readback. Census unknown/fork/reset'i
reddeder; full chain bound asilirsa partial trust yoktur. Ayrinti
docs/PUBLIC_REGISTRY_ROTASYONU.md. Testler CALISTIRILMADI; gercek signing key
ownership/provisioning, actual test/verifier receipt producers ve native enforcement
halen acik. Bunlar siradaki kaynak/kabul baglaridir; full WP kapanisi uretilmedi.
Test/generator/verify CALISTIRILMADI. Siradaki denetim: kalan canonical kaynak baglari,
gercek trusted producer/native kabul ve WP-12 traceability gelistirmeleri.
Resume komutu bu ortamda ZEKAM_HOME hazir degil dedi; Work/lease durumu
uretilmedi. Hedef araci paused gorunuyor; API resume desteklemiyor, yeni hedef
olusturulmadi. Kullanici devam talimati kaynak gelistirmesi icin gecerlidir.

Bu belge guncel WP-02..12 gorevinin conversational devir notudur. Onceki
PostgreSQL donemi faz/test sayilari ve calisma talimatlari Git gecmisinde
kalir; guncel kapsam veya kabul kaniti yerine kullanilmaz.

## Tarihsel checkpoint durumu

- Durum: DURAKLATILDI. Kullanici 07:40 checkpoint/duraklama istedi.
- Son gelistirme: `70b84433b8e1902412cdd2f5049b58cc093ab24e`; origin/main
  readback eslesti. Checkpoint commit'i bu dosyayi iceren commit'tir.
- Ayrintili kayit: `docs/CHECKPOINT_2026-10-16_0740.md`.
- Kapsam authority'si `AKTIF_GOREV.md`, YAML yalniz generated projection.
- Plan: `docs/superpowers/plans/2026-10-15-remaining-development.md`.
- Tum hedef tamamlanmadi; bu turun sekiz kaynak dilimi UNTESTED.
- Testler tum gelistirmelerden sonraki tek final asamada yapilacak.

## Yeni oturuma verilecek prompt

```text
Zekam'in gercek kaynak koku C:\innova\projeler\zekam.
Once kullanicinin devam et izninin geldigini dogrula; duraklama suruyorsa yeni
is baslatma. Devam edildiginde mevcut hedef sayacini/hedef durumunu kontrol et;
calisan hedef varken yeni hedef olusturma. Hedef tum acik WP-02..12 kapsamidir.

AGENTS.md, 00_BASLA.md, DEVAM_PROTOKOLU.md, AKTIF_GOREV.md ve
docs/CHECKPOINT_2026-10-16_0740.md dosyalarini oku. YAML'i elle degistirme;
ActiveTaskContract ile uret. Git branch/HEAD/remote/dirty durumunu ve
zekam resume --json ile mevcut Work/lease/recovery durumunu yeniden oku.
Markdown'dan claim, receipt veya kapanis uretme. Eski test sayilarini yeni
commit'lerin kaniti sayma.

Son gelistirme 70b8443. Bu devam turu 8d4837d/e666937/8760274/77ce074/1c4a602/
ed8f43e/8825714/70b8443 ile control gecmisi, usage kapsam dogrulugu, native scheduler
kabul bagi, radar read/claim/replay, read-only composition ve tournament
contract kontrolleri ile Radar connection kapanisini kaynakta guclendirdi.
Regresyon kodu var; testler CALISTIRILMADI. Her biri origin/main'e push edildi.

Siradaki somut kaynak isi WP-10.05 actual tournament akisi: typed plan/selection
katmanini mevcut canonical Work/claim/checkpoint/receipt ve actual dispatch'e,
fixture/baseline olcumune ve butce/no-progress stop'a bagla. Mevcut skill paired
trial/runtime oruntulerini yeniden kullan. Model adini bagimsizlik kaniti veya
bounded exact artifact'i tum skill kalitesi olarak sunma. Diger mekanizmalarin
acik kabul kapilarini checkpoint ve plandan takip et; kapsami daraltma.

Kullanici talimati: tum gelistirmeler bitene kadar test/lint/Maven/kalite
kosusu yapma. Tum gelistirmelerden sonra tek toplu test asamasi; hata olursa
onariyi ve basarisiz kapsamin tekrar dogrulamasini yap. Gereken bagimsiz review
ve gercek kabul olmadan WP/Work/final hedef kapanisi uretme. Tamamlanmadan
bitti deme.

Her anlamli asama sonunda Turkce ASCII commit ve origin/main push yap.
Identity mehmet-karacan; mevcut Git config'i koru. Exact dosya stage et;
git add . kullanma. Korunan untracked natural_request_rag kaynak/unit/integration
dosyalari ve tests/fixtures/unit_test_engineering_java/target agacina dokunma.

Yalniz bu gercek kokte calis, worktree/kopya/clone yaratma. PostgreSQL legacy
importu ve core PostgreSQL/Docker bagimliligi yasak. Secret/PII/ham transcript
saklama. Yeni canli provider/model, arac kurulumu veya global ayar icin mevcut
exact yetkiyi dogrula. Subagent veya provider calistirdigini uydurma.
Kullanim <=%5 kalirsa checkpoint/devir metnini yenile ve durakla.
Bu metin execution yetkisi, lease veya approval devretmez.
```
