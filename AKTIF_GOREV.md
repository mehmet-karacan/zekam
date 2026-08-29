# AKTİF GÖREV — Zekam Canlı Yürütme Gözleme Merkezi

> Bu dosya yeni görev girdisidir. Kanonik Work/Plan/Run/Authorization kaydı değildir ve tek başına yetki vermez. Uygulama başlamadan önce `AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md` ve güncel kanonik PostgreSQL çalışma durumu doğrulanmalıdır.

## 0. Görev kullanım ve güvenlik kuralı

1. Bu görevin ürün adı ve kullanıcıya görünen başlığı tam olarak **“Zekam Canlı Yürütme Gözleme Merkezi”** olacaktır.
2. Görev, mevcut `zekam ui serve` yüzeyinin yalnız görsel makyajını değil; gerçek işletim sistemi süreçlerini, istemci oturumlarını ve kanonik Zekam runtime kayıtlarını birleştiren yeni canlı gözlem deneyimini kapsar.
3. GitHub repository, commit, branch, contributor, pull request veya release dashboard’u yapılmayacaktır.
4. Mevcut root `AKTIF_GOREV.md` elle başarıya çekilmeyecek veya doğrudan authority olarak kullanılmayacaktır. Yeni Work/Plan/Run mevcut kanonik uygulama servisleri üzerinden oluşturulmalı; insan dostu Markdown/YAML projeksiyonları buradan üretilmelidir.
5. Güncel çalışma ağacındaki kullanıcı değişiklikleri korunmalıdır. `stash`, `reset --hard`, `clean`, history rewrite, toplu geri alma veya geçici klon/worktree kullanılmamalıdır.
6. Kullanıcı açıkça istemedikçe commit, push, PR, merge veya GitHub workflow tetikleme işlemi yapılmamalıdır.
7. Prompt, model yanıtı, terminal çıktısı, tool input/output gövdesi, secret, token, ortam değişkeni, tam komut satırı ve kişisel absolute path hiçbir UI/API/log/test artifact’ına taşınmamalıdır.
8. UI read-only kalmalıdır. Bu görev command plane, approval butonu, job başlatma/durdurma veya başka mutation endpoint’i eklemez.
9. Agentic uygulamada en az bir gerçek subagent ve builder’dan bağımsız verifier kullanılmalıdır.
10. Her kritik iddia test, ölçüm veya kanonik kayıtla kanıtlanmalıdır; “çalışıyor gibi görünüyor” kabul değildir.

---

## 1. Doğrulanmış başlangıç tabanı

| Alan | Değer |
|---|---|
| Repository | `mehmet-karacan/zekam` |
| Branch | `main` |
| İncelenen HEAD | `ddd2009551b5bf50818e1863fafabf6899839811` |
| Son commit | `Kok gorev projeksiyonunu kanonik durumla uzlastir` |
| Önceki kanonik iş | `Zekam Ölçümlü Loop ve Graph Yürütme Düzlemi` — `completed`, 32/32 kabul |
| Mevcut UI komutu | `zekam ui serve` |
| Mevcut ürün adı | `Zekam — Neuro Observatory` |
| Mevcut frontend | `src/zekam/interfaces/api/static/index.html`, `styles.css`, `app.js` |
| Mevcut API | `/api/observatory/health`, `/snapshot`, `/events` |
| Mevcut snapshot | `zekam-observatory-snapshot/v2` |
| Mevcut canlı kaynaklar | PostgreSQL runtime projection + OpenCode yerel DB + Codex/Claude session dosyaları |
| Temel eksik | Açık CLI sayısı gerçek OS process varlığıyla doğrulanmıyor; yakın tarihli session/event sinyali “canlı” gibi yorumlanabiliyor |

Uygulama anında HEAD bu SHA’dan ilerideyse görev iptal edilmez. Önce güncel HEAD, migration head, root projeksiyonlar ve kanonik Work/Run durumu tekrar doğrulanır; artık çözülmüş maddeler tekrar uygulanmaz, drift görünür kanıt olarak kaydedilir.

---

## 2. Kilitli ürün kararı

### 2.1 Ürün adı

Kullanıcıya görünen ana ad:

```text
Zekam Canlı Yürütme Gözleme Merkezi
```

İzin verilen kısa başlıklar:

```text
ZEKAM
Canlı Yürütme
Yürütme Alanı
Canlı Oturumlar
```

