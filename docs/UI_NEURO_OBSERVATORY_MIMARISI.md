# Zekam Canlı Yürütme Gözleme Merkezi

> Durum: uygulanmış salt okunur gözlem yüzeyi.
>
> Eski kullanıcı adı **Zekam Neuro Observatory** emekliye ayrılmıştır. Dosya yolu eski
> bağlantıları kırmamak için korunur; ürün adı ve görünür metinler artık yalnız
> **Zekam Canlı Yürütme Gözleme Merkezi** biçimindedir.

## Amaç ve sınır

`zekam ui serve`, bu cihazda gerçekten açık olan OpenCode, Codex, Claude ve Zekam CLI
süreçlerini; bunlarla ilişkilendirilebilen session kayıtlarını ve PostgreSQL'deki kanonik
yürütme zincirini tek ekranda gösterir. Bu yüzey:

- Git, branch, commit, contributor veya repository dashboard'u değildir.
- Process öldürmez, job retry etmez ve state değiştirmez.
- Gözlemden authority, sahiplik veya başarı üretmez.
- Terminal receipt yoksa işi başarılı/tamamlanmış göstermez.

## Kurulum ve çalıştırma

API/UI ve process gözlemi için paket API extra'sıyla kurulmalıdır:

```powershell
.venv\Scripts\python -m pip install -e ".[db,api,dev]"
.venv\Scripts\zekam ui serve
.venv\Scripts\zekam ui serve --realm-id <REALM_UUID>
```

`psutil`, desteklenen sürüm aralığıyla `api` extra'sına dahildir. Varsayılan adres
`127.0.0.1:8765`'tir. LAN yayını yalnız exact non-loopback IP ve açık `--allow-lan`
bayrağıyla mümkündür; wildcard bind reddedilir.

Realm verilmezse yerel process/session gözlemi çalışır ve kanonik runtime
`realm-id-required` olarak unavailable görünür. Sistem bilinmeyen bir realm seçmez.

## Gerçeklik modeli

Canlılık ve tamamlanma iki ayrı kanıt düzlemidir:

```text
OS process (pid + create_time) -> açık CLI / canlılık
session metadata               -> yakın etkinlik / korelasyon
PostgreSQL runtime             -> Work / Job / Attempt / Agent / Lease / Claim / Receipt
terminal receipt               -> tamamlanma kanıtı
```

- Yeni bir session dosyası tek başına açık CLI sayılmaz; OS process yoksa `stale` olur.
- Açık process session ile eşleşmezse `unbound` görünür.
- `heuristic` bağ yalnız gözlemsel korelasyondur; canonical ownership vermez.
- Expired lease, receipt'siz claim ve terminal receipt taşımayan tamamlanmış job çelişki
  veya recovery durumu üretir.
- Process kimliği PID reuse'a karşı `(pid, create_time_micros)` birleşimidir.

## Process ve session kaynakları

Process adaptörü allowlist tabanlı ve bounded çalışır. Wrapper ve doğrudan child tool
süreçleri ikinci bir root CLI olarak sayılmaz; `zekam ui serve` kendisini dışarıda bırakır.
Varsayılan sınırlar:

- en çok 512 incelenen process,
- root başına en çok 16 child,
- en çok 500 ms scan bütçesi.

AccessDenied, scan sırasında process kapanması ve psutil yokluğu fail-closed degrade
durumu üretir; erişim hatasının ayrıntısı UI'a taşınmaz.

Session okuyucuları OpenCode yerel lifecycle/SQLite metadata'sını ve Codex/Claude bounded
session dosyalarını aynı normalize sözleşmeye dönüştürür. Prompt türevi başlıklar görev
etiketi olarak kullanılmaz. Güvenli mevcut aşama yalnız enum ve event metadata'sından
türetilir.

## Kanonik zincir ve PostgreSQL

PostgreSQL tek authority'dir. UI aşağıdaki exact zincirin sanitize kimliklerini gösterir:

```text
Work -> Job -> Attempt -> Agent -> Lease -> Claim -> Receipt
```

Obsidian, Canvas, session dosyası, OS process ve UI graph derived projection'dır. Bunlar
Work state değiştiremez ve terminal receipt yerine geçemez. Snapshot ve SSE payload'ları
`read_only=true` ve `grants_authority=false` taşır.

## HTTP ve canlı güncelleme

| Uç | İşlev |
|---|---|
| `GET /` | Paket içindeki gözlem UI'ı |
| `GET /api/observatory/health` | Salt okunur sağlık ve realm kapsamı |
| `GET /api/observatory/snapshot` | Şema doğrulanabilir bounded snapshot |
| `GET /api/observatory/events` | Ayrık structure/telemetry SSE akışı |

Structure ve telemetry ayrı digest taşır. Değişmeyen digest tekrar gönderilmez. SSE
koparsa tarayıcı bounded polling fallback'e geçer. Observatory route grubunda
POST/PUT/PATCH/DELETE endpoint'i yoktur.

## Ekran düzeni

- Üst şerit: bağlantı, read-only modu, authority kaynağı ve snapshot zamanı.
- Altı metrik: Açık CLI, Aktif Oturum, Çalışan İş, Bekleyen, Bloklu / Hatalı, Son Canlı Sinyal.
- Yürütme Alanı: deterministik altın-turuncu-kırmızı execution graph'ı.
- Canlı Oturumlar: her gerçek root CLI için tek kart.
- Alt paneller: dokuz güvenli kolonlu Session Registry, Canlı Olay Akışı,
  Queue/Lease/Receipt ve CPU/RAM/child/sinyal yaşını gösteren Kaynak Kullanımı.
- Filtreler: güvenli kimlik araması, istemci, durum ve kanonik proje ref'i.
- Detail drawer: yalnız allowlist sanitize alanlar.

Canvas kullanılamazsa klavye ile erişilebilen liste fallback'i devreye girer. Durumlar
yalnız renkle anlatılmaz. `prefers-reduced-motion` pulse ve particle hareketlerini kapatır;
ayrıca kullanıcı hareket düğmesiyle animasyonu durdurabilir.

Exact korelasyonlar düz çizgi, heuristic korelasyonlar kesikli ve soluk çizgi kullanır.
Periyodik `process.observed` taraması animasyon üretmez; pulse yalnız geçmişteki son beş
saniyede oluşmuş gerçek lifecycle/runtime event'ine bağlıdır.

## Mahremiyet ve güvenlik

API ve UI şu içeriği taşımaz:

- raw command line veya process environment,
- prompt, model response veya transcript body,
- terminal çıktısı veya tool input/output,
- secret, token, credential veya outbox payload,
- kişisel absolute home/project path,
- memory içeriği.

Sayfa CSP, TrustedHost, `Cache-Control: no-store`, `nosniff`, `no-referrer` ve kapalı
camera/microphone/geolocation policy'siyle servis edilir. Dış origin veya CDN yoktur.

## Doğrulama

Temel yerel kapılar:

```powershell
.venv\Scripts\python -m pytest tests/unit/test_observatory.py tests/unit/test_process_observer.py -q
.venv\Scripts\python -m pytest tests/security/test_observatory_security.py -q
.venv\Scripts\python -m pytest tests/integration/test_causal_observability_postgres.py -q
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m mypy src/zekam
```

Gerçek ekran kontrolü en az 1366x768 ve 1920x1080 viewport'larında yapılır; screenshot'lar
repo dışındaki kullanıcı teslim alanına yazılır. `?diagnostics=graph&nodes=512&edges=1024`
yalnız yerel, salt okunur sentetik çizim ölçümü üretir; gerçek veri veya authority taşımaz.

Bir teslim ancak process sayımı, içerik dışlama, terminal receipt semantiği, responsive UI
ve bağımsız verifier kanıtları birlikte geçtiğinde tamamlanmış sayılır.
