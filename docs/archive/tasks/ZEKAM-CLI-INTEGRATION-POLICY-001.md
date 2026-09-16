---
schema: zekam-active-task/v2
task_id: ZEKAM-CLI-INTEGRATION-POLICY-001
status: APPROVED_ACTIVE_TASK
title: OpenCode Varsayılanlı CLI Entegrasyon Politikası ve Mevcut Entegrasyonların Güvenli Temizlenmesi
created_at: 2026-09-15T00:00:00+03:00
baseline_repository: mehmet-karacan/zekam
baseline_branch: main
baseline_head: 638b6b703226b0888c031d3e38f919779d4b4865
legacy_postgresql_data_import: FORBIDDEN
postgresql_runtime_dependency: FORBIDDEN
docker_required_for_zekam_core: false
push_authorized: false
---

# AKTIF_GOREV.md

> **Özet:** Zekam varsayılan olarak yalnız **OpenCode entegrasyonunu** etkin kabul edecek. Codex ve Claude Code ancak açık kullanıcı seçimiyle etkinleşecek. Önceki kurulumlardan kalan, kapalı istemcilere ait **Zekam tarafından yönetildiği doğrulanmış** skill, hook, plugin ve yapılandırma parçaları kontrollü bir geçişle aktif keşif konumlarından kaldırılacak. Kullanıcının CLI programları, hesapları, modelleri, kendi ayarları ve proje dosyaları korunacak.
>
> **İstenen teslim:** Bu davranışı çalışan koda uygula; konfigürasyon, kurulum/güncelleme, skill dağıtımı, çalışma zamanı kontrolü, mevcut kurulum migrasyonu, geri alma, testler ve ilgili belgeler birlikte tamamlanmalı. Yalnız YAML ekleyip işi bitmiş sayma.
>
> **İnceleme temeli:** GitHub `main`, `638b6b703226b0888c031d3e38f919779d4b4865` commit’inin bu işle ilgili kaynakları. Bu dosya uygulama promptudur; hazırlanırken ürün kodu değiştirilmedi, yerel CLI konfigürasyonları okunmadı veya silinmedi, ürün testleri çalıştırılmadı. Kaynak bağlantıları son bölümdedir.

## 1. Yetki, kapsam ve başlangıç

### 1.1. Bu görev neyi değiştiriyor?

Kullanıcının talebi **Zekam’ın hangi istemcilere entegre olacağını seçmek ve artık seçili olmayan istemcilerdeki eski Zekam entegrasyonlarını temizlemektir.** Buradaki kaldırma, `codex`, `claude` veya başka bir CLI executable’ını kaldırmak değildir. Paket yöneticisinden uninstall, hesap çıkışı, API anahtarı silme, oturum geçmişi temizleme veya model/provider kaydı kaldırma kapsam dışıdır.

OpenCode bir istemcidir; bu seçim OpenCode üzerinden erişilen model sağlayıcılarını sınırlamaz. OpenAI, Anthropic, MiniMax, yerel model veya LiteLLM model kimliklerini bu görev nedeniyle değiştirme. Özellikle sağlayıcı prefix’lerini düşürme. CLI entegrasyon izni, model çağrısı, ağ erişimi veya araç çalıştırma yetkisi değildir.

Mevcut doğrulanmış adapter kimlikleri `opencode`, `codex`, `claude-code` şeklindedir. `claude` adlı ikinci bir kalıcı kimlik üretme. İncelenen `infrastructure/clients/adapters.py` içinde Gemini adapter’ı yoktur. Bu görevde sırf önceki sohbet örneğinde adı geçti diye Gemini desteği uydurma; mevcut destekli istemcileri çalışır hale getir, yeni istemci eklemeyi registry üzerinden genişletilebilir bırak. Yerelde başka destekli adapter bulunursa çağrı ve test kanıtıyla envantere al; desteklenmeyen istemciyi etkinleştirme isteğini görünür hatayla reddet. [R04]

### 1.2. Önce gerçek kaynak kökü ve yaşayan görevi doğrula

1. `AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md`, `PROJE_MANIFESTI.yaml`, mevcut yaşayan `AKTIF_GOREV.md`, onun üretilmiş YAML projection’ı ve ilgili DoD/güvenlik sözleşmelerini oku. Bu yeni dosya dışarıdan verildiyse mevcut aktif görevin üzerine henüz yazma.
2. Kayıtlı gerçek Zekam source rootunu, Git rootunu, origin’i, branch/HEAD’i ve tracked/untracked değişiklikleri doğrula. Kaynak mutation’i yalnız bağlı gerçek kökte yapılacak; geçici clone, mirror veya detached worktree açılmayacak.
3. Daha yeni HEAD varsa bu baseline’a geri dönme. İlgili diff’i inceleyerek aşağıdaki dosya haritasını güncel karşılıklarıyla eşleştir. Temiz ve yetkili durumda yalnız fast-forward hizalama yap; dirty/diverged durumda reset, otomatik stash, zorla checkout veya force-push yapma.
4. En az bir gerçek, salt-okunur subagent ile etki/sahiplik incelemesi; değişikliklerden sonra builder’dan bağımsız verifier kullan. Gerçekte çalıştırılmayan subagent veya test için kanıt üretme. Aynı yazılabilir kaynakta tek writer kuralını koru.
5. Mevcut kalite/test durumunu baseline olarak kaydet. Geçici log, plan ve raporlar kaynak ağacı dışındaki izinli kullanıcı artifact alanında tutulacak. Kaynak/test/kalıcı ürün belgesi değişiklikleri normal tracked dosyalarda yapılacak.

### 1.3. Önceki görevden güvenli geçiş

İncelenen yaşayan görev `ZEKAM-PERSONAL-SKILL-LIFECYCLE-001` idi. Bu görev onun bütün açık işlerini tamamlanmış saymaz veya yeniden uygulamaya başlamaz. Özellikle skill activation/evolution/DoD açıklarını bu CLI seçimi işi içinde sessizce genişletme.