“Neuro Observatory” ifadesi yeni ana UI’da kullanıcıya görünmemelidir. İç sınıf veya route adlarının sırf isim uğruna değiştirilmesi zorunlu değildir; gereksiz teknik churn yaratılmamalıdır.

### 2.2 Ana ürün sorusu

Yeni ekran ilk bakışta şu soruları doğru cevaplamalıdır:

1. Bu cihazda şu anda kaç OpenCode, Codex ve Claude CLI süreci gerçekten açık?
2. Kaç aktif session/oturum var?
3. Her oturum hangi proje, model, Work/Job/Attempt ve agent ile ilişkili?
4. Şu anda güvenli metadata düzeyinde hangi aşamada: model bekliyor, tool çalıştırıyor, subagent çalıştırıyor, input/approval bekliyor, idle, hata veya recovery durumunda mı?
5. Hangi process/session eşleşmesi exact, hangisi heuristic, hangisi unbound?
6. Açık process ile stale session, kayıp process ile açık session veya expired lease gibi tutarsızlıklar var mı?
7. Tamamlandığı söylenen işin terminal receipt’i gerçekten var mı?

### 2.3 Ana olmayan konular

Aşağıdakiler ana ekranın konusu değildir ve bu göreve eklenmemelidir:

- GitHub commit sayıları
- branch ve PR durumu
- contributor sıralaması
- repository health skoru
- release yönetimi
- genel kod analitik dashboard’u
- prompt veya konuşma içeriği
- terminal canlı ekranı
- UI üzerinden süreç sonlandırma veya job kontrolü

---

## 3. Misyon

Mevcut UI’ı, görsel olarak yoğun fakat okunabilir; veri açısından gerçek, güvenli ve açıklanabilir bir **canlı yürütme gözlem düzlemine** dönüştür.

Hedef gerçeklik zinciri:

```text
OS PROCESS GERÇEKLİĞİ
    ↓
İSTEMCİ SESSION / EVENT GÖZLEMİ
    ↓
ZEKAM WORK / JOB / ATTEMPT / AGENT BAĞI
    ↓
LEASE / CLAIM / RECEIPT KANITI
```

Üç katman birbirinden ayrılmalıdır:

- Bir process’in açık olması yalnız işletim sistemi gerçeğidir.
- Bir session’ın yakın zamanda event üretmesi istemci gözlemidir.
- Bir işin başarılı olması yalnız kanonik terminal receipt ve acceptance evidence ile gösterilebilir.

UI bu ayrımları gizlememeli; açıkça görünür kılmalıdır.

---

## 4. Korunacak mevcut mimari kararlar

1. `zekam ui serve` komutu korunur.
2. FastAPI tabanlı yerel read-only sunucu korunur.
3. Loopback varsayılanı, TrustedHost, CSP, no-store ve mevcut güvenlik header’ları korunur.
4. SSE ana canlı akış olarak korunur; polling fallback devam eder.
5. Dış CDN, dış font ve zorunlu npm/build zinciri eklenmez.
6. PostgreSQL realm verilmeden kanonik DB realm’i tahmin edilmez.
7. Mevcut Work/Job/Lease/Receipt gerçeklik sırası korunur.
8. Repository/document graph kodu silinmek zorunda değildir; ancak yeni ana ekranın merkezinden çıkarılır. İleride ikincil “Bilgi Ağı” görünümü olarak kullanılabilir.
9. Mevcut istemci session okuyucuları yeniden kullanılmalı ve kontrollü biçimde zenginleştirilmelidir; aynı veriyi farklı, çelişkili kanallardan tekrar üretmekten kaçınılmalıdır.
10. Graph ve UI state türetilmiş, yeniden üretilebilir ve authority vermeyen projection olarak kalır.

---

## 5. Yeni gerçeklik modeli

### 5.1 Process kimliği

PID tek başına kalıcı kimlik değildir. PID reuse riskine karşı process kimliği en az şu ikiliden türetilmelidir:

```text
(pid, create_time)
```

UI/API için bounded ve secret-free bir `process_ref` üretilebilir. Tam komut satırı veya absolute executable path döndürülmemelidir.

### 5.2 Açık CLI tanımı

`Açık CLI` sayısı yalnız hedef root process’lerden hesaplanır:

- OpenCode root CLI
- Codex root CLI
- Claude root CLI

Şunlar açık CLI sayısına dahil edilmez:

- `zekam ui serve` sürecinin kendisi
- tarayıcı
- shell wrapper’ı
- node/python child tool süreci
- git, test, compiler veya database child process’i
- session dosyası olup OS process’i bulunmayan stale kayıt

