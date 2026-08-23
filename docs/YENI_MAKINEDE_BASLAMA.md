# Yeni makinede başlama (macOS / Linux / Windows)

Bu belge Zekam'yi başka bir makinede sıfırdan çalışır hâle getirip **kaldığı yerden
devam etmek** içindir. Ürün kuralları `NIHAI_UYGULAMA_PROMPTU.md` ve referans verdiği
sözleşmelerdedir; bu belge onların yerine geçmez.

## 1. Depoyu al ve ortamı kur

```bash
git clone https://github.com/mehmet-karacan/zekam.git
cd zekam
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[db,api,dev]"
```

Gereksinimler: Python >= 3.12, Docker Compose v2+, Git 2.40+.

macOS'ta sistemin `python3` komutu 3.9 olabilir; sürümü açıkça verin
(`python3.12 -m venv`). Başka bir makineden kopyalanmış `.venv` taşınmaz —
zip veya yedekle geldiyse silip yeniden kurun.

## 2. Veritabanını başlat

```bash
cp compose/.env.example compose/.env
```

`compose/.env` içindeki `ZEKAM_DATABASE_PASSWORD` değerini kendiniz belirleyin —
bu dosya sürüm kontrolüne **girmez**. Boşta bir port seçin (`ZEKAM_DATABASE_PORT`,
varsayılan 5433).

```bash
docker compose -f compose/docker-compose.yml --env-file compose/.env up -d
```

## 3. Kabuk ortamı

Parola yalnız ortam değişkeninden okunur; hiçbir dosyaya yazılmaz.

```bash
export ZEKAM_DATABASE_PASSWORD='<compose/.env icindeki deger>'
export ZEKAM_DATABASE_PORT=<secilen port>
export ZEKAM_TEST_DATABASE_HOST=127.0.0.1
export ZEKAM_TEST_DATABASE_PORT=<secilen port>
```

`ZEKAM_TEST_DATABASE_*` verilmezse PostgreSQL kabul testleri **atlanır** — kapılar
yeşil görünür ama gerçekte çalışmamıştır. Test yaparken bunları mutlaka verin.

Yukarıdaki dört değişkenden fazlasını export etmeyin. Özellikle
`compose/.env` dosyasını `source` **etmeyin**: dosya `ZEKAM_DATABASE_NAME` ve
`ZEKAM_DATABASE_USER` da taşır, bunlar kabukta durursa CLI geliştirme
veritabanını hedefler. Test tarafı artık bu değişkenleri kendi içinde siliyor
(`tests/conftest.py`, autouse `clean_environ`), ama elle çalıştırdığınız
`zekam` komutları korumasızdır.

## 4. Migration ve sağlık

```bash
.venv/bin/zekam init
.venv/bin/zekam db upgrade --uygula
.venv/bin/zekam doctor
```

`zekam init` ZEKAM_HOME yerleşimini kurar; atlanırsa `doctor` `core.home-layout`
ve `storage.object-store` için `degraded` döner.

Migration'dan hemen sonra `doctor` `degraded` döner: üç runtime kontrolü kanonik
kayıt boş olduğu için sarıdır. Üçünü de tek tek kapatın:

```bash
.venv/bin/zekam policy init --uygula
.venv/bin/zekam model inventory --uygula
.venv/bin/zekam scheduler init --uygula
```

Bundan sonra `Toplam durum: healthy` görmelisiniz. Tek istisna
`runtime.clients`: yapılandırılmış istemci yoksa `skipped` döner ve bu genel
durumu bozmaz.

## 5. Kapıları doğrula

```bash
.venv/bin/python scripts/kalite.py --gorev <ZEKAM-Pxx>
.venv/bin/python scripts/paket_dogrula.py
```

Altısı da (`bicim`, `lint`, `tip`, `test`, `bagimlilik`, `olu-kod`) geçmeden faz
kapanmaz. `--cevrimdisi` ağ isteyen `bagimlilik` kapısını atlar ve atlamayı görünür
kılar.

## 5.1 Doğrulanmış platformlar

`ZEKAM-DOD-001` bu tabloya dayanır. Her satır gerçek bir temiz kurulum
tatbikatıdır; kanıt `.zekam/evidence/` altındadır.

| Platform | Tarih | Sonuç |
|---|---|---|
| Windows (`MINGW64_NT-10.0-26200`) | 2026-08-20 | altı kapı geçti |
| macOS `Darwin 25.6.0` arm64, Python 3.12.14 | 2026-08-21 | altı kapı geçti, `doctor` healthy |
| Debian 13 trixie arm64, Python 3.12 (konteyner) | 2026-08-21 | sıfırdan migration, altı kapı geçti, `doctor` healthy |

Linux ayağı `python:3.12-slim` konteynerinde, ayrı bir veritabanına karşı
sıfırdan migration ile koşuldu.

## 6. Nerede kaldığımızı öğren

Kanonik olmayan projeksiyonlar: `AKTIF_GOREV.yaml` ve `AKTIF_GOREV.md`. Bunlar
**iddia**dır; kanıt `.zekam/` altındadır ve `.gitignore`'dadır, yani klonla gelmez.
Bu yüzden yeni makinede gerçek durumu koddan ve kapılardan doğrulayın:

```bash
git log --oneline -5
.venv/bin/zekam db status          # migration head
.venv/bin/python scripts/kalite.py --gorev <ZEKAM-Pxx>
```

## 7. Modele verilecek başlangıç promptu

Aşağıdaki metni olduğu gibi yeni oturuma yapıştırın:

```text
Bu depo Zekam. Once 00_BASLA.md dosyasini uygula, sonra Global Definition of Done
tamamlanana kadar kaldigin yerden devam et.

Baslangic:
- git log --oneline -5 ile son commitleri oku.
- AKTIF_GOREV.yaml icindeki current_task ve next_safe_action alanlarini oku;
  bunlar projeksiyondur, kanit degildir.
- python scripts/paket_dogrula.py ve scripts/kalite.py --gorev <aktif-faz> calistir.
- kalite/UYGULAMA_IS_GRAFIGI.yaml icindeki aktif fazin task kabul kriterlerini oku.
- Markdown'daki "tamamlandi" ifadesini tek basina kabul etme; kod, test ve
  migration onceliklidir.

Calisma bicimi:
- Her faz icin: migration -> domain -> service -> repository -> CLI -> testler
  (unit/integration/security/e2e) -> dort kapi -> belge -> projeksiyon -> kanit
  -> SHA256SUMS -> commit -> push.
- Commit mesaji Turkce anlamli ve yalniz ASCII; baslik "<tur>: <kisa emir cumlesi>",
  govde Neden/Degisiklik/Kanit/Risk/Geri donus bolumlerini tasir.
- Push bu depo icin acikca yetkilendirilmistir (kullanici: mehmet-karacan).
- Agentic isde en az bir gercek subagent; koordinator sayilmaz.
- Testler mock degil gercek PostgreSQL, gercek bagli source repository ve gercek alt surec
  kullanmalidir.

Ortam:
- ZEKAM_DATABASE_PASSWORD ve ZEKAM_TEST_DATABASE_HOST/PORT disaridan verilir.
- compose/.env surum kontrolunde degildir; docs/YENI_MAKINEDE_BASLAMA.md'ye bak.
```

## 8. Devir sınırları

`.zekam/` altındaki checkpoint, continuity ve kanıt dosyaları klonla **gelmez**.
Yeni oturum bunları yeniden üretir; hiçbiri yetki devretmez. Yeni worker Work,
lease ve authorization durumunu kanonik kayıttan yeniden edinmek zorundadır.