Eski MD/YAML’nin exact bytes/digest’ini, açık work/job/claim/lease/recovery kayıtlarını ve kaynak durumunu kaydet. Çalışan iş varsa mevcut kontrollü checkpoint/pause/recovery akışını kullan; süreci öldürüp state dosyası silme. Eski görevi mevcut arşiv kuralıyla referans olarak koru; açık işler için carry-forward kaydı oluştur, duplicate enqueue yapma.

`application/active_task_contract.py` sözleşmesinde yaşayan kapsam otoritesi MD, YAML ise salt-okunur projection’dır. Yeni görev güvenle benimsendikten sonra YAML’yi mevcut generator ile exact MD bytes’tan üret. Yeni görev kimliği nedeniyle `evolution_runtime.py` veya başka bir transition/digest bağı bozuluyorsa yalnız gerekli sınırlı benimseme desteğini ekle; validator, grant, claim veya receipt kontrolünü devre dışı bırakma. Eski immutable receipt ve izinleri yeniden yazma. [R01, R02]

Bu görev kaynak geliştirmesini ve güvenli temizleme mekanizmasını kapsar. Gerçek kullanıcı dizinlerinde değişiklik, aşağıdaki exact plan/apply akışından geçer. `APPROVED_ACTIVE_TASK` etiketi sınırsız disk, provider veya gelecek işlemler için yetki değildir. Yeni commit/push, canlı model benchmark’ı, haricî veri aktarımı, servis kurulumu ve ilgisiz sistem temizliği yapma.

## 2. Güncel koddan doğrulanmış başlangıç bulguları

| Bulgu | Kaynak / sembol | Bu görevde gerekli karşılık |
|---|---|---|
| Core config’te `clients: []` var; bu bir etkin entegrasyon listesi değil. | `config/zekam.default.yaml`; `application/config.py::ClientSettings`, `_parse_clients`, `Settings` | Executable kayıtları ile kullanıcı tercih politikasını ayır. |
| Kayıtlı executable dosyası config yüklenirken kesin olarak doğrulanıyor. | `ClientSettings.__post_init__` | Kapalı istemcinin artık bulunmayan executable’ı, policy/status/cleanup işlemlerini kilitlemesin; açık istemcinin doğrulaması gevşemesin. |
| Skill projection hedefleri sabit: `.agents/skills/<name>` iki istemciye, `.claude/skills/<name>` Claude’a. | `application/skill_packages.py::build_projection_plan` | Hedefleri etkin policy’den üret; OpenCode-only için ortak `.agents` hedefini kullanma. |
| Skill export aktif paket/scope ve claim/receipt zincirini kontrol ediyor ama istemci seçimi geçirmiyor. | `interfaces/cli/skill.py::export_command`, `_run_claimed_effect` | Mevcut aktivasyon/yetki kontrollerini koruyarak policy bağını ekle. |
| Projection’da `.zekam-managed.json`, artifact/package/semantic digest ve drift kontrolü mevcut. | `application/skill_packages.py` | İkinci sahiplik sistemi icat etme; bunları migration ve removal için genişlet. |
| OpenCode kurulum planı global agent/plugin/config yazıyor; dosya başına atomik yazma ve agent retirement var. | `application/opencode_agent_bootstrap.py`; `interfaces/cli/opencode.py::install_command` | Yeni policy’yi doğrudan kurulum çağrısına da uygula; mevcut per-file atomic yazmayı bütün geçişin transactional olduğu şeklinde yorumlama. |
| Claude/Codex lifecycle hook’u doğrudan yerel spool oluşturabilir/yazabilir. | `interfaces/cli/client.py::hook_command`, `_client_spool` | Kapalı entegrasyonun kalan callback’i yeni kayıt ve entegrasyon yaratmasın. |
| Doctor kayıtlı executable’ları policy filtresi olmadan iletiyor. | `application/composition.py::_client_executables`, `build_doctor_checks` | `installed`, `enabled`, `supported`, `healthy` ayrı raporlansın. |
| Kökte sürekli keşfedilebilir bir `CLAUDE.md` compatibility dosyası var. | `CLAUDE.md` | Zekam’ın bu tracked stub’ını opt-in template’e taşı; kullanıcı projelerindeki aynı adlı dosyaları topluca silme. |
| Paket runtime’ı bazı tarihsel composition dosyalarını özellikle dışlıyor. | `pyproject.toml`, `scripts/audit_runtime_reachability.py` | Yeni davranış çalışan composition’da ve temiz wheel’de doğrulansın; yalnız tarihsel koda yama yapma. |

Bu tablo bütün repository’nin eksiksiz denetlendiği iddiası değildir. Uygulayıcı, bu sembollerin tüm **aktif çağrı noktalarını** yerel kaynakta arayıp etki listesini tamamlamalıdır. [R03–R13]

## 3. Tek ve çelişkisiz konfigürasyon sözleşmesi

### 3.1. Kalıcı tercih

Mevcut core config ve `$ZEKAM_HOME/config.yaml` katmanları içinde şu tek karar alanını kullan:

```yaml
cli:
  integrations:
    opencode: true
    codex: false
    claude-code: false
```

Bunun yanına aynı kararı tekrar eden `mode: opencode-only`, `all-supported`, ikinci `selected_clients` veya ikinci `default` alanı ekleme. `clients` executable envanteri olarak kalacak; orada kayıt bulunması, CLI’ın PATH’te bulunması veya eski dosyaların mevcut olması **opt-in sayılmayacak**.

Core varsayılan, yeni kullanıcı config’i, config alanı bulunmayan eski kurulum ve packaged default aynı sonucu vermeli: yalnız OpenCode etkin. Kullanıcı yeni policy alanını açıkça tanımlamışsa yükseltme onun seçimini sıfırlamamalı. Kısmi override deep-merge edilir: örneğin yalnız `codex: true`, OpenCode’u kapatmadan Codex’i ekler. Bütün istemcileri `false` yapmak geçerli bir entegrasyonsuz durumdur; başka istemciye sessiz fallback yapılmaz.

Doğrulama kuralları:

- Değerler gerçek boolean olmalı; `"false"`, `0`, `null`, liste ve boş string kabul edilmemeli. `bool("false")` benzeri coercion kullanma.
- Bilinmeyen istemci/alan, duplicate YAML key ve bozuk config mutasyon öncesinde hata vermeli. Bozuk config’i “varsayılan OpenCode-only” kabul edip silme başlatma.
- `Settings` tipli policy taşısın; `sanitized()` ve `config explain` bu alanların etkin değerini ve gerçek kaynağını gösterebilsin. Örtük default ile provenance çıktısı çelişmesin.
- Mevcut secret reddi, managed network/permission gereksinimleri ve config provenance sırası korunsun. Policy bir yetki belgesi değildir.
- Bu dar görevde ikinci bir proje-level config katmanı veya bağımsız ortam değişkeni matrisi ekleme. Mevcut session override kanalı bu alanlara uygulanıyorsa kalıcı kullanıcı opt-in’ini genişletmesin ve kalıcı silme gerekçesi olmasın; davranışı açıkça test et.
- Kapalı istemcinin executable metadata’sı korunur. Eksik executable’ın aktif kullanım doğrulaması ile salt-okunur registry/policy çözümlemesini ayır; sadece kapalı istemci için geçersiz dosya yolu cleanup’ı engellemesin. Path/scope güvenliği ve açık istemci kontrolleri aynen sürsün.

### 3.2. Seçili olma, destek ve kurulu olma ayrı şeylerdir

Her istemci için en az `enabled`, `supported_capabilities`, `executable_present`, `managed_artifacts_present`, `cleanup_required`, `conflicts` ve `effective_state` raporla. Durumlar okunabilir ve kararlı olsun: `enabled-ready`, `enabled-not-installed`, `disabled-clean`, `disabled-cleanup-required`, `blocked-conflict`, `unsupported` gibi.

OpenCode bulunmuyorsa varsayılan tercih değişmez; ürün sahte başarı veya otomatik Codex fallback üretmez. Codex/Claude için instruction projection desteği ile reviewed lifecycle desteğini ayrı raporla. Yeni policy, mevcut sürüm/capability doğrulamasının yerine geçmez.

## 4. OpenCode ile diğer istemcilerin keşif alanlarını ayır

**En kritik değişiklik budur.** `.agents/skills` içindeki Zekam skill’ini yerinde tutup yalnız config’te `codex: false` yazmak, onun Codex tarafından keşfedilmesini engellemez.

OpenCode için birinci tercih hedefleri:

| Kapsam | OpenCode’a özel hedef |
|---|---|
| Proje | `<exact-project-root>/.opencode/skills/<skill-name>/` |
| Kullanıcı | OpenCode’un kurulu sürüm/ortam için çözülen config kökü altında `skills/<skill-name>/`; normal yerleşim `~/.config/opencode/skills/` |

Resmî OpenCode belgeleri özel `.opencode/skills` ve global `~/.config/opencode/skills` konumlarını, ayrıca `.agents` ve `.claude` compatibility taramasını belirtir. Uygulamada kurulu sürümün ve varsa config-root override’larının gerçek davranışını doğrula; farklı OpenCode doküman sürümlerinin JSON sözleşmelerini karıştırma. [E01]

Codex ancak açıkken kendi desteklenen `.agents/skills` projection’ını; Claude Code ancak açıkken `.claude/skills` projection’ını almalı. Aynı artifact’ın ortak tüketicileri ve gerçek keşif yolları plan içinde görünmeli. OpenCode açık kaldığı sürece kapalı Codex temizliği OpenCode’un ihtiyaç duyduğu tek kopyayı ortadan kaldıramaz: önce doğrulanmış özel OpenCode kopyasını hazırla, readback doğrula, sonra uygun eski ortak kopyayı kaldır.

OpenCode’un compatibility keşfi nedeniyle iki istemci açıkken aynı isim iki yerden görülebilir. Çift dağıtımı, öncelik/drift durumunu kurulu sürümle doğrula; aynı skill’in farklı içeriklerinin sessizce birbirini ezmesine izin verme. CLI’a özgü varsayımları ortak semantik pakete zorla yazma.

**Önemli sınır:** Bu politika kullanıcı tarafından başka yerlere kopyalanmış talimatları, kullanıcıya ait özel skill yollarını veya üçüncü taraf CLI’ın bütün dosya okuma yetkisini denetleyen bir sandbox değildir. Ortak `AGENTS.md`/başlangıç sözleşmeleri sırf Codex okuyabiliyor diye silinmez. Garantimiz Zekam’ın yönettiği entegrasyonların dağıtım ve çalıştırılma politikasıdır; belirsiz kalıntılar görünür çatışmadır, “tam temiz” değildir.

## 5. Mevcut kurulum için reconcile / kaldırma / geri alma

### 5.1. Envanter

Yeni bir ikinci DB/registry kurma. Mevcut projection sahiplik metadata’sı, canonical package/activation kayıtları, kurulum receipt’leri ve uygun `$ZEKAM_HOME/state/manifests/` alanını kullanarak sürdürülebilir bir managed artifact envanteri oluştur veya genişlet.

Kapsamı bilinen kullanıcı-genel entegrasyon kökleri ve açıkça seçilmiş, registry’de bağlı gerçek proje kökleriyle sınırla. Home diskinin tamamını, başka kullanıcıları veya bütün projeleri recursive tarama. `--home` ile Zekam veri kökü seçimi, hangi işletim sistemi kullanıcısının native CLI config’ine yazılacağını sessizce değiştirmesin; her iki root plan kimliğinde ayrı bağlansın.

En az şu artifact türlerini değerlendir: skill projection dizinleri, Zekam agent/command dosyaları, lifecycle hook kayıtları, plugin dosyaları ve plugin referansları, Zekam’a ait MCP entries, compatibility başlangıç stub’ları. Kaynakta gerçekten mevcut olmayan türler için kurulum varmış gibi davranma. Eski bilinen yerleşimleri yalnız reviewed legacy tanıma kurallarıyla incele.

### 5.2. Sahiplik ve conflict

Silmek için dosya adı, `zekam-` prefix’i, metinde “Zekam” geçmesi veya tek bir marker yeterli değildir. Managed manifest/receipt kimliği, izinli relative path, gerçek içerik digest’i ve kaynak/template/package revision bağı birlikte doğrulanmalı.

