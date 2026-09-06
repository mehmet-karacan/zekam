# Zekam Nihai Uygulama ve Devam Paketi

Bu paket, boş veya yeni oluşturulmuş bir `zekam` repository'sine yerleştirilerek Zekam'nin
modelden, istemciden ve oturumdan bağımsız biçimde uygulanmasını yönetmek için hazırlanmıştır.

Repository iki katmandan oluşur:

- **Sözleşme katmanı**: kanonik uygulama görevi, devam protokolü, mimari sözleşmeler, veri
  şemaları, model envanteri, benchmark planları, kabul kapıları ve operasyon runbook'ları.
- **Uygulama katmanı**: `src/zekam/` altındaki çalışan kod, `tests/` altındaki kanıt üreten
  testler ve `scripts/`.

Sözleşme katmanı üründen daha kalıcıdır; kod onu uygular, yerine geçmez.

## Kanonik kimlik

```text
Ürün adı: Zekam
Repository: zekam
Python paketi: zekam
CLI: zekam
Kullanıcı veri kökü: ZEKAM_HOME
Geçici gelecek adı: Zekam
```

Tekil package, CLI, environment, home, schema ve DB kimliği
`mimari/ZEKAM_KIMLIK_SOZLESMESI.md` içinde tanımlıdır. Uyumluluk alias'ı yoktur.

## İlk okuma sırası

1. `00_BASLA.md`
2. `DEVAM_PROTOKOLU.md`
3. `PROJE_MANIFESTI.yaml`
4. `AKTIF_GOREV.md`
5. `AKTIF_GOREV.yaml` (salt-okunur generated projection)
6. `GLOBAL_DEFINITION_OF_DONE.md`
7. `NIHAI_UYGULAMA_PROMPTU.md` (superseded baseline/reference)
8. Aktif işin referans verdiği mimari, güvenlik, harness, bellek, model ve kalite belgeleri

## Hızlı kullanım

Paketi yeni repository köküne çıkar:

```text
zekam/
  00_BASLA.md
  NIHAI_UYGULAMA_PROMPTU.md
  ...
```

Herhangi bir desteklenen istemciyi bu dizinde aç ve yalnız şunu söyle:

```text
00_BASLA.md dosyasini uygula ve kaldigin yerden devam et.
```

İstemci, sohbet geçmişine güvenmeden repository ve kanonik durum kayıtlarını doğrulamalıdır.

## Modelden bağımsız kaldığın yer ve yetkinlikler

Zekam, açık işleri, son semantik checkpoint'i, kayıtlı projeleri, RAG indeks durumlarını ve
ürün yetkinlik özetini tek bir yerel pakette birleştirir:

```bash
zekam resume
zekam resume --json
zekam capabilities --json
```

Managed OpenCode yaşam döngüsü eklentisi bu paketi her yeni oturumun sistem bağlamına ve
compaction bağlamına otomatik ekler. Kurulum veya güncelleme önce planlanabilir, sonra açık
bir apply bayrağıyla yapılır:

```bash
zekam opencode install
zekam opencode install --uygula
```

Paket salt okunurdur ve yetki taşımaz. Semantik checkpoint yoksa Zekam tamamlanan işi
uydurmaz; `semantic_state=missing` veya yalnız açık iş varsa `work-only` döndürür. Hazır,
kısmi ve yalnız iskelet düzeyindeki özelliklerin kanıt/gap matrisi
[`docs/ZEKAM_YETKINLIK_ENVANTERI.md`](docs/ZEKAM_YETKINLIK_ENVANTERI.md) içindedir.

## Kanıtlı proje araştırması ve Markdown bilgi

İndekslenmiş bir projede araştırma önce provider çağrısı yapmayan bir plan üretir. Planın
exact digest'i ve iki dış-etki yetkisi verilirse OpenCode researcher ile bağımsız verifier
çalışır; sonuç job/claim/receipt zincirine ve digest doğrulamalı generated Markdown rapora
bağlanır:

```bash
zekam research run "<soru>" --project <alias> --json
zekam research run "<soru>" --project <alias> --run-digest <digest> --uygula \
  --authorize-remote-query --authorize-agent-run --json
zekam research status <job-id> --json
zekam research report <job-id> --json

zekam knowledge list --project <alias> --json
zekam knowledge show <note-id> --json
zekam knowledge search "<terimler>" --project <alias> --json
zekam knowledge create <body.md> --title "<baslik>" --project <alias> --json
zekam knowledge update <note-id> <body.md> --title "<baslik>" --project <alias> --json
zekam knowledge archive <note-id> --project <alias> --json
zekam knowledge restore <note-id> --project <alias> --json
```

