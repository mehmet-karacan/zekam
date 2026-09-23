---
schema: zekam-active-task/v2
task_id: ZEKAM-UI-REMOVAL-001
status: APPROVED_ACTIVE_TASK
title: Zekam UI ve Dashboard Yuzeylerinin Tamamen Kaldirilmasi
created_at: 2026-09-22T11:17:00+03:00
baseline_repository: mehmet-karacan/zekam
baseline_branch: main
baseline_head: 68b4833ff959185e5155a28680658ccbd96dd997
legacy_postgresql_data_import: FORBIDDEN
postgresql_runtime_dependency: FORBIDDEN
docker_required_for_zekam_core: false
push_authorized: false
---

# AKTIF_GOREV.md

## 1. Görev özeti

Bu görevde Zekam ürünündeki **kullanıcı arayüzü (UI), dashboard ve Canlı Yürütme Gözleme Merkezi / Neuro Observatory yüzeyleri tamamen kaldırılacaktır**.

Kullanıcı kararı kesindir:

> Zekam'da UI olmayacak. UI ile ilgili çalışan kod, komut, web yüzeyi, statik asset, UI'ye özel API, UI'ye özel projection/model/schema, test, paket bağımlılığı, dokümantasyon, manifest kaydı veya legacy-preserved kopya bırakılmayacak.

Hedef ürün **CLI + agent/client entegrasyonları + scheduler/worker + knowledge/memory/runtime servisleri + gerekli makine-okunur protokoller** üzerinden çalışacaktır. Yeni bir UI, TUI, browser paneli, dashboard, local web ekranı veya benzeri bir görsel yönetim yüzeyi bu görevin yerine ikame edilmeyecektir.

Bu dosya kullanıcı tarafından onaylanmış yeni aktif görevdir. Uygulamaya başlanırken repository içindeki mevcut `AKTIF_GOREV.md` yaşayan authority olarak bu dosyayla değiştirilir; `AKTIF_GOREV.yaml` elle tasarlanmaz, exact Markdown bytes üzerinden mevcut `ActiveTaskContract` projeksiyon mekanizmasıyla yeniden üretilir.

Push yetkisi yoktur. Commit ancak repo kuralları, testler ve bağımsız verifier geçtikten sonra yapılabilir; push için ayrıca açık kullanıcı onayı gerekir.

---

## 2. Doğrulanmış başlangıç baseline'ı

Araştırma sırasında GitHub `main` dalı şu revision'da doğrulandı:

```text
repository : mehmet-karacan/zekam
branch     : main
HEAD       : 68b4833ff959185e5155a28680658ccbd96dd997
commit     : duzeltme: bilgi indeksi chunk kimligi cakismasini gider
commit_at  : 2026-09-19T15:58:11Z
```

Bu görev uygulanırken önce `00_BASLA.md`, `AGENTS.md`, `DEVAM_PROTOKOLU.md` ve `GLOBAL_DEFINITION_OF_DONE.md` okunacak; local checkout HEAD'i bu baseline'dan ilerlemişse görev iptal edilmeyecek, fakat **stale plan** kabul edilerek güncel HEAD üzerinde yeniden discovery yapılacaktır.

Repository başlangıç protokolü gereği:

- gerçek source root bulunacak,
- `git status --short`, branch, HEAD ve son commitler okunacak,
- `python scripts/paket_dogrula.py` çalıştırılacak,
- aktif Work/lease/recovery durumu doğrulanacak,
- agentic uygulamada koordinatör dışında en az bir gerçek subagent kullanılacak,
- mutation yalnız bağlı gerçek source rootunda yapılacak,
- kopya/mirror/detached worktree oluşturulmayacak.

---

## 3. Ana hedef invariant'ları

Uygulama tamamlandığında aşağıdaki şartların **tamamı** doğru olmalıdır:

