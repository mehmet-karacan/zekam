# Context Compiler ve Continuity

Context Compiler ham transcript veya model çıktısı taşımaz. Adayları logical kimlik,
source revision, content/evidence digest, authority sınıfı, freshness ve token maliyetiyle
değerlendirir. Required adaylar önce yerleştirilir; required toplamı bütçeyi aşıyorsa işlem
fail-closed biter. Diğer adaylar authority-first, freshness-second ve kimlik tie-break sırasıyla
seçilir. Her dışlama `budget-exhausted`, `stale`, `insufficient-authority` veya `superseded`
nedeni taşır. Model Decision ve benchmark sonuçları yalnız logical ref ve digest olarak girer.

WorkJournal append-only zincirdir. Sequence, previous digest, payload digest ve truncation bayrağı
entry digest'e dahildir. PostgreSQL optimistic head kontrolü eşzamanlı stale writer'ı reddeder;
update/delete trigger'ları geçmişin değiştirilmesini engeller.

Checkpoint, bağlandığı task planın bütün adımlarını completed ve pending arasında exact partition
eder. Her completed adım exact result digest ister; plan ve checkpoint source revision aynı
olmalıdır. `payload.meaningful_step=true` işaretli job, kendisine bağlı checkpoint bulunmadan
`completed` olamaz.

ContinuitySnapshot ve FinalizedHandoff authority, aktif lease, approval, authorization, secret,
absolute path veya transcript taşımaz. Client/model değişiminde yalnız bounded first reads,
safe actions ve evidence digest'leri kullanılır. Yeni worker Work/lease/authorization durumunu
kanonik repository'den yeniden edinmek zorundadır; handoff bunu devralmaz.

## ResumeCoordinator prepare

`zekam work resume-plan <proje> <is-ref> --client <istemci> --json` checkpoint v2
head'ini ve current Work/Task Plan/routing context/migration/journal durumunu tek
`REPEATABLE READ, READ ONLY` PostgreSQL snapshot'ında okur. Çıktı; selected checkpoint,
stale dimension reason code'ları, receiptless effect reconciliation aksiyonları,
yeniden edinilmesi gereken lease/resource-lock/authorization gereksinimleri, exact
sonraki step DAG'i ve `resume_plan_digest` taşır.

`prepare` plan kaydetmez, audit veya queue satırı yazmaz, effect başlatmaz ve mevcut
lease/approval/authorization'i devralmaz. Receiptless effect varsa normal retry/dispatch
üretmez; `recovery-required` ile fail-closed kalır. Migration/integrity drift'i insan
incelemesine, source/dependency/plan drift'i replan'a, yalnız context/route drift'i
recompile'a gider. Planın uygulanması ayrı bir mutation protokolüdür ve P0-012 kapsamında
exact plan digest revalidation ile ele alınır.

## ResumeCoordinator apply

`apply`, yalniz `safe-continue` disposition'li ve gecerlilik penceresi dolmamis
bir plan kabul eder. Ayni Work icin mutation transaction'i advisory lock ile
siralanir. Daha once ayni `resume_plan_digest` uygulanmissa exact actor,
authorization, client ve effect kapsami dogrulanir; kayitli saga event'i doner ve
ikinci dispatch yapilmaz.

Yeni uygulamada plan kanonik snapshot'tan yeniden hazirlanir ve supplied digest ile
exact eslestirilir. Drift varsa authorization tuketilmez. Fresh planda one-shot
authorization tuketildikten sonra yalniz planin exact job'i yeni attempt, lease,
fencing token ve logical lock'larla claim edilir. Onceki attempt herhangi bir effect
claim tasiyorsa normal reclaim yasaktir; receipt durumuna gore reconciliation gerekir.

Dispatch'ten once canonical assignment/invocation, `bound-v2` execution envelope ve
effect claim kalicilastirilir. Envelope gercek `DispatchRequest` payload digest'ini,
run deadline'ini ve checkpoint v2 kimlik/digest'ini tasir. Saga event zinciri
claim -> dispatch -> terminal sirasi, previous digest ve DB tarafinda yeniden
hesaplanan event digest ile append-only'dir. Adapter sonucu bilinmiyorsa job
`recovery-required` olur ve sessiz retry yapilmaz. Basarili apply terminal kaydi,
dispatch receipt'i ile yeni step sonucunu ayni checkpoint v2 revision'ina baglar;
checkpoint DB completeness kapisi gecmeden job `succeeded` olamaz. Ardindan exact
fence ile job/attempt terminal olur ve ephemeral lease/resource lock temizlenir.
Claimed veya dispatched event'te kesilen replay'de canli lease varsa istemciye
acik in-flight durum doner; lease dolmussa adapter tekrar cagrilmaz, append-only
recovery eventi yazilir ve job `recovery-required` olarak terminalize edilir.
High/critical assignment icin tarihsel verifier invocation/receipt'i yeni sonuca
devredilmez. Current result ve execution envelope'a exact post-result verifier
binding'i kanonik olarak yoksa completion checkpoint'i uretilmez; receipt korunur,
is policy recovery'ye alinir ve yeniden dispatch yasak kalir.
