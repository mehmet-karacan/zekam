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

- Yapışkan sistem şeridi: bağlantı, read-only modu, PostgreSQL authority kaynağı ve
  snapshot zamanı.
- Sol gözlem rayı: yürütme, aktivite, integrity, inspector, analitik ve diagnostics
  bölümlerine klavye ile erişilebilen bağlantılar.
- Altı kanıta dayalı KPI: Açık CLI, Aktif Oturum, Aktif Agent, Çalışan İş, Açık Claim ve
  Son Sinyal. Payda yoksa oranlar `N/A` gösterilir; sahte yüzde üretilmez.
- Yürütme Alanı: istemci/session/job kümeleri, deterministik hull haze, exact/heuristic/
  unbound çizgi dili ve yalnız gerçek yeni olaylarda bounded pulse kullanan organik
  altın-turuncu-kırmızı canvas.
- Sağ ray: içeriksiz canlı aktivite, Work -> Job -> Attempt -> Claim -> Receipt integrity
  zinciri ve allowlist alanlardan oluşan Session Inspector.
- Alt analitik matris: normalize Session Registry, gerçek olay heatmap'i, agent/client
  sıralaması, durum donut'ı ve açıkça kanonik olmadığı belirtilen bounded tarayıcı
  telemetrisi.
- Filtreler: güvenli kimlik araması, istemci, durum, binding confidence, zaman penceresi
  ve kanonik proje ref'i.
- Kontroller: pan, zoom, seçili düğüme odak, görünümü sığdır, animasyonu durdur ve
  erişilebilir liste fallback'i.

Canvas odağındayken ok tuşları görünümü kaydırır, `+`/`-` zoom yapar, `0` görünümü
sığdırır; `Enter` veya `Space` güvenli düğüm seçimini inspector'a taşır.

Canvas kullanılamazsa klavye ile erişilebilen liste fallback'i devreye girer. Durumlar
yalnız renkle anlatılmaz. `prefers-reduced-motion` pulse ve particle hareketlerini kapatır;
ayrıca kullanıcı hareket düğmesiyle animasyonu durdurabilir. Sekme görünmez olduğunda
`requestAnimationFrame` iptal edilir; tekrar görünür olduğunda tek çizim döngüsü kurulur.
Renderer en çok 96 particle ve 72 etiketi aynı anda çizer.

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
repo dışındaki kullanıcı teslim alanına yazılır. Ayrıca 1024x768 ve 390x844 görünümünde
yatay taşma olmaması, mobil kontrollerin en az 44 piksel olması, seçili inspector ve boş
filtre durumu doğrulanır. `?diagnostics=graph&nodes=512&edges=1024` yalnız yerel, salt
okunur sentetik çizim ölçümü üretir; gerçek veri veya authority taşımaz. Tanı 15 örnekten
medyan ve p95 çizim süresini, tahmini FPS'i ve bounded particle/etiket sayılarını üretir.
Diagnostics modu aynı 512 düğüm/1024 bağı ana etkileşimli renderer'a da yükler ve üst
durum şeridinde açıkça `SENTETİK DIAGNOSTICS` yazar; bu grafik Work, receipt veya process
kanıtı değildir.

On dakikalık yerel dayanıklılık kapısı yalnız sayısal ve sanitize sayaçları okur:
EventSource bağlantı sayısı, telemetry olay sayısı, bounded ring uzunluğu, DOM düğüm
sayısı, animasyon/hidden-tab durumu ve tarayıcı sağlıyorsa JS heap kullanımı. Prompt,
yanıt, komut, path veya process ayrıntısı bu tanı yüzeyine yazılmaz.

Görsel kabul rubriği dört başlık taşır: referans kompozisyonu, execution veri mimarisi,
interaction/motion ve okunabilirlik/responsive. İlk üç başlığın her biri en az 75/100,
toplam skor en az 85/100 olmalıdır. Bu değerlendirme builder'dan bağımsız verifier
tarafından yapılır.

Bir teslim ancak process sayımı, içerik dışlama, terminal receipt semantiği, responsive UI
ve bağımsız verifier kanıtları birlikte geçtiğinde tamamlanmış sayılır.
