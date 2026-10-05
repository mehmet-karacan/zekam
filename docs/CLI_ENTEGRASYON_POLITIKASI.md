# CLI Entegrasyon Politikası (native-first, v2)

Kullanıcı OpenCode, Codex, Claude Code veya Gemini CLI'ı doğrudan açar. Zekam bunların önüne
geçen bir koordinatör, wrapper, proxy veya global kurulum değildir. Zekam kendi çalışma
alanında ince bir giriş sunar; veri, ölçüm, süreklilik ve doğrulama servisleri `zekam`
komutlarıyla isteğe bağlı kullanılır. Karar kaydı: [ADR-NATIVE-CLI-001](adr/ADR-NATIVE-CLI-001.md).

## Proje-yerel giriş

| Dosya | Rol | Keşfeden istemci |
|---|---|---|
| `AGENTS.md` | Ortak ince giriş (amaç, yetki/kanıt sınırı, ihtiyaç halinde açılacaklar) | Codex, OpenCode; Claude Code ve Gemini yeni sürümlerinde doğrudan okuyabilir (sürüm/koşul gerçek kurulumda ayrıca doğrulanır) |
| `CLAUDE.md` | Yalnız `@AGENTS.md` | Claude Code (resmî uyumluluk yolu) |
| `GEMINI.md` | Yalnız `@AGENTS.md` | Gemini CLI (native import) |
| `opencode.json` | Yalnız `$schema`; default agent, geniş `allow`, eager `instructions` yok | OpenCode |

Klasörü açmak görev, provider çağrısı, migration veya tarama başlatmaz. Bir istemcinin Zekam
dosyasını başka bağlamda (üst dizin, JIT) görmesi mümkündür; vaat edilen sınır, Zekam'ın
kapsam dışında kendiliğinden kurulup çalışmamasıdır. `00_BASLA.md` ve `DEVAM_PROTOKOLU.md`
Zekam Work/devam/recovery işinde açılır; her oturumda yüklenmez.

## Policy v2: `cli.integrations`

`cli.integrations` yalnız **kullanıcı-geneli yönetilen** Zekam girişini (instruction, hook,
agent/plugin, MCP bootstrap) seçer. Varsayılan hiçbirini kurmaz:

```yaml
cli:
  integrations:
    version: 2
    opencode: false
    codex: false
    claude-code: false
    gemini: false
```

- `version: 2` taşımayan eski değerler **legacy**'dir. `true` değerleri yeni oturumda global
  kurulum üretmez; `legacy_selection` altında yalnız uzlaştırma/temizlik planı için görünür.
  `zekam integration sync --scope user` v2 yazar (tümü `false`) ve sahipliği ispatlı global
  parçaları temizleyen exact planı üretir. Eski v1 policy digest biçimi değişmez.
- Gemini'nin global Zekam artifact'ı yoktur; `gemini: true` yeni bir global kurulum üretmez.
- `clients` yalnız executable envanteridir. Destekleniyor, seçili, kurulu, keşfedildi ve
  çalıştırılarak doğrulandı ayrı alanlardır; kurulu bir istemci `ready`/qualified sayılmaz.

## Skill keşfi ve projection

Fiziksel projection hedefleri `.agents/skills` (Codex) ve `.claude/skills` (Claude Code). Resmî
belgelere göre OpenCode `.agents`, `.claude` ve `.opencode` dizinlerini, Gemini CLI `.agents`
ve `.gemini` dizinlerini tarar; bu yüzden ayrı `.opencode/skills` veya `.gemini/skills` kopyası
**varsayılan olarak üretilmez**. Yalnız açık v2 `opencode: true` seçimi `.opencode/skills`
kopyasını ekler. Mevcut yönetilen `.opencode/skills` kopyası, `integration sync --scope project`
planıyla (sahiplik + digest + karantina) temizlenir.

Açık kalan: aynı kanonik skill'in `.agents` ve `.claude` altında iki fiziksel kopyası olduğunda
OpenCode'un tek etkin girişe indirip indirmediği gerçek sürümde **ölçülmedi**. İndirmiyorsa
sonraki adım Zekam-owned alias'lara uygulanan dar, proje-yerel native filtredir; kullanıcının
bütün skill keşfini kapatan genel ayar kullanılmaz.

