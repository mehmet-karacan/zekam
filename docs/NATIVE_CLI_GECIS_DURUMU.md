# Native CLI geçişi: durum ve kabul kaydı

Görev: `ZEKAM-NATIVE-CLI-WORKSPACE-001` (5 Ekim 2026). Üç sonuç ayrı raporlanır.

## 1. Kaynak implementasyonu

Tamamlanan: policy v2 (4 istemci, global kurulum varsayılanı kapalı, legacy uzlaştırma, eski
digest korunumu), ince `AGENTS.md` + `CLAUDE.md`/`GEMINI.md`, `opencode.json` ve
`.opencode/agents` coordinator katmanının kaldırılması, opt-in OpenCode bootstrap'ın
`default_agent`/`permission` yazmaması, lifecycle plugin v3 çalışma alanı guard'ı, MCP
varsayılan istemcilerinin eski üçlüde sabitlenmesi, skill projection hedeflerinin
`.agents` + `.claude` olması, legacy plugin digest sahipliği, effective-context ölçümünün
Gemini/skill/config instruction kapsaması, native yardımcı yol (`test measure/evidence`,
`continuity native ...`), capability matrisi, ADR ve runbook.

## 2. Yerel geçiş (bu cihaz)

**Uygulandı (5 Ekim 2026), kullanıcı delegasyonuyla.** Salt okunur planda üç conflict çıktı:
koordinatör ajanı ve Codex hook'u gözden geçirilmiş *eski sürümlerdi* (git geçmişiyle
eşleştirildi: ajan şablonu `bd151ed`, Codex hook `0.153.1`); bunlar için geçmiş digest/sürüm
sahipliği koda eklendi (`opencode_legacy_agent_digests.py`, `_LEGACY_VERSIONS`).
`~/.config/opencode/opencode.json` secret içerdiği için araç onu bilerek yeniden yazmıyor; bu
dosyadan yalnız birebir Zekam'a ait üç anahtar (`default_agent`, `plugin`, legacy `permission`)
tek seferlik, byte-roundtrip'i doğrulanmış JSON düzenlemesiyle çıkarıldı (yedek:
`~/.zekam/quarantine/manual/`, secret değeri çıktıya yazılmadı). Ardından exact plan digest'iyle
`integration sync --scope user --uygula` çalıştırıldı: 13 işlem, receipt üretildi; 6 ajan +
plugin karantinada, Codex/Claude hook ve OpenCode/Claude instruction bölümleri çıkarıldı,
kullanıcı metni korundu. Readback: dört istemci `disabled-clean`, ikinci plan 0 işlem, 0 conflict.
Eski oturumların yüklenmiş bağlamı silinmez; doğrulama temiz yeni oturumda yapılır.

Vendor çözümleme: `opencode`/`codex`/`claude` vendor yollarında, Zekam shim/alias/profile
girdisi yok; `gemini` kurulu değil; Codex `0.160.0`, Claude Code `2.1.268` (< `2.1.277`,
bu yüzden `CLAUDE.md` + `@AGENTS.md` yolu şart).

Açık: Zekam kökündeki üç Jira skill kopyası `managed-artifact-metadata-drift` conflict'i
veriyor (onceki bir ruff-format commit'i `89da67b` sonrası artifact digest'i kaymış olabilir;
doğrulanmadı). Yenileme `skill export` ister; o da aktif yetkili skill revizyonu ister (önceki
görevin açık activation işi) — bu görevde yapılmadı. Diğer kayıtlı projelerin temizliği ayrı
`--scope project` planlarıdır; yapılmadı.

## Kapsam kararı (kullanıcı, 5 Ekim 2026)

Gemini CLI bu görevde **öncelik dışı**: kurulu değil, kabul koşulmayacak; Codex ve Claude Code
ile aynı proje-yerel `AGENTS.md`/`GEMINI.md` yaklaşımının yeterli olacağı varsayıldı. Kod ve
dosyalar (typed identity, `GEMINI.md`) duruyor; "Gemini çalışıyor" iddiası kurulmaz.

Kullanıcı-geneli talimat dosyaları (`~/.claude/CLAUDE.md`, `~/.config/opencode/AGENTS.md`,
`~/.codex/AGENTS.md`) temizlik sonrası Zekam içeriği taşımıyor (ilk ikisi sıfır bayt, üçüncüsü
yok); yeni yönlendirme eklenmedi. Kayıtlı projelerden yalnız `akis` kökünde `AGENTS.md` var ve
Zekam referansı içermiyor; dokunulmadı.

## 3. CLI/model qualification

Gercek, minimum kapsamli kabul (5 Ekim 2026, Windows 11):