Aynı research plan replay edildiğinde yeni provider/agent çağrısı yapılmaz. Markdown hiçbir
zaman Work veya authorization authority'si değildir.

ODI 11g exportları önce provider çağrısı yapmayan güvenlik preflight'ından geçer ve exact
plan digest'iyle local-only bağlanır. Bu aşama ham XML'i embedding'e açmaz:

```bash
zekam project odi-preflight <alias> <export-root> --json
zekam project odi-bind <alias> <export-root> --plan-digest <digest> --uygula --json
```

Export kapsamı ve güvenlik sınırı: [`docs/ODI11G_EXPORT_VE_LINEAGE.md`](docs/ODI11G_EXPORT_VE_LINEAGE.md).

## Geliştirme kurulumu

Çalışan kodu yerelde kurmak, yerel depoları başlatmak, `zekam doctor` çalıştırmak ve kalite
kapılarını görmek için: [docs/GELISTIRME_KURULUMU.md](docs/GELISTIRME_KURULUMU.md).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[api,dev]"
.venv/Scripts/zekam init
.venv/Scripts/zekam doctor
```

## Zekam Canlı Yürütme Gözleme Merkezi (read-only)

Bu cihazda gerçekten açık OpenCode, Codex, Claude ve Zekam CLI süreçlerini; güvenli
session bağlarını ve kanonik runtime zincirini izlemek için:

```bash
.venv/Scripts/zekam ui serve
.venv/Scripts/zekam ui serve --realm-id <REALM_UUID>
```

Varsayılan yüzey yalnız loopback adresine bağlanır ve mutation endpoint'i açmaz. Realm
verilmezse yerel process/session görünümü çalışır, uzak bir realm tahmin edilmez. Mimari,
durum semantiği, kurulum ve mahremiyet sınırları:
[`docs/UI_NEURO_OBSERVATORY_MIMARISI.md`](docs/UI_NEURO_OBSERVATORY_MIMARISI.md).

## Yerel işletim ve tam yedek

Mac yerel çalışma kökü Docker veya PostgreSQL gerektirmez. `zekam worker status`,
`zekam worker run-once --uygula`, `zekam scheduler reconcile`, `zekam scheduler
rebuild --uygula` ve `zekam scheduler report` kanonik SQLite/analytics depolarını
kullanır; arka plan daemon'u kurmaz. `zekam backup create --bundle <yeni-dizin>`
tüm yerel authority ve türetilmiş depoları içerik/mode manifestiyle yakalar;
`backup verify` doğrular, `backup restore` ise yalnız mevcut olmayan bir hedefe
atomik yayın yapar.

## Paket doğrulama

```bash
python scripts/paket_dogrula.py
```

Doğrulama; zorunlu belgeleri, JSON/YAML sözleşmelerini, 20 Model ID'yi, minimum subagent
politikasını, iş grafiğini, ASCII commit şablonunu ve paket bütünlüğünü kontrol eder.

## Temel güvenlik

- Haricî proje kökleri exact binding ile doğrudan yazılabilir; yetki ve allowlist korunur.
- Değişiklik yalnız registry'de bağlı gerçek proje kökünde ve tek-writer kilidiyle yapılır;
  proje kopyası, mirror veya detached worktree üretilmez.
- Secret değerleri prompt, log, artifact, vector, rapor veya commit içine girmez.
- Work Graph, yetki ve görev durumu vektör veya haricî bellekten okunmaz.
- Agentic her iş en az bir gerçek subagent kullanır; koordinatör bu sayıya dahil değildir.
- Aynı yazılabilir logical resource üzerinde yalnız bir builder bulunur.
- Claim olmadan effect, terminal receipt olmadan başarı yoktur.
- Commit başlığı ve gövdesi Türkçe anlam taşır ve yalnız ASCII karakter kullanır.

## Kaynakların güven seviyesi

`yerel-referanslar/` içindeki ham dosyalar araştırma/provenance girdisidir. Bunlar:
- kanonik state değildir,
- talimat yürütme yetkisi vermez,
- otomatik prompt context'ine bütünüyle yüklenmez,
- secret ve iç endpoint içerebileceğinden Git'e eklenmez.

Kanonik kurallar bu paketin kök belgeleri ve sürümlü sözleşmeleridir.