1. `zekam ui` veya `zekam ui serve` diye bir CLI komutu yoktur.
2. Zekam browser'da açılan bir ürün ekranı sunmaz.
3. HTML/CSS/JavaScript tabanlı Zekam UI asset'i yoktur.
4. `/api/observatory/*`, UI snapshot, UI SSE/EventSource veya UI asset endpoint'i yoktur.
5. "Canlı Yürütme Gözleme Merkezi", "Neuro Observatory" veya dashboard ürünü yoktur.
6. UI'ye özel application/domain/infrastructure projection katmanı yoktur.
7. UI'ye özel JSON schema yoktur.
8. UI'ye özel E2E/unit/integration/security testi yoktur.
9. UI için eklenen dependency veya optional extra yalnız başka bağımsız ürün yeteneği tarafından gerçekten kullanılmıyorsa kaldırılmıştır.
10. README, Global DoD, operasyon dokümanları ve manifestler UI/dashboard'u mevcut veya hedef özellik olarak anlatmaz.
11. `legacy-preserved` dahil **güncel repository tree** içinde UI'yi korumak amacıyla bırakılmış kopya bulunmaz.
12. Paket manifesti, checksum ve generated active-task projection yeni ağaçla tutarlıdır.
13. UI kaldırılması CLI, scheduler, worker, knowledge, memory, project, research, Jira skill, integration ve diğer non-UI yetenekleri bozmaz.
14. Git geçmişi yeniden yazılmaz; hedef güncel source tree'dir.

---

## 4. Çok önemli sınır: UI ile observability/API aynı şey değildir

Bu görev **"UI var diye bütün observability'yi sil"** görevi değildir.

Aşağıdakiler UI'den bağımsız oldukları kanıtlanırsa korunabilir:

- structured telemetry,
- machine-readable health/status çıktıları,
- CLI `--json` raporları,
- scheduler/worker metrikleri,
- memory health/observability servisleri,
- agent/client protocol contract'ları,
- App Server domain/application protocol'ü,
- MCP veya başka headless entegrasyon yüzeyleri,
- UI göstermeyen, bağımsız ve gerçekten kullanılan bir API transport'u.

Ancak bir katman sadece UI/dashboard'u beslemek için varsa **"ileride lazım olur" gerekçesiyle tutulamaz**.

Özellikle App Server için şu karar ağacı uygulanacaktır:

```text
App Server domain/application contract bağımsız kullanılıyor mu?
  evet -> koru.

FastAPI/WebSocket transport bağımsız, desteklenen bir headless entrypoint'e sahip mi?
  evet -> UI hostundan ayır; observatory/static bağımlılığı olmadan koru.
  hayır -> dead transport olarak kaldır.

SQLiteLocalProjectionStore App Server tarafından gerçekten gerekiyorsa?
  evet -> UI/observatory isimlendirmesinden çıkar, nötr bir modüle taşı.
  hayır -> UI projection ile birlikte kaldır.
```

Bu ayrım yapılmadan `src/zekam/interfaces/api/`, FastAPI veya App Server topluca silinmeyecektir.

---

## 5. Baseline'da doğrulanmış UI izi

Aşağıdaki alanlar `main@68b4833...` üzerinde doğrudan doğrulanmıştır ve uygulama sırasında ilk inceleme kümesidir.

### 5.1 Doğrudan UI yüzeyi

- `src/zekam/interfaces/cli/ui.py`
- `src/zekam/interfaces/api/observatory.py`
- `src/zekam/interfaces/api/static/index.html`
- `src/zekam/interfaces/api/static/styles.css`
- `src/zekam/interfaces/api/static/app.js`
- `src/zekam/application/observatory.py`
- `schemas/observatory_snapshot.schema.json`

### 5.2 UI wiring ve ürün sözleşmesi

- `src/zekam/interfaces/cli/main.py`
  - `ui_commands` importu
  - `app.add_typer(ui_commands.app)` kaydı
- `src/zekam/domain/observability.py`
  - `ui serve` canonical command kaydı
  - dashboard/graph'a özel domain modelleri varsa bunların reachability'si
- `pyproject.toml`
  - `[project.optional-dependencies].api`
  - `fastapi`, `uvicorn`, `psutil` bağımlılıklarının gerçek non-UI kullanım durumu
- `README.md`
  - geliştirme kurulumundaki `.[api,dev]`
  - `Zekam Canlı Yürütme Gözleme Merkezi` bölümü
  - `zekam ui serve` örnekleri
- `GLOBAL_DEFINITION_OF_DONE.md`
  - `Scheduler, rapor ve dashboard` başlığı
  - Dashboard zorunlulukları
  - Obsidian/sinaps benzeri graph görünümü zorunluluğu
- `PROJE_MANIFESTI.yaml`
  - `dashboard_is_authority`
  - `api` process kaydının UI'den bağımsız olup olmadığı
- `operasyon/OBSERVABILITY_DASHBOARD_RAPORLAMA.md`
- `PACKAGE_MANIFEST.json`
- `SHA256SUMS.txt`