Wrapper → runtime → CLI zinciri çift sayılmamalıdır. Her gerçek root CLI yalnız bir kez sayılmalıdır.

### 5.3 Zekam worker ve ajanlar

Zekam worker/agent process’leri ayrı tür olarak gözlenebilir; ancak `Açık CLI` metriğine karıştırılmamalıdır. Bunlar `Çalışan İş`, `Worker` veya session detayında ayrı gösterilmelidir.

### 5.4 Güvenli process observation sözleşmesi

En az aşağıdaki alanlar değerlendirilmelidir:

```text
process_ref
pid
parent_process_ref?
client                  opencode | codex | claude | zekam
role                    cli-root | worker | tool-child
state                   running | waiting | idle | stale | failed | exited
executable_label        yalnız basename / bounded label
started_at
observed_at
cpu_percent?
memory_rss_bytes?
child_process_count
session_ref?
project_ref?
model_ref?
current_action?
current_tool_category?
work_item_id?
job_id?
attempt_id?
agent_ref?
lease_expires_at?
binding_confidence      exact | strong | heuristic | unbound
canonical               false
content_included        false
```

### 5.5 Safe `current_action` semantiği

“Şu anda ne yapıyor?” bilgisi prompt veya terminal içeriğinden çıkarılmayacaktır. Yalnız güvenli, sınırlı durum enum’larından türetilmelidir:

```text
starting
model-wait
tool-running
subagent-running
queue-wait
approval-wait
user-input-wait
idle
closing
failed
unknown
```

Gösterilebilecek örnekler:

- `Terminal aracı çalışıyor`
- `Model yanıtı bekleniyor`
- `2 subagent aktif`
- `Kanonik job kuyruğunda bekliyor`
- `Kullanıcı girdisi bekliyor`
- `Lease süresi dolmuş; recovery gerekli`

Gösterilmemesi gerekenler:

- prompt özeti
- dosya içeriği
- terminal komutu
- tool input/output
- model yanıtı
- absolute dosya yolu

### 5.6 Bağlama güven seviyesi

Process ↔ session ↔ canonical runtime eşleşmesi şu sırayla yapılmalıdır:

1. Explicit client/session/correlation kimliği — `exact`
2. PID + create time veya istemci tarafından yazılmış process metadata’sı — `strong`
3. Aynı client + sanitize project ref + dar zaman penceresi — `heuristic`
4. Eşleşme yok — `unbound`

Heuristic bağ hiçbir zaman canonical başarı, sahiplik veya authority iddiası üretmemelidir. UI exact ve heuristic edge’leri görsel olarak ayırmalıdır.

### 5.7 Çelişki ve orphan durumları

En az şu durumlar hesaplanmalı ve görünür olmalıdır:

- `process-without-session`
- `session-without-process`
- `process-session-project-mismatch`
- `active-session-with-expired-lease`
- `claimed-job-without-live-owner`
- `completed-observation-without-terminal-receipt`
- `duplicate-cli-root`
- `pid-reuse-detected`
- `stale-heartbeat`

---

## 6. Backend uygulama kapsamı

### 6.1 Cross-platform process okuyucusu

Yeni bir process observation portu ve platformdan bağımsız uygulama servisi oluşturulmalıdır.

Tercih edilen yöntem:

- Python 3.12 ile uyumlu, lisansı ve güvenlik/audit sonucu kabul edilmiş bounded bir `psutil` sürüm aralığı
- dependency, mevcut paket politikasına uygun biçimde `api` veya ayrı `ui` optional extra altında tanımlanmalı
- doğrudan `ps`, `wmic`, PowerShell, shell pipeline veya sınırsız `/proc` scraping kullanılmamalı

Process okuma başarısız olduğunda UI sahte veri üretmemeli; `process_observation.available=false` ve güvenli bir hata sınıfı göstermelidir.

Okuyucu şu yarış koşullarına dayanmalıdır:

- process scan sırasında kapanabilir
- erişim reddedilebilir
- create time okunamayabilir
- PID yeniden kullanılabilir
- parent process child’dan önce kapanabilir
- CPU metriği için ilk örnek ölçümsüz olabilir

### 6.2 Process sınıflandırma

Tüm `node`, `python`, `bash`, `zsh`, `cmd` veya `powershell` süreçleri CLI sayılmamalıdır.

Sınıflandırma:

- allowlist tabanlı olmalı
- executable basename, bounded internal cmd token incelemesi ve parent/child ilişkisi kullanabilir
- raw cmdline yalnız process içinde geçici sınıflandırma amacıyla okunabilir; API, log, test snapshot veya artifact’a yazılamaz
- unknown süreçler UI’a taşınmamalı
- child tool process’leri root CLI’dan ayrılmalı

### 6.3 Platform hedefi

- Windows 10/11
- macOS
- Linux

Gerçek platform smoke testi çalışılan cihazda yapılmalı; diğer platform davranışları synthetic process fixture’larıyla doğrulanmalıdır.

### 6.4 Session okuyucularının zenginleştirilmesi

Mevcut OpenCode, Codex ve Claude local session okuyucuları şu güvenli alanlarla zenginleştirilmelidir:

- session ref
- client
- sanitize project ref
- model ref bulunabiliyorsa bounded model kimliği
- started/last observed timestamp
- safe phase
- active tool category
- parent/child session veya subagent bağı
- exact correlation kimlikleri

Raw transcript, prompt, response veya tool body okunmamalı; zorunlu teknik format doğrulaması dışında parse edilmemelidir.

### 6.5 Canonical runtime korelasyonu

Mevcut PostgreSQL projection’dan en az şunlar güvenli şekilde eşleştirilmelidir:

```text
Work Item
Plan step
Job
Attempt
Agent/session assignment
Lease + fencing
Effect claim
Terminal receipt
Recovery state
```

UI’daki `completed` durumu client event’inden değil terminal receipt’ten türetilmelidir.

### 6.6 Snapshot sözleşmesi

Mevcut `zekam-observatory-snapshot/v2` sözleşmesi sürümlü biçimde genişletilmelidir. Alan silerek sessiz kırılma yapılmamalıdır.

Yeni sürüm en az şu bölümleri taşımalıdır:

```text
execution_summary
processes
sessions
bindings
agents
events
causal
runtime
safety
```

Önerilen ana özet:

```text
open_cli_count
worker_count
active_session_count
running_work_count
waiting_count
blocked_or_failed_count
unbound_process_count
stale_session_count
last_live_signal_at
process_observation_available
canonical_runtime_available
```

Schema `additionalProperties=false`, bounded listeler, max string uzunlukları ve content exclusion garantileriyle güncellenmelidir.

### 6.7 SSE ve telemetry

Canlı akış şu davranışı sağlamalıdır:

- ilk bağlantıda tam snapshot
- process/session/topology değişince structure snapshot veya delta
- CPU/RAM/heartbeat için bounded telemetry event’i
- bağlantı canlı fakat değişiklik yoksa heartbeat
- bağlantı koparsa polling fallback
- reconnect sonrası duplicate session/process üretmeme

Volatile telemetry yüzünden UI state sürekli yeniden kurulup node’lar zıplamamalıdır. Structural identity ile telemetry güncellemesi ayrılmalı; node konumları korunmalıdır.

### 6.8 Konfigürasyon

En az şu ayarlar değerlendirilmelidir:

```text
process_observation.enabled
process_observation.sample_seconds
process_observation.max_cli_roots
process_observation.max_children_per_root
process_observation.stale_after_seconds
process_observation.include_resource_metrics
```

Varsayılanlar güvenli, bounded ve yerel olmalıdır. Process observation kullanıcı tarafından kapatılabilmelidir.

---

## 7. Yeni UI bilgi mimarisi

### 7.1 Ana yerleşim

Desktop ana ekranı dört katmandan oluşmalıdır:

```text
ÜST SİSTEM ŞERİDİ
────────────────────────────────────────────
ANA YÜRÜTME ALANI              CANLI OTURUMLAR
                               SAĞ RAYI
────────────────────────────────────────────
SESSION REGISTRY | OLAY AKIŞI | QUEUE/LEASE/RECEIPT | KAYNAK
────────────────────────────────────────────
ALT DURUM ŞERİDİ
```

Mevcut repository/document beyin grafiği ana ekranın merkezinden çıkarılmalıdır.

### 7.2 Üst sistem şeridi

Tek bakışta en az şu sayaçlar görünmelidir:

- **Açık CLI**
- **Aktif Oturum**
- **Çalışan İş**
- **Bekleyen**
- **Bloklu / Hatalı**
- **Son Canlı Sinyal**

Her sayaç gerçek kaynağını tooltip veya detay içinde açıklamalıdır. “Açık CLI” yalnız OS process sayısıdır.

### 7.3 Ana “Yürütme Alanı” grafiği