## Status, plan ve apply

`zekam integration status --json` politika, desteklenen yetenek, executable varlığı, yönetilen
artifact, cleanup ihtiyacı, conflict ve effective state'i ayrı raporlar. Status ve `sync`
dry-run config, DB, spool, lock veya karantina oluşturmaz.

```bash
zekam integration sync --scope user --json
zekam integration sync --scope user --plan-digest <digest> --uygula --json
zekam integration sync --scope project --project-root <exact-kok> --json
```

User scope kalıcı policy'yi ve bilinen kullanıcı-genel Zekam artifact'larını, project scope
yalnız registry'de bağlı exact kök projection'larını uzlaştırır. User-scope plan, diğer
projelerdeki kalıntıları `pending_project_cleanup` altında listeler ve onlara yazmaz. Apply,
aynı kaynak/config/root/ownership snapshot'ından üretilmiş exact digest ister; drift varsa
reddedilir. **Gerçek kullanıcı home'una apply, planı gösterdikten sonra ayrı exact yetki
gerektirir.**

## Sahiplik, karantina, rollback

Sahiplik; marker + bilinen/mevcut içerik digest'i + receipt zinciriyle kanıtlanır. Plugin için
mevcut ve gözden geçirilmiş önceki sürümlerin digest'leri korunur
(`LEGACY_LIFECYCLE_PLUGIN_DIGESTS`); bilinmeyen veya kullanıcı tarafından düzenlenmiş içerik
conflict'tir. Kullanıcının `AGENTS.md`/`CLAUDE.md`/`GEMINI.md` içeriği, skill'i, MCP ayarı,
executable'ı, provider/model bağlantısı, credential'ı ve native geçmişi korunur. Shared
config'ten yalnız exact Zekam girdileri çıkarılır.

Bilinen sınır: eski `opencode install` kullanıcı `permission` değerlerini `allow` ile
birleştirmişti. Orijinal değerler kayıtlı olmadığından bu birleştirme ispatlanamaz ve otomatik
geri alınmaz; plan yalnız `default_agent`/plugin referanslarını çıkarır. Kullanıcı `permission`
bloğunu kendi gözden geçirmelidir. Yeni opt-in OpenCode bootstrap `default_agent` ve `permission`
yazmaz; lifecycle plugin (v3) Zekam çalışma alanı dışında hiçbir dizin, spool veya subprocess
etkisi üretmez (marker: `.ai/repository-context.json` + `PROJE_MANIFESTI.yaml`; birebir kopya
klasörü ayırt edemez, kopya projeler zaten yasaktır).

Rollback önce planlanır ve ayrı digest ister; kullanıcı drift'i üzerine yazılmaz. Rollback
eski global dağıtımı geri getirir; bu durumda native-only garanti artık geçerli değildir ve
sonuç böyle raporlanır.

```bash
zekam integration rollback --receipt <receipt-id> --json
zekam integration rollback --receipt <receipt-id> --plan-digest <digest> --uygula --json
```

## Geçiş sırası (kullanıcı cihazı)

1. `zekam integration status --json` ve `zekam integration sync --scope user --json` ile planı oku
   (dry-run). Conflict varsa önce çöz.
2. Planı onayla; exact digest ile `--uygula`.
3. Diğer projeler için her biri ayrı `--scope project --project-root` planıdır.
4. **Yeni** bir native oturum aç; eski oturumun yüklenmiş bağlamı klasör değiştirerek silinmez
   ve history otomatik silinmez. Doğrulama temiz yeni oturumda yapılır.

## Kabul durumu

Bkz. [ZEKAM_YETKINLIK_ENVANTERI.md](ZEKAM_YETKINLIK_ENVANTERI.md). Kaynak implementasyonu,
yerel geçiş ve CLI/model qualification ayrı raporlanır; dört CLI için gerçek kabul koşusu
henüz yapılmamıştır.
