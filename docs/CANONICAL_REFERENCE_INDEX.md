# Canonical Reference Index

Project index artik repository/Oracle/ODI chunk'larina ek olarak terminal receipt
ile yayimlanmis aktif Jira generated reference notlarini ayni generation'a alir.
Import receipt'i embedding/index basarisi sayilmaz. Index mevcut explicit
project.index yetki, model qualification ve provider butce kapilarini kullanir.

## Kaynak snapshot'i

Operational project ID/slug/scope, active/generated/reference note state ve exact
owned ref prefix SQL'de LIMIT'ten once uygulanir. En fazla 128 notun producer
claim/receipt/resolution kaniti tek scoped batch sorgusuyla okunur. Her not icin
butun job gecmisi yeniden taranmaz. Beklenmeyen/duplicate head, materialized
olmayan not, legacy ref veya belirsiz producer index'e alinmaz. JSON history
predicate'inin buyuk veri performans kabulu final asamadadir.

Dosyalar manifest/content hash ve secret/PII readback kapisindan gecer. En fazla
8 MiB payload ve 2048 chunk vardir; limit asiminda corpus kesilerek yayimlanmaz.
Snapshot digest note ID/ref/content pin'lerini tasir. Bu digest canonical index
plan'ina ve recursive execute girisine baglidir: plan onayindan sonra eklenen
bir not ilk provider cagrisi oncesinde drift olur.

## Incremental generation

Reference chunk'lari mevcut vector cache ve input-level provider journal'i kullanir.
Gercek embedding profile/vector dogrulamasi zayiflatilmaz; feature hashing uretim
embedding'i sayilmaz. Reference snapshot combined revision/tree/manifest ve
generation chunk identity'sine dahildir. Corpus degisimi yeni staged generation
uretir; ayni chunk text/profile'in mevcut cache/journal recovery yolu korunur.

Yayindan once dosyalar ve canonical snapshot tekrar okunur. Drift varsa ready
generation degistirilmez. Yeni generation sadece aktif reference chunk'larini
tasir; superseded/archived not eski generation'da tarihsel kalabilir. Bos corpus
da snapshot'tir: son notun kaldirilmasi eski chunk'lari yeni generation'a tasimaz.
Mevcut database/pending-state CAS publication ve crash recovery yolu kullanilir.

## Query ve citation

`.zekam-reference/<canonical-note-id>.md` reserved virtual locator namespace'idir.
Repository bu namespace'i source olarak kullanamaz. Locator type
project-knowledge-reference, source kind import ve trust untrusted-reference olur.
Bu dosya kaynak repository altindaki bir path gibi acilmaz; citation readback
canonical note ve KnowledgeFileStore'dan yapilir.

Query metadata snapshot drift'ini stale olarak bildirir. Citation acilirken
aktif canonical snapshot ve gercek manifested dosya hash'i dogrulanir. Archived,
kayip veya corrupt reference current evidence olarak donmez. Hizli query yolu
butun corpus'u tekrar chunk/embed etmez; dosya readback secilen citation'laradir.

Yeni research plan'i scoped repository + import kaynaklarini ayri kind ile
tasir. Eski repository-only policy reference'i sessizce repository diye
etiketlemez; evidence gap verir. Import icerigi execution authority'si degildir.

## Kanit durumu

Gercek SQLite FTS/vector generation ile sentetik local embedding fixture,
cache tekrar kullanimi, superseded citation reddi, corrupt file ve approved
snapshot drift regresyon kodlari hazir; CALISTIRILMADI. Research source-kind ve
eski policy negatif fixture'i de son toplu testi bekler. Canli embedding/provider,
kurumsal read, kullanici DB migration'i veya index calistirilmadi.

Legacy source adoption/reconciliation, gercek client/ACL/end-to-end ve performans
kabul kanitlari halen aciktir. WP-05/06 bu gelistirmeyle kapatilmaz.