| Istemci/surum | Yontem | Sonuc |
|---|---|---|
| Codex `0.160.0` | `codex debug prompt-input` (provider cagrisi yok) 5 dizinde | Zekam koku ve alt dizini: ince giris tam 1 kez; home, baska kayitli proje ve sentetik dizin: Zekam girisi yok; eski managed bootstrap yok |
| Claude Code `2.1.268` | `claude -p` ile 3-6 kucuk canli cagri (model cagrisi, toplam ~USD 1) | Kok ve alt dizinler (`src`, `docs`): giris yuklu (`@AGENTS.md` yolu calisiyor); baska kayitli proje: yuklu degil. Ilk soru bicimi `src`te tutarsiz `NO` verdi, katı alinti sorusu 3 dizinde tutarli; model-yaniti sinirliligi kayitli |
| OpenCode `1.18.34` | `opencode debug skill/config` (provider cagrisi yok) | Uc proje skill dizini de taraniyor; ayni ada sahip kopyalar **tek girise** iniyor (hangi kopyanin kazandigi kosuya gore degisti, kopyalar ozdes oldugundan zararsiz); kullanici global skill'i ayni adli proje skill'ini golgeleyebilir |
| Gemini | - | Kapsam disi (kullanici karari) |

Bu, tam senaryo kabulu degildir: gorev/plan/MCP/subagent davranislari ve baska modeller
denenmedi; `ready`/qualified denmez.

Hiçbir istemci/model/sürüm kombinasyonunda canlı kabul koşulmadı (`not-run`; Gemini:
`not-installed`). "Dört CLI tam destekli" iddiası kurulamaz.

## Kabul matrisi

Durumlar: `test` = otomatik provider-free test; `gözlem` = salt okunur yerel gözlem;
`model` = belgelenmiş keşif modeli, gerçek runtime ölçülmedi; `not-run`.

| ID | Durum | Kanıt |
|---|---|---|
| A01 | gözlem | vendor yolları; shim yok |
| A02 | gözlem (Codex, Claude) | `codex debug prompt-input`; `claude -p` kök+alt dizin; OpenCode doğrudan oturum denenmedi |
| A03 | gözlem + test | Codex/Claude: home ve başka kayıtlı projede giriş yok; plugin: `test_plugin_is_inert_outside_a_zekam_workspace` |
| A04 | test (Bun) | `test_plugin_is_inert_when_marker_is_not_a_zekam_context`; birebir kopya klasörü ayırt edilemez (kopyalar zaten yasak) |
| A05 | kısmi | mevcut reparse/exact-root testleri; Windows'ta symlink ayrıcalığı olmadığından symlink testleri atlanır |
| A06 | test | varsayılan config + `test_legacy_*` (ZEKAM_HOME/kurulum global açmaz) |
| A07 | not-run | kod yolu değişmedi |
| A08 | kısmi | `test_native_continuity.py`, `test_native_unit_test_*` (DB/runtime eksikliği reddi) |
| A09 | test | `opencode.json` yalnız `$schema`; opt-in bootstrap permission/default_agent yazmaz |
| A10 | test (Bun) | `..._precompact_never_blocks_but_stays_queued` |
| A11 | test | `test_config.py` legacy/v2, `test_producers_do_not_regrow_global_artifacts_after_cleanup` |
| A12 | test | `test_dry_run_plans_exact_cleanup...` (dosya ağacı değişmez) |
| A13 | test | aynı + `test_client_integration_sync.py` |
| A14 | test | `test_managed_instruction_migration.py` conflict senaryoları |
| A15 | test | kullanıcı prefix/suffix/CRLF korunumu (migration testleri) |
| A16 | test | crash/retry/rollback/drift (migration + sync testleri) |
| A17 | test | `pending_project_cleanup` ayrı root planı (sync testleri) |
| A18 | test | `..._second_plan_is_noop`, `..._do_not_regrow...` |
| A19 | test | `test_config.py` Gemini identity; v1 digest eşitliği |
| A20 | gözlem | Claude `2.1.268` < `2.1.277`: `@AGENTS.md` fallback gerçek çağrıda çalıştı |
| A21 | gözlem | OpenCode `1.18.34`: aynı ad tek girişe iniyor (kazanan koşuya göre değişti); kullanıcı global skill'i gölgeleyebilir |
| A22–A23 | model | ince giriş boyut testi; skill/referans yerinde (Jira skill `references/`) |
| A24 | test | `test_native_continuity.py` |
| A25–A27 | test | `test_native_unit_test_helper.py`, `..._cli.py` (gerçek Maven/JaCoCo fixture) |
| A28 | kısmi | mevcut admission/claim testleri; native yolda `target-met` Work kapatmaz |
| A29 | test | checkpoint alanları `verified=false`; sonradan ingest pre-effect yetki üretmez |
| A30 | kısmi | ilgili regresyon süitleri (bkz. son rapor) |
| A31 | test | `ClientIntegrationState`: installed/enabled/ready ayrı |
| A32 | kısmi | yeni süreçlerde (codex/claude) eski global bootstrap yok; history silinmedi; etkileşimli yeni oturum doğrulaması kullanıcıda |