Gönderilen referanslardaki koyu, yoğun ve ışıklı network dili Zekam’a uyarlanmalıdır.

Her CLI/session ayrı bir görsel küme olmalıdır:

```text
CLI root process
  ├─ session
  ├─ canonical work/job/attempt
  ├─ agent/subagent
  └─ aktif tool child process’leri
```

Grafik davranışı:

- stabil ve deterministik cluster yerleşimi
- incremental update; her snapshot’ta tam rastgele yeniden dizilim yok
- gerçek event geldiğinde edge boyunca kısa, sınırlı pulse
- hover ile kısa güvenli özet
- tıklama ile detay drawer/panel
- search ve client/state/project filtreleri
- exact bağ düz çizgi; heuristic bağ kesikli/soluk çizgi
- error/recovery edge’i kırmızı
- completed yalnız küçük receipt işaretiyle gösterilir
- boş dekoratif düğüm ve sahte trafik üretilmez

### 7.4 Sağ ray — Canlı Oturumlar

Her root CLI için bir kart gösterilmelidir:

```text
CODEX
PID 18472 · 18 dk

GPU / Oracle Analizi
Model: GPT-5.6 Pro
Durum: Terminal aracı çalışıyor
Bağ: exact
Heartbeat: 2 sn önce
CPU 14% · RAM 612 MB · 3 child
```

Kartta yalnız mevcut veriler gösterilir; bilinmeyen alan uydurulmaz.

Kart durumları:

- aktif/running: altın–turuncu vurgu
- waiting/idle: nötr gri ve sınırlı amber
- failed/recovery/stale: kırmızı vurgu
- terminal receipt: küçük ve ölçülü yeşil işaret
- yeni event: kısa süreli pulse; sürekli glow yok

### 7.5 Alt operasyon panelleri

#### Session Registry

Kolonlar:

```text
Client | PID | Session | Proje | Model | Başlangıç | Süre | Durum | Bağ Güveni
```

#### Canlı Olay Akışı

Yalnız güvenli event metadata’sı:

```text
tool-started
model-wait
subagent-started
job-claimed
lease-renewed
input-required
receipt-recorded
recovery-required
```

#### Queue / Lease / Receipt

- pending job
- active lease
- expired lease
- receipt’siz claim
- recovery job
- terminal receipt
- orphan/correlation boşluğu

#### Kaynak Kullanımı

- root CLI CPU
- root CLI RAM
- child count
- observation lag
- son heartbeat

Bunlar repository performans metriği değildir; yalnız yerel runtime kaynağıdır.

### 7.6 Detay paneli

Seçilen oturum için şu zincir görünmelidir:

```text
Process
  → Session
    → Work Item
      → Job
        → Attempt
          → Agent
            → Tool Category
              → Lease / Claim / Receipt
```

Her satır kaynak türünü ve bağ güvenini göstermeli; kanonik referans varsa kopyalanabilmelidir. Raw process cmdline veya içerik gösterilmemelidir.

### 7.7 Görsel dil

Ana tema:

- çok koyu charcoal/siyah zemin
- ince, düşük kontrastlı panel çizgileri
- ana vurgu altın ve sıcak turuncu
- hata/recovery kırmızı
- başarı/receipt yalnız küçük yeşil vurgu
- idle nötr gri
- cyan/mint/violet gökkuşağı görünümü kullanılmamalı
- yoğun gradient, tüm kartı boyayan durum rengi ve aşırı glow kullanılmamalı
- glow yalnız aktif node, event pulse ve küçük durum işaretlerinde kullanılmalı

Önerilen başlangıç token’ları:

```text
bg-deep        #060607
bg-panel       #0D0E10
bg-elevated    #141519
line           #2A2926
text           #EAE5DB
muted          #87847D
gold           #F1A52A
gold-hot       #FFC04A
orange         #FF7A1A
red            #E84A3C
green-receipt  #78B86B
idle           #666A70
```

Bunlar nihai zorunlu renk kodları değildir; erişilebilirlik ve gerçek ekran kanıtıyla ayarlanmalıdır.

### 7.8 Responsive davranış

Öncelikli viewport’lar:

- 1366×768
- 1440×900
- 1728 genişlik sınıfı
- 1920×1080
- 2560×1440

Dar ekranda:

- sağ ray ana grafiğin altına taşınabilir
- alt paneller 2×2 veya tek kolon olabilir
- tablo yatay scroll kullanabilir
- graph minimum kullanılabilir yüksekliği korur
- metin taşması kontrollü ellipsis ile çözülür

