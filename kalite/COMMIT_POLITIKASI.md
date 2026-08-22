# Commit Politikası — Türkçe Anlam, ASCII Karakter

## Amaç

Commit geçmişi teknik değişikliği, nedeni, kanıtı ve geri dönüşü Türkçe olarak açıklamalı;
platform uyumu için yalnız ASCII karakter kullanmalıdır.

## Başlık

```text
<tur>: <kisa emir cumlesi>
```

Türler:

```text
ozellik
duzeltme
yeniden-duzenleme
test
belge
altyapi
guvenlik
performans
gecis
bakim
```

Örnek:

```text
ozellik: is grafigi icin kalici lease ekle
duzeltme: eski fencing token ile sonucu reddet
test: claim ve receipt kurtarma senaryosunu dogrula
belge: model benchmark akisini acikla
```

ASCII olmayan Türkçe harf kullanma:
- `ç` → `c`
- `ğ` → `g`
- `ı` → `i`
- `İ` → `I`
- `ö` → `o`
- `ş` → `s`
- `ü` → `u`

İçerik Türkçe anlamlı kalır.

## Gövde

```text
Neden:
- Degisikligin cozmeyi hedefledigi sorun.

Degisiklik:
- Yapilan teknik degisiklikler.

Kanit:
- Calistirilan test, eval, migration veya verifier sonucu.

Risk:
- Bilinen risk ve sinirlar.

Geri donus:
- Guvenli geri alma adimi.
```

## Kurallar

- Başlık 72 karakter hedefi.
- Emir kipi ve somut kapsam.
- “update”, “fix stuff”, “wip”, “misc” yasak.
- Sadece issue ID başlık olmaz.
- Test geçmeden commit yok.
- Generated/format-only unrelated değişiklik aynı commit'e karıştırılmaz.
- Migration ve code compatibility aynı commit veya açıklanmış sıralı commit olabilir.
- Secret, endpoint credential, personal path yok.
- Co-author/model adı zorunlu değildir; execution evidence Work/Run'da tutulur.
- Push varsayılan deny.

## Commit hook

CI/local hook:
- UTF-8 bytes içindeki non-ASCII karakteri reddeder,
- izinli tür ve başlık formatını doğrular,
- gövde zorunlu bölüm policy'sini risk bazlı uygular,
- merge/revert generated Git mesajlarına controlled exception verir.

## Örnek tam mesaj

```text
ozellik: model benchmark icin kalici claim ekle

Neden:
- Tekrar calistirilan benchmark ayni maliyeti yeniden olusturuyordu.

Degisiklik:
- Plan digestine bagli claim ve terminal receipt kayitlari eklendi.
- Receipt olmayan claim recovery-required durumuna alindi.

Kanit:
- Ayni planin ikinci kez model cagirmadigi integration testi gecti.
- Worker crash senaryosu PostgreSQL kabul testinde dogrulandi.

Risk:
- Eski benchmark kayitlari yeni claim alanlarini tasimiyor.

Geri donus:
- Yeni route feature flag ile kapatilir ve onceki okuyucu kullanilir.
```