## Açık kalanlar

- Dört CLI için gerçek kabul koşusu ve OpenCode çift skill keşfinin ölçümü.
- Gerçek kullanıcı home'u ve diğer projeler için exact plan + yetki + apply/readback.
- Paired değerlendirme ([NATIVE_DEGERLENDIRME_PLANI.md](NATIVE_DEGERLENDIRME_PLANI.md)) çalıştırılmadı.
- Önceki unit-test görevinin qualification/activation açıkları ve Global DoD kapsam dışı olarak açık.

## Test sonuçları (Windows 11, Python 3.13, bu cihaz)

Dizin bazlı tam koşu (yinelenen test dosya adları nedeniyle `tests/` tek seferde toplanamaz):
unit 5481 geçti / 6 kaldı; integration 305 / 1; security 250 / 0; e2e 172 / 5. Koşu sonrası
düzeltilen/uyarlanan: paket manifesti, doctor, OpenCode/resume e2e (açık v2 opt-in), capability
sayıları; ilgili dosyalar yeniden koşuldu ve geçti. Bağımsız review bulguları (H1 permission
temizliği, H2 dirty içerik digest'i, M1-M3, M6) giderildi ve testlendi. Tüm paketler son
düzeltmelerden sonra **bir daha uçtan uca koşulmadı**.

Kalan, bu görevden bağımsız/önceden var: `test_rag_router_probe_fixture` (dış GPU kaynak
dizini yok), `test_unit_test_cli::test_pause_...` (gerçek home DB'sine bağlı),
`test_ci_pytest` (gecici bir `!!!!!…` dosya adi nedeniyle bir kosuda dustu; sonradan
yeniden kosuldu ve gecti — kalici bir home kalintisi degil), `paket_dogrula` git-history secret-pattern bulguları (test fixture'ları).
Ruff ve mypy değişen dosyalarda temiz.

## Kapanis guncellemesi (kalan isler)

- **Jira skill drift'i (neden dogrulandi):** Kaynak paketi (`src/zekam/skills/jira-is-kaydi`) aktif
  revizyondan sonra degismis (`SKILL.md`, `references/kurum-icerik-standardi.md`, script; ayrica
  `89da67b` ruff-format commit'i). Projection'lar aktif revizyonun digest'ine (`9394e78…`) bagli.
  Cozum yeni bir skill revizyonu (propose → degerlendirme → yetkili activation) gerektirir; bu,
  onceki gorevin acik review/activation isidir ve degerlendirme uydurulamaz. **Acik birakildi.**
  Denenen digest'i-geri-alma yaklasimi yetmedi (uc dosya farkli) ve geri alindi.
- **Diger kayitli projeler:** User-scope plan yalniz `zekam` icin pending raporluyor; digerlerinde
  Zekam-managed projection kalintisi yok, ek plan gerekmedi.
- **Paired degerlendirme (provider-free kisim):** bkz. `NATIVE_DEGERLENDIRME_PLANI.md` on olcum.
  Canli kalite karsilastirmasi calistirilmadi.
- Unit test hermetikligi: `test_unit_test_cli::test_pause_...` gercek home yerine gecici home
  kullaniyor (onceden var olan bagimlilik giderildi).

## Kapanış kararları (5 Ekim 2026, delegasyonla)

- **Jira skill kopyaları:** Seçenek B. Yeni revizyon aktivasyonu bağımsız onaylayan (kullanıcı),
  ≥5 gerçek deneme ve rollback planı ister; CLI'da activation komutu yok ve gerçek Jira verisi
  gerekir. Kaynak paket değiştirilmedi; üç eski kopya, OpenCode tarafından tek girişe indiği için
  işlevsel zarar üretmiyor. `integration sync --scope project` conflict'i bilinen açık kayıt
  olarak duruyor.
- **Canlı paired kalite karşılaştırması:** Çalıştırılmadı. Maliyet ve tekrar sayısı sabit
  (5x5x2), eski kolun izole yeniden kurulumu ve önceden sabitlenmiş kabul ölçütü yok; sonuç
  kalite iddiasını kanıtlamaz. Yük karşılaştırması (`NATIVE_DEGERLENDIRME_PLANI.md`) tek ölçülen
  kısım. Kullanıcı bütçe/kapsam verirse plan olduğu gibi çalıştırılır.
