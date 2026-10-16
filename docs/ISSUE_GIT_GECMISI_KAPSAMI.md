# Issue Git history evidence scope - WP-06.05

`project_history_query.py` mevcut exact issue-key ve pinned yerel HEAD yolunu
korur. Bu kaynak diliminde ilk commit, salt okunur `diff-tree --root` ile bos
agaca karsi incelenir; sabit SHA-1 empty tree hash'i veya Git object yazmasi yoktur.
SHA-1 ve SHA-256 commit/parent kimlikleri kabul edilir. Normal commit ve merge
first-parent diff kullanir; `diff_basis` bunu raporda gosterir.

20 commit, commit basina 3 dosya, 2000 hunk satiri ve 6000 karakter sinirlari
korunur. Secret taramasi truncation'dan once tam bounded diff cevabina uygulanir;
external diff/textconv kapali, dosya secimi literal pathspec'tir. Fetch, network,
checkout veya kullanici kaynak yazmasi yapilmaz.

`partial` artik yalniz shallow/commit-list sinirindan ibaret degildir: omitted
dosyalar, truncated hunk/subject, redacted subject/content veya binary-unavailable
icerik commit ve overall sonucu partial yapar. File diff_state text, metadata-only,
redacted, omitted veya binary-unavailable olarak raporlanir. Binary summary bos
metin diff'i gibi sunulmaz. Uzun subject'te issue anahtari display truncation'dan
once aranir; subject match yanlislikla body match olmaz.

Bu sonuc issue label'li commit'lerin bounded historical kanitidir. Issue API'si,
tum branch'ler, butun issue kapsam degisiklikleri veya guncel davranis kabulu
sayilmaz. Git history sonucu issue yazma veya Work kapanisi yetkisi vermez.

Dogal dil `zekam ask` exact issue anahtari ve degisiklik/gecmis ifadesini birlikte
tasidiginda bu ayni local history okuyucusuna yonlenir. Proje explicit verilirse
scope exact projeye sabitlenir; birden fazla olasi proje varsa clarification
verilir. Bu yol provider veya index cagrisi yapmaz ve `read_only=true` tasir.

Root addition, real binary Git diff, file/hunk truncation propagation, secret
redaction, long subject ve root SHA-256 command projection regresyon kaynaklari
hazirlandi. Testler kullanici talimatiyla CALISTIRILMADI; final toplu asamada
calistirilacak. Native/live issue ve genel WP kabul kapilari aciktir.