### 5.3 UI testleri

En az:

- `tests/e2e/test_ui_live_observatory.py`
- `tests/unit/test_observatory.py`
- `tests/unit/test_observatory_assets.py`
- `tests/security/test_observatory_security.py`
- `tests/integration/test_local_observatory_sqlite.py`

Bunlar yalnız başlangıç listesidir. İsimleri farklı olup UI contract'ını test eden başka testler discovery sırasında bulunursa onlar da kaldırılmalı veya non-UI davranışı test edecek şekilde ayrıştırılmalıdır.

### 5.4 İncelenecek ortak / sınır modüller

Aşağıdaki dosyalar körlemesine silinmeyecek; önce UI-dışı reachability analizi yapılacaktır:

- `src/zekam/infrastructure/sqlite/local_observatory.py`
- `src/zekam/domain/observability.py`
- `src/zekam/application/loop_observatory.py`
- `src/zekam/application/memory_observability.py`
- `src/zekam/interfaces/api/app_server.py`
- `src/zekam/application/app_server.py`
- `src/zekam/domain/app_server_protocol.py`
- `schemas/app-server-protocol-v1.schema.json`
- `tests/unit/test_app_server_transport.py`

Karar kuralı: **UI olmadan anlamlı ve desteklenen bir kullanım yolu varsa koru/refactor et; yalnız UI'nin taşıdığı ölü kod ise sil.**

### 5.5 Legacy-preserved içindeki doğrulanmış UI kalıntıları

Güncel tree'de en az şu UI kalıntıları vardır:

- `legacy-preserved/active-docs-postgresql/docs/UI_NEURO_OBSERVATORY_MIMARISI.md`
- `legacy-preserved/active-importer-preimages/src/zekam/interfaces/api/observatory.py`
- `legacy-preserved/src-postgres/observatory_projection.py`

Bu görev kullanıcının açık UI kaldırma kararı olduğu için bunlar "legacy korunmalı" bahanesiyle current tree'de tutulmayacaktır. Tarihsel bilgi Git geçmişinde zaten mevcuttur; history rewrite yapılmaz.

---

## 6. Kapsam

### Kapsam dahil

- UI CLI komutlarının kaldırılması.
- UI web server/route/static asset katmanının kaldırılması.
- UI'ye özel application servisleri ve projection modellerinin kaldırılması.
- UI'ye özel SQLite/read model adapter'larının kaldırılması veya shared kısımlarının nötr modüle ayrıştırılması.
- UI'ye özel schema'ların kaldırılması.
- UI'ye özel testlerin kaldırılması.
- UI için var olan dependency/extra'ların reachability sonucuna göre kaldırılması.
- README ve kurulum komutlarının güncellenmesi.
- Global DoD'daki dashboard/graph UI gereksinimlerinin kaldırılması.
- Operasyon dokümanlarının UI'siz observability/reporting modeline çevrilmesi veya gereksizse silinmesi.
- `PROJE_MANIFESTI.yaml` içindeki UI/dashboard sözleşmelerinin kaldırılması.
- current tree'deki legacy-preserved UI artifact'larının kaldırılması.
- package manifest/checksum/generated projection yenilenmesi.
- dead-code ve import cleanup.

### Kapsam dışı

- Yeni web UI yapmak.
- TUI/curses/Textual benzeri terminal UI yapmak.
- Dashboard yerine başka görsel panel yapmak.
- Electron/desktop/mobile UI yapmak.
- Git geçmişini rewrite/filter-repo ile temizlemek.
- UI kaldırma bahanesiyle unrelated business logic'i yeniden tasarlamak.
- PostgreSQL'i geri getirmek.
- Docker'ı core dependency yapmak.
- Push yapmak.

---

## 7. Zorunlu discovery — mutation öncesi

Mutation başlamadan önce gerçek source rootunda repo-wide tarama yapılacaktır.

En az aşağıdaki kavramlar için path + content taraması yap:

```text
ui
ui serve
Canlı Yürütme Gözleme Merkezi
Neuro Observatory
observatory
observatory_snapshot
/api/observatory
EventSource
/assets/
dashboard
UI_NEURO_OBSERVATORY_MIMARISI
fastapi
uvicorn
StaticFiles
FileResponse
StreamingResponse
zekam-dashboard
zekam-observatory
```