Skill projection v1 için mevcut `.zekam-managed.json` sözleşmesini ve gerçek artifact digest’ini doğrula. Eski shared `clients: [codex, opencode]` kaydını yeni politika altında doğrudan geçersiz sayıp üstüne yazma; explicit v1→yeni sözleşme migrasyonu yap. Gerekirse plan/receipt/manifest şemalarını sürümle. Eski receipt’leri yeni bytes’a uyacak şekilde yeniden yazma.

Marker/manifest bulunmayan eski Zekam dosyaları yalnız exact bilinen tarihsel template hash’i veya bağımsız sağlam sahiplik kanıtıyla otomatik sahiplenilebilir. Sadece bugünkü template’e benziyor diye sahiplenme. Mevcut OpenCode bootstrap’ın marker/description heuristic’ini destructive cleanup için tek başına kullanma.

Kullanıcının düzenlediği managed dosya, bilinmeyen dosya, bozuk manifest, checksum drift, symlink/junction veya çelişen sahiplik **conflict** olur. Varsayılan olarak korunur; dosya ve gerekçe sanitize raporlanır. CLI kapalı olsa da kullanıcı dosyasını zorla kaldıran `--force` ekleme. Çatışma varsa etkilenen atomik geçişi uygulama; bağımsız kapsamlar ayrı planlanabilir.

### 5.3. Kaldırma davranışı

Kapatılan istemcinin **doğrulanmış Zekam entegrasyonları**, sadece atlanmayacak; approved migration/apply sırasında aktif keşif alanından kaldırılacak.

- Tamamı Zekam’a ait ve değişmemiş artifact’ı geri alınabilir karantinaya taşı. Kalıcı imha bu görevin varsayılanı değil.
- Paylaşılan JSON/JSONC/TOML/YAML dosyasında yalnız exact yönetilen key, liste elemanı veya işaretli blok kaldırılır. Tüm `settings.json`, `config.toml` veya CLI klasörü silinmez. Kullanıcı alanları, yorumlar, format ve başka entegrasyonlar korunur; güvenli edit desteklenmiyorsa conflict üret.
- Hook matcher/event bloğunda birden çok komut varsa yalnız kanıtlı Zekam komutunu çıkar. MCP entry’sini ad eşleşmesiyle silme; sahiplik ve command/config bağı gerekir. Secret değerlerini rapora, diff çıktısına veya artifact envanterine koyma.
- Kaldırılmış agent/plugin’e işaret eden Zekam-yönetimli default/reference kalmasın. Önceki kullanıcı değerini ancak kayıtlı before-value ve değişmemiş after-value ile güvenli biçimde restore et; önceki değeri tahmin etme.
- Başka etkin istemcinin ihtiyaç duyduğu ortak dosyayı, ona özel doğrulanmış replacement olmadan kaldırma.
- Canonical skill paketi, kullanıcı skill’i, skill activation/outcome geçmişi, operational DB, eski oturumlar, proje notları ve task kayıtları kaldırılmaz. Disabled-client cleanup, skill revoke veya geçmiş veri temizliği değildir.

Karantina **bütün ilgili native keşif ağaçlarının dışında** olmalı. `.agents/skills/.backup`, `.claude/skills/.retired` veya OpenCode’un taradığı bir alt klasöre yalnız dosya adını değiştirerek taşıma; `SKILL.md`/agent dosyaları yeniden keşfedilebilir. `$ZEKAM_HOME` da kullanıcı tarafından bir keşif ağacının içine konmuşsa bunu reddet veya onaylı başka güvenli kök seç; yalnız yol adı güvence değildir. Normal yerleşimde `$ZEKAM_HOME` altındaki ayrı private migration alanı tercih edilebilir.

### 5.4. Plan, transactional apply ve recovery

Plan en az şunları bağlasın: sürüm, task/source kimliği, etkin config/provenance digest’i, kalıcı policy before/after, executable/istemci kimliği, user/project root kimlikleri, kapsam, bütün hedef dosyaların before digest’leri, sahiplik kanıtı, planlanan create/update/quarantine/detach/no-op/conflict işlemleri ve safe rollback referansı.

Plan deterministic, provider-free ve salt-okunur olacak. Plan/doctor/status çalıştırmak config, lock dosyası, spool, DB veya karantina yaratmamalı. Apply flag tek başına yetmez; kullanıcıya gösterilmiş exact plan digest’i ve mevcut mutation admission/claim/receipt koşulları gerekir.

Uygulamadan hemen önce kaynaklar, etkin policy, izinli kökler ve before digest’leri yeniden doğrulanacak. Seçim, dosya veya symlink/junction değişmişse eski planla işlem yapılmayacak. Yol çözümleme Windows reparse point, UNC/case-normalization ve kullanıcı kökü kaçışlarını dikkate almalı; sadece ilk `resolve()` çağrısına güvenme.

Uygulama aşamaları: gerekli özel yeni kopyaları staging’de oluştur → hash/readback doğrula → yönetilen referansları güvenle değiştir → devre dışı eski artifact’ları keşif dışına taşı → nihai native/config readback yap → kalıcı manifest/receipt ve tamamlanma bilgisini yaz. Çok dosyalı işlem için mevcut local effect runtime’ını kullan; dosya başına `replace()` olması bütün geçişin atomik olduğu iddiasına dönüşmesin. Global config ve proje dosyaları farklı filesystem’lerdeyse atomik rename varsayma; doğrulanmış copy/commit/delete sırası ve recovery kaydı gerekir.

Kesintide ya önceki güvenli durum geri gelmeli ya da `recovery-required` açıkça raporlanmalı. Tek-writer/lock ve receipt koşulları uygulanmalı. Tekrar çalıştırma aynı işlemi iki kez yapmamalı; karantina/dosya yedeği çoğaltmamalı. Rollback de planlı ve digest-bağlı olmalı; apply sonrasında kullanıcı değişmişse onun üzerine eski yedek yazmamalı. Başarı ancak gerçek son durum ve receipt readback ile verilebilir.

### 5.5. Skill’in yeniden dağıtılması aktivasyon değildir

