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