Tek başına `ui` substring'iyle toplu silme yapılmayacak; `build`, `suite` vb. yanlış eşleşmeler filtrelenecektir.

Her bulunan öğe şu sınıflardan birine atanmalıdır:

- `DELETE_UI_ONLY`
- `REFACTOR_SHARED_UI_COUPLING`
- `KEEP_NON_UI_PROVEN`
- `DOC_CONTRACT_REMOVE`
- `LEGACY_TREE_REMOVE`

Mutation planında her dosyanın sınıfı ve gerekçesi bulunmalıdır.

Ayrıca import/reachability analizi yapılmalıdır:

- hangi modül kimi import ediyor,
- CLI'dan hangi entrypoint gerçekten erişilebilir,
- wheel içine hangi dosyalar giriyor,
- optional dependency'leri kim kullanıyor,
- App Server transport'un UI dışında gerçek entrypoint'i var mı,
- `psutil` başka non-UI süreç gözleminde kullanılıyor mu,
- dashboard/graph domain tipleri başka machine-readable akışlarda gerçekten kullanılıyor mu.

---

## 8. Uygulama fazları

### Faz A — Baseline ve güvenli plan

1. Başlangıç protokolünü çalıştır.
2. Exact HEAD ve dirty state'i kaydet.
3. Existing active Work/lease/recovery durumunu doğrula.
4. En az bir gerçek subagent ile bağımsız UI footprint discovery yaptır.
5. Ana agent ve subagent sonuçlarını birleştirerek dosya bazlı deletion/refactor matrisi oluştur.
6. Değişiklik öncesi mevcut test baseline'ını kaydet.

### Faz B — Ürün yüzeyini kaldır

Kesin olarak:

- `zekam ui` komut ağacını kaldır.
- `src/zekam/interfaces/cli/ui.py` dosyasını kaldır.
- CLI main wiring'ini temizle.
- `ui serve` canonical command kaydını kaldır.
- browser UI hostunu kaldır.
- `/` HTML route'unu kaldır.
- `/assets/*` mount'unu kaldır.
- `/api/observatory/*` endpoint'lerini kaldır.
- observatory SSE/EventSource stream'ini kaldır.
- static HTML/CSS/JS asset dizinini kaldır.

Başka bir komut altında aynı UI'yi yeniden sunma.

### Faz C — UI projection/application katmanını temizle

- `src/zekam/application/observatory.py` UI-only ise tamamen kaldır.
- `schemas/observatory_snapshot.schema.json` kaldır.
- `zekam-observatory-*`, `zekam-dashboard-*`, UI graph snapshot sözleşmelerini kullanan kodu temizle.
- `src/zekam/infrastructure/sqlite/local_observatory.py` içindeki UI runtime projection reader'ı kaldır.
- Aynı dosyadaki App Server için gerekli store bağımsız kullanılıyorsa nötr isimli bir modüle taşı; UI/observatory import zinciri kalmasın.
- `src/zekam/domain/observability.py` içindeki generic telemetry/command contract korunabilir; UI/dashboard/graph'a özel sınıf ve sabitlerin kullanımını kanıtla. Ölü olanları kaldır.
- `loop_observatory.py` ve `memory_observability.py` sadece isim benzerliği nedeniyle silinmez. Headless çalışma/doctor/CLI raporlamasında kullanılıyorsa kalır. UI'den miras kalan yanlış adlandırma varsa davranış değiştirmeden daha nötr isimlendirme değerlendirilebilir.

### Faz D — App Server / API ayrıştırması

`src/zekam/interfaces/api/observatory.py` bugün App Server route'larını da kuruyor. UI kaldırılırken App Server'ın kaderi açıkça belirlenmelidir.

1. `AppServerConnection` ve `app_server_protocol` UI'den bağımsız domain/application contract ise korunur.
2. FastAPI `install_app_server_routes` transport'u UI dışında desteklenen gerçek bir server entrypoint'e sahipse o entrypoint UI'siz hale getirilir.
3. Transport'un tek host'u `zekam ui serve` ise yeni gereksiz server ürünü icat etme; transport dead code olarak kaldırılabilir, fakat domain protocol ancak ayrıca dead olduğu kanıtlanırsa kaldırılır.
4. Eğer bağımsız headless API gerçekten korunuyorsa:
   - HTML/static route bulunmayacak,
   - dashboard/observatory endpoint'i bulunmayacak,
   - adı ve kurulum metni UI çağrıştırmayacak,
   - testleri yalnız machine protocol davranışını doğrulayacak.

