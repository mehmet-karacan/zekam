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