Codex/Claude kapatılırken eski shared skill’in OpenCode’a taşınması, revoked/retired/inactive paketi diriltmemeli. OpenCode’a yeni projection için mevcut `require_active_package` ve exact project scope doğrulaması korunacak. Sahiplik temizleme için yeterli olsa da aktivasyon kanıtı yoksa paketi yeniden etkinleştirme; yalnız izinli detach/karantina işlemini yap, eksik OpenCode projection’ını nedenli raporla.

Yeni policy alanı bulunmayan eski kurulumda ilk salt-okunur açılış `migration-required` gösterebilir; kendi kendine silme yapmaz. Açık kurulum/güncelleme/migration apply, aynı policy üzerinden eski kapalı entegrasyonları gerçekten kaldırır. Migrasyon tamamlanana kadar `disabled-clean` veya “tamamen OpenCode-only” raporu verilmez.

## 6. Yeni kullanıcı komutları

Mevcut `client status` lifecycle outbox içindir; anlamını değiştirme. `interfaces/cli/integration.py` altında yeni, küçük bir Typer grubu önerilir. İş kuralları CLI içine gömülmeyecek.

Aşağıdaki **yeni eklenecek** yüzeyi uygula veya mevcut eşdeğer bir yüzey bulunursa tek isimle ona bağla:

```text
zekam integration status --json
zekam integration sync --scope user --json
zekam integration sync --scope user --plan-digest <gosterilen-digest> --uygula --json
zekam integration sync --scope user --enable codex --json
zekam integration sync --scope user --enable codex --plan-digest <gosterilen-digest> --uygula --json
zekam integration sync --scope user --disable codex --json
zekam integration sync --scope project --project-root <exact-kok> --json
zekam integration sync --scope project --project-root <exact-kok> --plan-digest <gosterilen-digest> --uygula --json
zekam integration rollback --receipt <receipt-id> --json
```

`sync` varsayılanı dry-run; `--uygula` verilmeden hiçbir değişiklik yok. `--enable/--disable` aynı istemci için birlikte kullanılamaz. `--scope user` kalıcı kullanıcı tercih değişiklikleri ve kullanıcı-genel entegrasyonlar içindir; `--scope project` mevcut etkin policy’ye göre yalnız exact bağlı projenin projection’larını uzlaştırır. Proje scope’u kullanıcı-genel tercih değiştiremez. Project-root gerekli olduğunda cwd eşleşmesini ve registry binding’ini mevcut yolla doğrula.

Plan ve apply çağrılarında aynı seçim/kapsam parametreleri kullanılacak; farklı seçim eski digest ile uygulanamaz. İki çağrı arasında hesaplanan plan değişirse kullanıcıya yeni plan gösterilir. CLI plan’ı kendiliğinden diske yazarak dry-run sözleşmesini bozma. Rollback için de ayrı plan digest + apply aşaması tanımla; yukarıdaki çağrı sadece geri alma planıdır.

Kullanıcı-genel seçim değiştikten sonra başka kayıtlı projelerde temizlenmesi gereken managed projection’ları bounded metadata ile listele. Yetkisi alınmamış bütün projelere otomatik yazma. Her seçili proje için ayrı exact plan üret; kalan projeler `pending-cleanup` görünür. Kullanıcıya tek global apply’ın bütün proje dosyalarını temizlediği söylenmez.

`zekam config explain cli.integrations.codex --json` mevcut provenance yüzeyinden doğru sonucu göstermeli. Yardım çıktısı “istemci kurulu” ile “Zekam entegrasyonu etkin” farkını açıklamalı.

## 7. Nerede ne değişecek?

Aşağıdaki mevcut dosyalar incelenerek eşleştirildi. **Yeni** işaretli dosyalar önerilen ekleme yerleridir; eşdeğer mevcut bileşen bulunursa çoğaltma.