### Faz E — Dependency ve packaging cleanup

`pyproject.toml` için reachability bazlı karar ver:

- `fastapi`: yalnız kalan bağımsız headless transport gerektiriyorsa kalsın.
- `uvicorn`: gerçekten desteklenen server entrypoint'i varsa kalsın; yalnız UI serve içinse sil.
- `psutil`: non-UI process observation gerçekten kullanıyorsa kalsın; yalnız UI agent paneli içinse sil.
- `[project.optional-dependencies].api`: içerik tamamen boşalırsa kaldır. Headless API kalırsa ad ve amaç UI'siz olacak şekilde korunabilir.
- README kurulum komutu buna göre `.[dev]`, `.[server,dev]` veya gerçek kalan extra ile güncellenir. Olmayan extra yazılmaz.

Wheel/package-data içinde statik UI asset kalmadığını doğrula.

### Faz F — Dokümantasyon ve sözleşme cleanup

Aşağıdakiler güncellenecektir:

- `README.md`: UI bölümü ve `zekam ui serve` örnekleri kaldırılacak.
- `GLOBAL_DEFINITION_OF_DONE.md`:
  - Bölüm I dashboard merkezli olmaktan çıkarılacak.
  - Dashboard gösterim şartı kaldırılacak.
  - Obsidian/sinaps graph görünümü UI şartı kaldırılacak.
  - scheduler, telemetry ve insan/machine-readable rapor gereksinimleri korunabilir.
- `PROJE_MANIFESTI.yaml`:
  - `dashboard_is_authority` kaldırılacak.
  - `api` process yalnız bağımsız headless API kanıtlanıyorsa kalacak.
- `operasyon/OBSERVABILITY_DASHBOARD_RAPORLAMA.md`:
  - dashboard ürünü anlatımı silinecek.
  - dosya gerekli telemetry/reporting içeriği taşıyorsa `OBSERVABILITY_VE_RAPORLAMA.md` benzeri UI'siz bir adla taşınacak ve içerik sadeleştirilecek.
  - UI'ye özgü minimum sayfalar, click-through, görsel graph vb. kaldırılacak.
- UI mimarisini anlatan başka docs/referanslar kaldırılacak.
- UI future roadmap veya TODO olarak da bırakılmayacak.

### Faz G — Test cleanup ve regression kanıtı

UI varlığını doğrulayan testler silinecek:

- live UI HTTP E2E,
- static asset içerik testleri,
- UI LAN binding testleri,
- observatory web security-header testleri,
- observatory snapshot schema/UI projection testleri,
- UI-only SQLite projection testleri.

Ancak shared non-UI logic UI testlerinden ayrılıyorsa ilgili testler nötr test dosyasına taşınacaktır.

Yeni/uyarlanmış negatif kabul testleri en az şunları doğrulamalıdır:

- CLI help'te `ui` command yok.
- `zekam ui` çağrısı desteklenmiyor.
- package içinde `interfaces/cli/ui.py` yok.
- package içinde `interfaces/api/static/` yok.
- `observatory_snapshot.schema.json` yok.
- source tree'de `/api/observatory` route'u yok.
- README UI çalıştırma talimatı içermiyor.
- package manifest UI artifact'i içermiyor.

### Faz H — Legacy-preserved cleanup

Current tree'deki UI legacy dosyaları kaldırılacaktır. En az doğrulanmış üç dosya bölüm 5.5'te listelenmiştir.

`legacy-preserved` altında başka `observatory`, dashboard veya UI preimage bulunduysa ve yalnız kaldırılan UI'yi koruyorsa o da silinir.

Bu işlem Git geçmişini değiştirmez. Kullanıcı source tree'de UI kalmamasını istemiştir; geçmiş commitler bu görevin kapsamı değildir.

### Faz I — Manifest, checksum ve projection

Tüm değişikliklerden sonra:

1. `PACKAGE_MANIFEST.json` repository'nin mevcut generator/validator yöntemiyle güncellenecek.
2. `SHA256SUMS.txt` aynı kanonik yöntemle güncellenecek.
3. `AKTIF_GOREV.yaml`, yeni `AKTIF_GOREV.md` exact digest'inden deterministik üretilecek.
4. Generated projection'a bağımsız scope/state/yetki yazılmayacak.
5. `python scripts/paket_dogrula.py` PASS vermeli.