### 7.9 Erişilebilirlik

- `prefers-reduced-motion` bütün pulse/particle hareketlerini azaltır
- keyboard focus görünürdür
- node seçimi klavyeyle erişilebilir alternatif liste taşır
- durum yalnız renkle anlatılmaz; label/ikon bulunur
- kontrast gerçek viewport screenshot’ında doğrulanır
- Canvas başarısızsa session registry tam işlevli fallback olur

---

## 8. Güvenlik ve mahremiyet kapıları

API veya UI’da kesinlikle bulunmaması gerekenler:

```text
raw command line
process environment
absolute home/project path
prompt
model response
transcript body
tool input/output
terminal output
secret/token/key
memory content
outbox payload
owner credential
```

Ek kurallar:

1. Yalnız allowlist hedef process’ler taşınır.
2. Erişilemeyen process için AccessDenied detayı kullanıcıya verilmez; yalnız bounded hata sınıfı görünür.
3. Project adı absolute path’ten türetiliyorsa yalnız sanitize basename veya kanonik project ref gösterilir.
4. Raw cmdline fingerprint gerçekten zorunluysa cihaz-yerel salt ile geri döndürülemez ve bounded biçimde üretilmeli; mümkünse hiç taşınmamalıdır.
5. Process snapshot kalıcı DB’ye yazılmamalıdır; bu görev yalnız canlı read-only projection’dır.
6. LAN erişimi mevcut explicit allowlist politikasından sapmamalıdır.
7. Mutation route eklenmemelidir.
8. CSP’ye dış origin eklenmemelidir.
9. UI hata mesajları traceback, DB mesajı veya filesystem path sızdırmamalıdır.
10. Testler secret/path leakage için negatif assertion içermelidir.

---

## 9. Önerilen uygulama paketleri

Dosya adları güncel mimariye göre yeniden tabanlanabilir; sorumluluk sınırları korunmalıdır.

### WP0 — Baseline ve kanonik admission

- güncel HEAD/migration/doctor/Work state doğrulaması
- önceki Work’ün gerçekten terminal completed olduğunu kanıtlama
- bu yeni görevi kanonik Work/Plan/Run olarak oluşturma
- kapsam dışı kullanıcı değişikliklerini kaydetme ve koruma

### WP1 — Process observation domain ve port

- process kimliği ve state modelleri
- process reader portu
- cross-platform psutil adaptörü
- PID reuse, AccessDenied ve process-exit yarışları
- CLI root/worker/tool-child sınıflandırma
- bounded config

### WP2 — Session ve process korelasyonu

- OpenCode/Codex/Claude session modellerini normalize etme
- process ↔ session bağlama
- exact/strong/heuristic/unbound confidence
- orphan ve çelişki üretimi
- safe current_action derivation

### WP3 — Canonical runtime ve snapshot vNext

- Work/Job/Attempt/Agent/Lease/Claim/Receipt bağları
- execution summary
- process/session/binding listeleri
- schema ve JSON contract
- SSE structure/telemetry ayrımı
- polling fallback ve bounded response

### WP4 — UI’ın tamamen yeniden kurulması

- kullanıcıya görünen ürün adını değiştirme
- yeni page shell ve top system strip
- yeni Yürütme Alanı graph renderer
- Canlı Oturumlar sağ rayı
- dört alt operasyon paneli
- detail drawer
- search/filter/recenter/pause
- eski repository graph’ı ana görünümden çıkarma

### WP5 — Güvenlik, degrade mode ve erişilebilirlik

- content/path/cmdline leakage negatif testleri
- process observation kapalı/uygunsuz/erişim reddi modları
- realm yokken local process görünümü + canonical runtime unavailable açıklaması
- reduced motion, keyboard ve Canvas fallback

### WP6 — Ölçüm ve kalite

- unit/integration/e2e/security testleri
- gerçek platform smoke testi
- 64 root process + bounded child fixture performansı
- 512 node / 1024 edge graph performansı
- SSE reconnect ve duplicate engelleme
- screenshot tabanlı gerçek viewport incelemesi
- Ruff, mypy, package validation ve mevcut kalite kapıları

### WP7 — Dokümantasyon ve kapanış

- UI mimari belgesini yeni ürün adı ve gerçeklik modeliyle güncelleme
- eski “Neuro Observatory” adını migration notuyla emekliye ayırma
- kurulum, process observation dependency ve privacy ayarlarını belgelemek
- canonical projection/receipt üretmek
- bağımsız verifier onayı
- kullanıcı yetkisi yoksa commit/push yapmadan teslim raporu