| Dosya / bileşen | Yapılacak iş |
|---|---|
| `config/zekam.default.yaml` | Tek `cli.integrations` default’unu ekle. Secret-free yapıyı ve `clients` envanterini koru. |
| `src/zekam/application/config.py` | Tipli integration policy’yi `Settings` ve `sanitized()` içine bağla; strict parser/default/provenance; kapalı executable doğrulaması ayrımı. Mevcut config katmanlarını kullan. |
| **Yeni:** `src/zekam/domain/client_integration.py` | I/O’suz policy/istemci kimliği/durum sözleşmesi. Aynı kararın CLI, installer ve runtime’da ayrı ayrı yazılmasını engelle. |
| **Yeni:** `src/zekam/application/client_integrations.py` | Bounded inventory, policy resolution, deterministic plan, sync/migration/rollback orkestrasyonu ve receipt readback. Yeni DB/scheduler oluşturma. |
| Gerektiğinde **yeni:** `src/zekam/infrastructure/clients/managed_artifacts.py` | Native konum çözümleme, güvenli format-aware edit, ownership/digest doğrulama ve dosya işlemleri. Mevcut safe-write/path yardımcılarını kullan. |
| `src/zekam/application/skill_packages.py` | `SkillProjectionPlan`, `build_projection_plan`, `apply_projection_plan`, `projection_receipt` policy’yi ve root/snapshot bağını taşısın. OpenCode özel hedefi, eski shared v1 migrasyonu, disabled hedeflerin deprojection’ı ve stale-policy/replay kontrolünü ekle. |
| `src/zekam/interfaces/cli/skill.py` | `export_command` ve çağırdığı apply/replay yoluna aynı policy snapshot’ını geçir. Aktif revision, exact scope ve `_run_claimed_effect` zincirini atlama. Diğer status/list çıktılarında destek/etkinlik anlamını doğru ayır. |
| `src/zekam/application/opencode_agent_bootstrap.py` | `plan_opencode_agent_bootstrap` / `apply_opencode_agent_bootstrap` ve managed lifecycle plugin başlangıcını policy’ye bağla. Kapalı OpenCode doğrudan installer çağrısıyla geri kurulmasın. Model kataloğu/drift, permission ve retirement kontrollerini koru. Kaldırmada description heuristic yerine güçlü sahiplik kullan. |
| `src/zekam/interfaces/cli/opencode.py` | `install_command` yeni policy/reconcile servisinin aynı kurallarını kullansın. Event/spool/pre-compact yollarında kapalı istemci davranışı açık olsun; salt-okunur status/resume geçmişe erişebilsin. |
| `src/zekam/interfaces/cli/client.py` | `hook_command` ve mutasyon girişlerinde etkinlik kontrolü. Disabled callback’i native protokole uygun, yan etkisiz no-op yap; sahte durable ACK/receipt üretme. Var olan pending outbox/recovery’yi silme veya erişilmez hale getirme. |
| `src/zekam/infrastructure/clients/adapters.py` ve aktif factory/dispatch çağrı noktaları | Adapter implementasyonlarını kaldırma. Etkin registry/selection ve gerçek dispatch öncesi kontrol ile kapalı istemci çağrılarını engelle. Generic adapter ve capability sözleşmesini policy izniyle karıştırma; domain’e application import ettirme. |
| `src/zekam/infrastructure/clients/claude_lifecycle.py`, `codex_lifecycle.py`, ilgili mevcut platform contract’ları | Hook/protokol sürüm doğrulamalarını koru. Cleanup’ın tanıyacağı managed hook kimliğini mevcut reviewed config/fixture’larla eşleştir. Geniş veya belirsiz command substring silme yapma. |
| `src/zekam/application/composition.py`; `src/zekam/infrastructure/doctor/runtime_checks.py` | Aynı çözülmüş policy’yi tüket. Disabled CLI’ı bozuk sayma, repair ile yeniden kurma. Yeni kurulum, kalıntı, çatışma ve paket desteği ayrı raporlansın. |
| `src/zekam/application/fresh_bootstrap.py::_write_config`; `src/zekam/application/setup.py` | Yeni home’da policy/default uyumu; mevcut config’i koruyan upgrade davranışı. Setup’ın plan/apply sözleşmesini ve core-only readiness anlamını bozma. |
| `src/zekam/interfaces/cli/main.py`; **yeni:** `interfaces/cli/integration.py` | Yeni grubu kaydet; bütün CLI yüzeyleri aynı application servisini çağırsın. Eski komut isimlerini keyfî değiştirme. |
| `src/zekam/application/mutation_admission.py`; `application/local_runtime_service.py` ve ilgili composition | Yeni mutasyonları mevcut registry/capability/admission akışına doğru kaydet. Digest’i authority gibi kullanma; geniş bootstrap exemption veya public direct-write bypass ekleme. Mevcut servis yeterliyse yalnız bağla, refactor etme. |
| `CLAUDE.md` → **yeni template:** `config/client-templates/claude-code/CLAUDE.md` | Bu repo-owned compatibility stub’ını varsayılan keşiften çıkar ve opt-in üretime taşı. Template’in başlangıcı güncel `AGENTS.md` / yaşayan MD otoritesine yönelsin; superseded görev veya YAML’yi bağımsız otorite yapmasın. Diğer projelerin kullanıcı `CLAUDE.md` dosyaları korunur. |
| `AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md` | Ortak çekirdek başlangıç sözleşmelerini koru. Gerekliyse yalnız yeni görev/entegrasyon kullanımı bağlantısını güncelle; Codex kapalı diye ortak dosyaları silme. |
| `README.md`, `KURULUM_VE_PROJE_ENTEGRASYONU.md`, `docs/GELISTIRME_KURULUMU.md` | Varsayılan/opt-in, yeni komutlar, scope farkı, migration, conflict ve rollback’i açıkla. README’deki daima ortak OpenCode/Codex projection ifadesini yeni davranışa göre düzelt. |
| **Yeni gerekirse:** `docs/CLI_ENTEGRASYON_POLITIKASI.md` | Operasyon sözleşmesini tek yerde tut: hangi dosya yönetilir, hangisi silinmez, yarım geçiş ve rollback nasıl yürütülür. Bu, ikinci aktif görev dosyası değildir. |
| `pyproject.toml`, `scripts/audit_runtime_reachability.py`, manifest/generator araçları | Yeni runtime dosyaları ve template packaged artifact’ta bulunsun. Tarihsel wheel-excluded composition’ı yeniden etkinleştirme. Aktif görev projection’ı ve checksum/manifestleri mevcut araçlarla yenile; hash’i elle uydurma. |

**Çağrı noktası taraması:** Yerel source içinde `build_projection_plan`, `apply_projection_plan`, `projection_receipt`, `plan_opencode_agent_bootstrap`, `apply_opencode_agent_bootstrap`, `ClientRegistry`, `_parse_clients`, `hook_command`, `.agents`, `.claude`, `CLAUDE.md`, `zekam-lifecycle` ve managed-marker kullanımlarını ara. Kurulum/güncelleme script’i veya otomatik onarım yolu bu fonksiyonları çağırıyorsa onu da aynı policy’ye bağla. Salt tarihsel `legacy-preserved` içeriğini aktif kod sanma veya değiştirme. Bu arama yeni bir genel repository yeniden yazımına dönüşmeyecek.

## 8. Çalışma zamanı ve otomatik yeniden kurulumun engellenmesi

Config’in bulunması tek başına yeterli değildir. Açık kapalı kararı bütün dağıtım ve kullanım girişlerinde aynı kaynaktan okunmalı.

Yeni install/export/upgrade/repair işlemleri yalnız etkin istemcilere artifact üretir. Kapalı istemciyi “eksik kurulum” kabul eden fallback veya self-heal kalmamalı. Doğrudan `opencode install`, skill export, lifecycle callback veya adapter çağrısı policy’yi atlayamaz. Yeniden başlatma ve ikinci upgrade de aynı seçimi korumalı.

Kalıcı kullanıcı opt-in’i olmayan bir istemci, ortamda executable bulunduğu veya bir araştırma/model seçicisi onu önerdiği için etkinleşemez. Runtime policy drift’i launch/effect öncesinde yeniden doğrulanmalı; eski plan/permit var diye kapalı hedef çalıştırılmamalı. Mevcut canonical permit/admission kontrolleri ayrıca sürer.

Disabled callback normal CLI kullanımını sonsuz hata döngüsüne sokmamalı. Native hook protokolüne uygun no-op dönmeli, yeni spool/DB/agent/plugin dosyası yaratmamalı. Mevcut kabul edilmiş pending işlemlerin bounded drain/recovery yolu ise ayrı ve yetkili kalmalı; kapanış “geçmişi çöpe at” anlamına gelmez. Çalışmakta olan native CLI süreçlerini öldürme veya görevlerini silme; yeniden yükleme/oturum yenileme gerekiyorsa raporla.