Generator yoksa mevcut formatı elle taklit etmeden önce repository contract'ı incelenecek; deterministic mevcut mekanizma tercih edilecek.

---

## 9. Kabul kriterleri

Görev yalnız aşağıdaki kapıların tamamı geçerse tamamlanmıştır.

### AC-01 — CLI yüzeyi

```text
zekam --help
```

çıktısında `ui` komutu yoktur.

`zekam ui` artık ürün özelliği değildir ve başarılı server başlatamaz.

### AC-02 — Web UI asset yok

Aşağıdaki path'ler current tree'de yoktur:

```text
src/zekam/interfaces/cli/ui.py
src/zekam/interfaces/api/observatory.py
src/zekam/interfaces/api/static/
schemas/observatory_snapshot.schema.json
```

### AC-03 — UI route yok

Executable source içinde aşağıdakiler yoktur:

```text
/api/observatory
observatory-assets
StaticFiles(... UI asset ...)
index.html UI serving
EventSource tabanli UI stream
Zekam Canli Yurutme Gozleme Merkezi
Neuro Observatory
```

### AC-04 — UI product contract yok

`CANONICAL_COMMANDS`, Global DoD, README, project manifest ve operasyon belgeleri UI/dashboard'u ürün capability'si veya hedefi olarak göstermiyor.

### AC-05 — UI-only backend yok

UI snapshot/graph/dashboard için yazılmış ve başka kanıtlı consumer'ı olmayan application/domain/infrastructure kodu kaldırılmıştır.

### AC-06 — Shared backend güvenli

Observability, App Server veya process observation'dan korunan her parça için non-UI consumer/test kanıtı vardır. "Belki ileride kullanılır" kabul edilmez.

### AC-07 — Dependency temizliği

FastAPI/uvicorn/psutil ve `api` extra için dosya bazlı reachability kararı kayıtlıdır. UI-only dependency kalmamıştır.

### AC-08 — Test temizliği

UI'yi ayağa kaldıran veya UI assetlerini doğrulayan test yoktur. Kalan test suite yeni UI'siz ürünü doğrular.

### AC-09 — Legacy current tree temizliği

`legacy-preserved` altında kaldırılan UI'nin source/doc kopyaları bulunmaz.

### AC-10 — Paket bütünlüğü

En az:

```text
python scripts/paket_dogrula.py
pytest
ruff check
mypy
```

repo standardındaki gerçek komutlarla geçer. Komut isimleri repo tooling'inde farklıysa mevcut canonical quality scriptleri kullanılır ve exit code'lar kaydedilir.

### AC-11 — Dead code

Mevcut dead-code/reachability kontrolü geçer. UI kaldırıldıktan sonra import edilemeyen, kullanılmayan veya yalnız eski testlere bağlı modül kalmaz.

### AC-12 — Final residue scan

Final scan exact product/UI terimleri için çalıştırılır. UI özelliğini ifade eden eşleşme kalmamalıdır.

Generic teknik kelimeler (`Surface.API`, `observability`, rapor, telemetry vb.) ancak gerçekten non-UI anlamda kullanılıyorsa kalabilir.

---

## 10. Final residue scan standardı

Finalde en az şu taramalar veya platform eşdeğerleri uygulanacaktır:

```bash
git grep -n -I -E 'zekam ui|ui serve|Canl[iı] Y[uü]r[uü]tme G[oö]zleme Merkezi|Neuro Observatory|/api/observatory|zekam-observatory|zekam-dashboard|observatory_snapshot|observatory-assets|UI_NEURO_OBSERVATORY_MIMARISI'

git ls-files | grep -Ei '(^|/)(ui)([._/-]|$)|observatory|dashboard|interfaces/api/static'
```

Beklenen sonuç otomatik olarak "mutlak sıfır eşleşme" değildir; çünkü örneğin bu yaşayan `AKTIF_GOREV.md` görevin neyi kaldırdığını açıklamak zorundadır ve generic `memory_observability.py` non-UI olabilir.

Bu nedenle final verifier her eşleşmeyi sınıflandırmalıdır:

- aktif ürün UI kalıntısı → **FAIL**
- test/docs içinde UI'yi mevcut özellik olarak anlatan kalıntı → **FAIL**
- legacy-preserved UI kopyası → **FAIL**
- bu aktif görevin tarihsel/kapsam açıklaması → izinli
- gerçekten non-UI observability/telemetry → kanıtla izinli