---

## 10. Dosya etkisi için başlangıç haritası

Uygulama öncesi güncel kod yeniden okunmalıdır. Beklenen etki alanı:

```text
pyproject.toml
config/zekam.default.yaml
schemas/observatory_snapshot.schema.json
src/zekam/domain/observability.py veya yeni process-observation domain modülü
src/zekam/application/observatory.py
src/zekam/application/composition.py
src/zekam/interfaces/api/observatory.py
src/zekam/interfaces/api/static/index.html
src/zekam/interfaces/api/static/styles.css
src/zekam/interfaces/api/static/app.js
src/zekam/interfaces/cli/ui.py
docs/UI_NEURO_OBSERVATORY_MIMARISI.md veya kontrollü yeni adlandırılmış karşılığı
tests/unit/test_observatory*.py
tests/integration/test_*observatory*.py
tests/security/test_*observatory*.py
tests/e2e/test_*ui*.py
```

Sırf dosya adı bu listede diye değiştirme yapılmamalı; yalnız gerçek sorumluluğu olan dosya etkilenmelidir.

---

## 11. Zorunlu kabul kriterleri

### Process doğruluğu

- [ ] Açık CLI sayısı fake process fixture’daki gerçek root sayısıyla birebir eşleşir.
- [ ] Node/python/shell child process’leri ayrı CLI olarak sayılmaz.
- [ ] `zekam ui serve` kendi kendisini CLI saymaz.
- [ ] Session dosyası yeni olsa bile OS process yoksa `Açık CLI` sayılmaz.
- [ ] Process açık fakat session eşleşmemişse `unbound process` olarak görünür.
- [ ] PID reuse `(pid, create_time)` ile ayrılır.
- [ ] Scan sırasında kapanan process UI/API’yi düşürmez.
- [ ] AccessDenied sahte inactive/active sonucu üretmez.

### Session ve binding doğruluğu

- [ ] OpenCode, Codex ve Claude aynı normalize session sözleşmesine dönüşür.
- [ ] Exact ve heuristic bağlar veri ve görsel düzeyde ayrıdır.
- [ ] Heuristic bağ canonical ownership veya başarı iddiası vermez.
- [ ] Parent/child session ve subagent ilişkisi mümkün olduğunda görünürdür.
- [ ] Safe current_action yalnız enum/event metadata’dan türetilir.
- [ ] Prompt/terminal içeriğinden görev özeti üretilmez.

### Canonical runtime doğruluğu

- [ ] Work → Job → Attempt → Agent → Lease → Claim → Receipt zinciri exact ID’lerle gösterilir.
- [ ] Terminal receipt olmadan UI `completed/success` göstermez.
- [ ] Expired lease, receipt’siz claim ve recovery-required durumları görünürdür.
- [ ] Realm verilmezse DB realm tahmini yapılmaz.
- [ ] Local process observation canonical runtime’dan bağımsız availability taşır.

### UI doğruluğu

- [ ] Kullanıcıya görünen ana ad tam olarak `Zekam Canlı Yürütme Gözleme Merkezi`dir.
- [ ] Ana ekran repository/commit dashboard’u değildir.
- [ ] Üst şerit altı temel canlı metriği gösterir.
- [ ] Ana graph her gerçek session/CLI için stabil cluster üretir.
- [ ] Snapshot güncellemelerinde node’lar gereksiz yere yer değiştirmez.
- [ ] Sağ ray her açık root CLI için tek kart gösterir.
- [ ] Session Registry, Olay Akışı, Queue/Lease/Receipt ve Kaynak Kullanımı panelleri çalışır.
- [ ] Search ve client/state/project filtreleri çalışır.
- [ ] Canvas kullanılamazsa liste/tablo fallback ile temel bilgi kaybolmaz.
- [ ] Reduced motion davranışı doğrulanır.
- [ ] 1366×768 ve 1920×1080 ekranlarda taşma/üst üste binme yoktur.

### Güvenlik

- [ ] API response’da raw cmdline yoktur.
- [ ] API response’da environment veya absolute path yoktur.
- [ ] Prompt, response, transcript ve tool body yoktur.
- [ ] Secret/URL/path leakage negatif testleri geçer.
- [ ] Mutation endpoint’i yoktur.
- [ ] CSP, TrustedHost, no-store ve mevcut güvenlik header’ları korunur.
- [ ] Process observation kapalıysa fail-closed ve açıklanabilir degrade mode çalışır.