## 9. Uygulama sırası

**WP-00 — Baseline ve task adoption:** Gerçek kök, açık işler, eski görev, kalite baseline’ı ve etki envanteri. Kaynak doğrulamasında desteklenmeyen CLI’ları ayır.

**WP-01 — Policy:** Tipli sözleşme, default, config/provenance/strict validation ve executable metadata ayrımı. Henüz gerçek kullanıcı dosyalarında cleanup yok.

**WP-02 — Managed hedefler:** OpenCode özel skill konumu; etkin istemciye göre hedef üretme; policy-bound projection/receipt; mevcut activation/scope kontrollerinin korunması.

**WP-03 — Migration:** v1 shared artifact’ları tanı, ownership/conflict kararını ver; dry-run plan, digest-bound apply, keşif dışı karantina, rollback ve failure-injection testleri.

**WP-04 — Tüm girişleri bağla:** OpenCode bootstrap, skill CLI, client hook/dispatch, setup/upgrade/repair/doctor, yeni integration CLI. Kapalı istemcinin tekrar üretilmesini ve çağrılmasını engelle.

**WP-05 — Kaynak stub’ları, belgeler, packaging:** Repo-owned `CLAUDE.md` opt-in template’e taşınsın. Manifest/projection güncel, temiz wheel davranışı doğrulanmış olsun.

**WP-06 — Verifier ve teslim:** Aşağıdaki kabul matrisi gerçek test/son durum kanıtıyla doğrulansın. Üretim cihazındaki dosya değişiklikleri ayrıca exact plan üzerinden uygulanmadıysa “yerel migrasyon uygulanmadı” yaz; kodun hazır olması ile cihazın temiz olması aynı sonuç değildir.

## 10. Testler ve kabul ölçütleri

Mevcut `tests/unit/test_config.py` ve `tests/unit/test_opencode_agent_bootstrap.py` genişletilecek. Skill/lifecycle/provenance/admission/fresh-bootstrap testlerinin gerçek adlarını `git ls-files tests` ile bulup ilgili mevcut testlere ekle; bu dosyada adı doğrulanmamış bir test dosyasını varmış gibi kabul etme. [R14, R15]

Ayrı kapsam gerekirse şu **yeni** dosyaları oluştur:

- `tests/unit/test_client_integration_policy.py`
- `tests/integration/test_client_integration_sync.py`
- `tests/security/test_client_integration_cleanup.py`

Bütün testler geçici, kontrollü HOME/ZEKAM_HOME ve project fixture’larında çalışmalı. Kullanıcının gerçek global CLI config’i, gerçek credentials veya ücretli model API’si test girdisi olamaz.

| No | Senaryo | Beklenen kanıt |
|---|---|---|
| AC-01 | Fresh home / kullanıcı config’i yok / eski config’te policy yok | Yalnız OpenCode etkin; diğer CLI artifact’ları üretilmez. |
| AC-02 | Kullanıcı Codex’i açıyor; sonra tekrar kapatıyor | Önce yalnız gerekli opt-in artifact’ları oluşur; kapanış apply’ında yalnız owned parçalar kaldırılır. |
| AC-03 | Claude-only veya tüm istemciler kapalı | Policy tutarlı; OpenCode otomatik açılmaz, başka CLI fallback’i yok. |
| AC-04 | Partial override ve upgrade | Açık kullanıcı seçimi korunur; `clients` envanteri opt-in sayılmaz. |
| AC-05 | `"false"`, duplicate key, null, bilinmeyen ID, unsupported hedef | Hata; hiçbir hedef dosyasında veya config’te değişiklik yok. |
| AC-06 | Kapalı CLI executable’ı artık yok | Status ve cleanup planı çalışır; açık CLI için eksik executable görünür kalır. |
| AC-07 | Eski v1 `.agents` shared skill + `.claude` projection | Aktif paket önce OpenCode özel hedefinde doğrulanır; sonra kapalı istemci kopyaları karantinaya alınır. |
| AC-08 | Aktif olmayan/revoked paket | Migrasyon paketi yeniden aktive veya export etmez; canonical geçmiş korunur. |
| AC-09 | Aynı isimli user skill / user CLAUDE.md / değiştirilmiş managed dosya | İçerik aynı kalır; conflict ve cleanup eksikliği açık raporlanır. |
| AC-10 | Paylaşılan settings dosyasında Zekam ve başka hook/MCP/plugin | Yalnız doğrulanmış Zekam girdisi değişir; diğer semantic değerler ve yorumlar korunur. |
| AC-11 | Plan sonrası config, hedef içerik, root veya ownership değişiyor | Eski digest reddedilir; mutation yok. |
| AC-12 | Symlink, junction/reparse, path traversal, karantina keşif ağacında | Fail-closed; hedef kökü dışına yazma/silme yok. Native Windows kanıtı ayrıca verilir. |
| AC-13 | Kesinti: staging, reference update, quarantine veya config publish sonrası | Güvenli rollback veya recovery-required; sahte başarı yok. |
| AC-14 | İki eşzamanlı sync/apply | Tek writer; kayıp güncelleme, çift quarantine ve çift receipt yok. |
| AC-15 | Aynı apply yeniden çalıştırılıyor | İdempotent tamamlanma/no-op; native aktif dosya tekrarı oluşmaz. |
| AC-16 | Rollback sonrası/öncesi kullanıcı dosyayı değiştiriyor | Değişmemiş durum güvenle geri alınır; yeni kullanıcı içeriğinin üzerine yazılmaz. |
| AC-17 | Disabled CLI sonrası install/export/upgrade/repair/yeniden başlatma | Kapatılan entegrasyon geri oluşmaz. |
| AC-18 | Disabled CLI’ın kalan hook’u veya doğrudan dispatch’i çağrılıyor | Yeni integration effect yok; native hook no-op, dispatch görünür policy reddi; eski history korunur. |
| AC-19 | Doctor/status/config explain ve dry-run | Salt-okunur; disabled ile unhealthy karışmaz; provenance doğru. |
| AC-20 | User scope’tan sonra başka projelerde kalıntı var | Yetkisiz projeye yazılmaz; pending-cleanup açıkça listelenir. |
| AC-21 | `.opencode` özel skill + native compatibility discovery | OpenCode çalışır; kapalı Codex/Claude’un yönetilen keşif konumlarında aktif kopya kalmaz; duplicate/drift yok. |
| AC-22 | Secret içerebilen native config ve raporlama | Secret diff/log/plan/receipt/artifact çıktısına çıkmaz; credential dosyaları dokunulmadan kalır. |
| AC-23 | Kaynaktan değil temiz wheel’den kullanım | Default config, template ve yeni modüller mevcut; eski dışlanmış composition’a bağımlılık yok. |
| AC-24 | Mevcut güvenlik ve local-first kurallar | Network deny, activation, exact source binding, admission/claim/receipt ve SQLite akışları bozulmaz. |
| AC-25 | Task adoption ve packaging | Eski açık işler korunur, yeni MD/YAML exact uyumlu; checksum/package validator gerçek sonucu kaydedilir. |