Verifier raporunda izin verilen her eşleşmenin path + gerekçesi bulunmalıdır.

---

## 11. Geri dönüş / güvenlik yaklaşımı

Bu görev ağırlıklı olarak deletion/refactor işidir.

Kurallar:

- Deletion öncesi exact baseline commit ve changed-file listesi kaydedilir.
- Unrelated kullanıcı dosyaları silinmez.
- Secret/config içeriği artifact'e taşınmaz.
- UI kaldırma sırasında data migration yapılmaz.
- Yerel operational SQLite authority kayıtları UI kaldırıldığı için silinmez.
- UI projection için ayrı türetilmiş data dosyası varsa ve runtime tarafından yeniden üretilebiliyorsa cleanup mevcut güvenli lifecycle'a göre yapılır; kullanıcı datası olduğu belirsiz dosya otomatik silinmez.
- Rollback kodu eski UI'yi ayrı legacy klasöre kopyalamak değildir; Git source revision geri dönüş noktasıdır.

---

## 12. Bağımsız verifier görevi

Ana builder'dan ayrı gerçek subagent/verifier şu sorulara cevap vermelidir:

1. Current tree'de çalışan veya dokümante edilmiş herhangi bir UI kaldı mı?
2. `zekam ui` tamamen kalktı mı?
3. HTML/CSS/JS UI asset'i kaldı mı?
4. `/api/observatory` veya SSE UI route'u kaldı mı?
5. Dashboard/UI requirement'ı Global DoD veya manifestte kaldı mı?
6. UI-only projection/schema/test kaldı mı?
7. Legacy-preserved içinde UI kopyası kaldı mı?
8. Korunan observability/App Server parçalarının non-UI consumer kanıtı var mı?
9. UI kaldırılması unrelated CLI/runtime/knowledge/memory işlevlerini bozdu mu?
10. Package/manifest/checksum/projection tutarlı mı?

Bu verifier PASS vermeden Work Item terminal success olamaz.

---

## 13. Tamamlandı sayılmayacak durumlar

Aşağıdakilerden biri varsa görev tamamlanmamıştır:

- sadece HTML/CSS/JS silinmiş ama `zekam ui` duruyorsa,
- CLI kaldırılmış ama observatory API/routes duruyorsa,
- source silinmiş ama UI schema/test/docs duruyorsa,
- README temizlenmiş ama Global DoD dashboard istiyorsa,
- current legacy-preserved içinde UI kopyaları tutuluyorsa,
- `api` extra yalnız artık olmayan UI için dependency taşıyorsa,
- App Server ile UI coupling'i çözülmemişse,
- package manifest/checksum eski dosyaları referanslıyorsa,
- full regression suite başarısızsa,
- verifier yalnız dosya adına bakıp reachability incelememişse,
- yeni bir TUI/dashboard ile eski UI'nin yerine başka UI konmuşsa.

---

## 14. Son çıktı standardı

Uygulayan model final raporunda kısa ve kanıta dayalı olarak şunları vermelidir:

- başlangıç ve bitiş HEAD,
- silinen UI dosyaları,
- refactor edilen shared dosyalar,
- korunan observability/App Server parçaları ve nedenleri,
- kaldırılan dependency/extra'lar,
- güncellenen docs/contracts/manifestler,
- final residue scan sonucu,
- çalıştırılan test/quality komutları ve exit code'ları,
- bağımsız verifier sonucu,
- commit yapıldıysa commit SHA,
- push yapılmadığı.

UI'nin ekran görüntüsü, yeni tasarım önerisi veya alternatif UI sunulmayacaktır.

---

## 15. Nihai hedef

Bu görevden sonra Zekam için beklenen ürün modeli:

```text
human / agent
    |
    +--> CLI
    +--> MCP / client integrations
    +--> headless machine protocols (yalniz gercekten gerekli olanlar)
    +--> scheduler / worker
            |
            +--> application services
                    |
                    +--> canonical local stores
                    +--> knowledge / memory / research / runtime
                    +--> structured telemetry / reports

NO browser UI
NO dashboard
NO static web assets
NO UI server command
NO UI-only API
NO UI-only projection layer
```

Başarı ölçütü "UI varsayılan kapalı" değildir.

Başarı ölçütü:

> **UI ürünün içinde artık mevcut değildir.**
