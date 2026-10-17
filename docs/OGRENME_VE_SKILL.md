# Öğrenme döngüsü, skill yaşam döngüsü ve ölçülü iterasyon

## Ders kanıttan türer

Bir başarısızlık ders üretmez; **doğrulanmış kök neden** üretir.

```text
gozlem -> ayni kanit tekillestirme -> dogrulanmis kok neden -> ders adayi
  -> bagimsiz verifier -> test | eval | guidance | skill
```

- **Aynı kanıt iki kez sayılmaz.** İki farklı run aynı `evidence_digest` üretiyorsa
  bu tek gözlemdir. Kural hem alanda (`distinct_observations`) hem veritabanında
  (`occurrence_evidence_unique`) geçerlidir.
- Kök neden doğrulanmadan ders üretilmez. Kök neden üçlüsü (ifade, doğrulayan,
  kanıt digest'i) ya birlikte doldurulur ya hiç doldurulmaz.
- En az **iki bağımsız gözlem** gerekir; tek olay ancak açıkça `critical`
  işaretlendiğinde ders üretebilir.
- Ders verifier'ı yazarla aynı kimlik olamaz (alan + constraint).

## Skill yaşam döngüsü

```text
candidate -> evaluated -> active -> deprecated -> retired
```

**Skill kendi kendini aktif registry'ye yazamaz** — `self_promoted` alanı ve
`skill_no_self_promotion` constraint'i bunu birlikte engeller.

Aktivasyon dört kapıyı birden ister:

| Kapı | Zorlanma yeri |
|---|---|
| Ölçüm var | alan + `skill_active_requires_gates` |
| Ölçüm baseline'ı geçiyor | alan + `skill_requires_improving_evaluation` trigger |
| Bağımsız onay (yazar ≠ onaylayan) | alan + constraint |
| Rollback planı boş değil | alan + constraint |

Değerlendirme en az **beş deneme** ister; değerlendiren ve doğrulayan kimlikler
ayrı olmak zorundadır. Aynı gövdeye sahip skill adayları tekilleştirilir
(`skill_body_unique`).

## Ölçülü döngü

Döngü sınırsız dönmez. Durdurucular:

| Sebep | Koşul |
|---|---|
| `goal-reached` | **doğrulanmış** sonuç hedef skoru geçti |
| `iteration-budget` | iterasyon sayısı doldu |
| `cost-budget` | maliyet bütçesi doldu |
| `no-progress` | son N iterasyonda en iyi skor iyileşmedi |
| `blocked` | harici blocker |

Doğrulanmamış bir başarı hedefi kapatmaz — "model başarılı dedi" yeterli değildir.

## Bağlam etkinliği ve geri bildirim

Her çalışma bağlam manifest'i için token maliyeti, kullanılan kanıt sayısı ve
**doğrulanmış** başarı bilgisi üretir. Bunlar route kararına girer ama
`grants_authority` her zaman `false`'tur: ölçüm bir sinyaldir, yetki değil.

`RouteFeedback` doğrulanmış başarı oranı, ortalama token maliyeti ve kanıt
yoğunluğu (kanıt/kilotoken) üretir.


## Yerel bellek duzeltme ve arsivleme — Y-09

Native ajan kullanicinin bellek isteginde kaydi ve revision'i mevcut `memory
inspect`/`candidates` ciktisindan cozer; kullanicidan digest veya JSON istemez.
Duzeltme yeni aday/review ve `memory promote --supersedes <onceki-revision>` ile
exact onceki revision'a baglanir. Kayitlar ezilmez; gecmis ve provenance korunur.
Arsivleme `memory archive <memory-id>` salt-okunur planindan alinan exact digest
ile `--uygula --plan-digest` kullanir; claim/receipt uretir. Arsiv kaydi v3 learning
DB'de append-only'dir. Ayni revision ayni actor/reason ile tekrar uygulanirsa yeni
kayit uretilmez; stale head veya farkli replay metadata'si reddedilir.

Arsivlenmis revision `search`/`active_records`/`is_active` sonucunda aktif degildir;
inspect, status ve revision listesi archived durumunu ve archive_digest bagini
korur. Eski candidate, review, revision ve head baytlari silinmez veya degismez.
Yeni bir onayli supersession kendi yeni revision'iyle aktif olabilir.

Mevcut learning v2 icin `memory prepare` yalniz plan uretir. Acikca uygulaninca
v2->v3, exact hedef kimligi/icerik digest'i ve SQLite online backup ile yapilir;
oncesi verinin logical paritesi ve integrity/FK sonuclari okunur. V1 kullanicisi
once mevcut `skill prepare` v1->v2 yolunu kullanir. Yukseltme arka planda veya
`status`/`search` sirasinda uygulanmaz; yedek ezilmez, yabanci hedef benimsenmez.
Yeni home v3 baslar; v2 bellek/skill okumasi ve mevcut islevleri korunur, archive
icin explicit prepare gerekir. Geri donus yedegi ayri yeni hedefe restore ile
planlanir; guncel kullanici DB'si yerinde silinmez.

Bu Y-09 kaynak teslimidir; regresyon, migration, archive/replay ve tam native
kabul kosulari Y-12 final asamasindadir. Otomatik evolution/radar acilmadi.