Native istemci doğrulaması ile fixture testini ayır. Kurulu sürümün provider çağrısı yapmayan keşif/list/debug yüzeyi varsa kullan; model çağrısı üretme. Native test çalıştırılamıyorsa nedenini ve hangi kabulün yalnız fixture kanıtı taşıdığını yaz. Sadece dosyanın varlığı native yükleme kanıtı değildir.

Asgari doğrulama: hedefli pytest grubu, ilgili bütün regresyon testleri, Ruff, mypy, `python scripts/paket_dogrula.py` ve mevcut packaging/reachability smoke kapıları. Komutları güncel `pyproject.toml` ve script help’iyle doğrula; olmayan CLI bayrağı uydurma. Test/coverage çıktısını kaynak dışına yaz. Önceden var olan hata ile bu patch’in regresyonunu ayrı raporla; test silerek, assertion gevşeterek veya bütün suite’i skip ederek başarı üretme.

## 11. Kapsam dışı işler ve tamamlanma raporu

Yeni model/router ürünü, yeni UI, ikinci skill/evolution platformu, yeni scheduler/DB, PostgreSQL importu, Docker bağımlılığı, Gemini’nin sıfırdan entegrasyonu ve genel kullanıcı dosyası temizliği eklenmeyecek. Var olan model, embedding, research, RAG ve otomasyon ayarları bu değişikliğin yan etkisiyle değiştirilmez.

Son kullanıcı raporu kısa olsun ve şu gerçekleri içersin: değişen dosyalar/işlevler; etkin default ve opt-in komutları; uygulanmış migration’ın scope’u ile kaldırılan/korunan/conflict sayıları; çalıştırılan testler ve native/packaged kanıt ayrımı; kalan blocker/recovery; rollback receipt’i veya uygulanmadı bilgisi. Secret veya uzun terminal çıktısı ekleme.

Bu görev, yalnız bir config alanı eklenmişken veya eski CLI kalıntıları otomatik tekrar oluşurken tamamlanmış sayılamaz. Buna karşılık kullanıcının dosyalarını korumak için bırakılan conflict’ler saklanmaz: kodun tamamlanma durumu ile ilgili cihaz/projenin tam temizlenme durumu ayrı sunulur. İlgisiz Global DoD açıkları kapatılmış gösterilmez.

## 12. İncelenen kaynaklar

Tüm repository bağlantıları aşağıdaki baseline commit’ine sabittir. Uygulayıcı daha yeni HEAD’de aynı sembollerin güncel karşılığını doğrulamalıdır.

- **R01 — Görev/başlangıç otoritesi:** [AGENTS.md](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/AGENTS.md), [active_task_contract.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/application/active_task_contract.py)
- **R02 — Önceki yaşayan görev:** [AKTIF_GOREV.md](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/AKTIF_GOREV.md)
- **R03 — Config:** [zekam.default.yaml](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/config/zekam.default.yaml), [application/config.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/application/config.py), [CLI configuration.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/interfaces/cli/configuration.py)
- **R04 — Gerçek adapter/registry:** [infrastructure/clients/adapters.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/infrastructure/clients/adapters.py)
- **R05 — Skill projection ve sahiplik:** [application/skill_packages.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/application/skill_packages.py)
- **R06 — Skill export ve local effect:** [interfaces/cli/skill.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/interfaces/cli/skill.py)
- **R07 — OpenCode agent/plugin bootstrap:** [application/opencode_agent_bootstrap.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/application/opencode_agent_bootstrap.py), [interfaces/cli/opencode.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/interfaces/cli/opencode.py)
- **R08 — Client lifecycle girişleri:** [interfaces/cli/client.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/interfaces/cli/client.py), [claude_lifecycle.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/infrastructure/clients/claude_lifecycle.py)
- **R09 — Composition ve doctor wiring:** [application/composition.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/application/composition.py)
- **R10 — Native Claude başlangıcı:** [CLAUDE.md](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/CLAUDE.md)
- **R11 — Fresh-home/setup:** [fresh_bootstrap.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/application/fresh_bootstrap.py), [setup.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/application/setup.py)
- **R12 — CLI/admission:** [interfaces/cli/main.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/interfaces/cli/main.py), [application/mutation_admission.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/src/zekam/application/mutation_admission.py)
- **R13 — Paket/ürün belgeleri:** [pyproject.toml](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/pyproject.toml), [README.md](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/README.md)
- **R14 — Config regression:** [tests/unit/test_config.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/tests/unit/test_config.py)
- **R15 — OpenCode bootstrap regression:** [tests/unit/test_opencode_agent_bootstrap.py](https://github.com/mehmet-karacan/zekam/blob/638b6b703226b0888c031d3e38f919779d4b4865/tests/unit/test_opencode_agent_bootstrap.py)
- **E01 — Resmî OpenCode keşif sözleşmesi:** [Agent Skills](https://opencode.ai/docs/skills), [v2 Skills](https://opencode.ai/v2/docs/skills). Erişim: 15 Eylül 2026. Özel/global/compatibility keşif yolları için kullanıldı; kurulu sürümle uyuşan sözleşme esas alınmalı.