### Performans ve kalite

- [ ] Process scan bounded limitler içinde ölçülür ve ana thread’i bloklamaz.
- [ ] 64 root + bounded child fixture’da snapshot süresi raporlanır.
- [ ] 512 node / 1024 edge graph etkileşimi kullanılabilir kalır.
- [ ] SSE reconnect duplicate process/session oluşturmaz.
- [ ] Polling fallback canlı veriyi sürdürebilir.
- [ ] Unit, integration, e2e, security, Ruff, mypy ve package validation geçer.
- [ ] Gerçek viewport screenshot kanıtları repo dışı teslim alanında üretilir.
- [ ] Bağımsız verifier process sayımı, content exclusion, receipt semantiği ve UI davranışını doğrular.

---

## 12. Test matrisi

| Alan | Zorunlu senaryolar |
|---|---|
| Process | root CLI, wrapper, child tool, process exit, AccessDenied, PID reuse, duplicate root |
| Platform | Windows fixture, macOS fixture, Linux fixture, çalışılan platform smoke |
| Session | OpenCode/Codex/Claude aktif, idle, stale, parent-child, bozuk metadata |
| Binding | exact, strong, heuristic, unbound, project mismatch |
| Runtime | active lease, expired lease, receipt, receipt’siz claim, recovery |
| API | schema, bounds, no-store, CSP, reconnect, unavailable source |
| Security | cmdline, env, secret, prompt, response, absolute path leakage |
| UI | empty, only process, only runtime, mixed, many sessions, error states |
| Responsive | 1366×768, 1440×900, 1728 sınıfı, 1920×1080, 2560×1440 |
| Accessibility | reduced motion, keyboard focus, color-independent state, Canvas fallback |
| Performance | 64 CLI roots, bounded child set, 512/1024 graph, rapid telemetry |

---

## 13. Yasak kısa yollar

- Son event 30 saniyeden yeniyse process’i otomatik “canlı CLI” sayma.
- Tüm node/python process’leri istemci sayma.
- Raw cmdline’ı frontend’e gönderip JavaScript’te filtreleme.
- Promptu özetleyerek “şu an ne yapıyor” alanı üretme.
- Client `completed` event’ini terminal receipt yerine kullanma.
- Exact ve heuristic bağları aynı çizgi/durumla gösterme.
- Her SSE güncellemesinde graph’ı baştan random dizme.
- Görsel yoğunluk için sahte node/edge/event üretme.
- Repository Git verisini ana dashboard’a taşıma.
- UI üzerinden process kill, pause, retry veya job mutation ekleme.
- Test yerine yalnız screenshot’a güvenme.
- Gerçek viewport görmeden yalnız DOM testleriyle görsel işi tamamlandı sayma.
- Kullanıcının açık izni olmadan commit/push/PR oluşturma.

---

## 14. Nihai Definition of Done

Görev ancak aşağıdaki ifade kanıtla doğruysa tamamlanmış sayılır:

> `zekam ui serve` açıldığında kullanıcı, bu cihazda gerçekten açık OpenCode/Codex/Claude CLI süreçlerini çift sayım olmadan görebiliyor; her process’in session, proje, model, güvenli mevcut aşama, agent/tool ve varsa kanonik Work/Job/Attempt/Lease/Receipt bağını ayırt edebiliyor; stale, unbound ve recovery durumlarını görebiliyor; hiçbir prompt, yanıt, terminal içeriği, secret, raw cmdline veya absolute path sızmıyor; UI yeni koyu altın–turuncu yürütme alanı tasarımında stabil, erişilebilir ve ölçülmüş biçimde çalışıyor.`

Bu kanıt oluşmadan yalnız renk değişimi, yeni başlık veya dekoratif graph görev tamamlanması değildir.

---

## 15. Kısa uygulama başlangıç promptu

```text
AGENTS.md dosyasını oku ve içindeki başlangıç protokolünü uygula. ../zekam-girdi/AKTIF_GOREV.md içindeki onaylı “Zekam Canlı Yürütme Gözleme Merkezi” görevini güncel HEAD ve kanonik PostgreSQL Work durumu üzerinde yeniden doğrula; yalnız bu kapsamı uygula. Mevcut kullanıcı değişikliklerini koru, prompt/response/secret/raw cmdline/absolute path sızdırma, read-only sınırı bozma ve kullanıcı açıkça istemedikçe commit veya push yapma.
```
