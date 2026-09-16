# CLI Entegrasyon Politikası

Zekam'ın tek kalıcı seçim alanı `cli.integrations` değeridir. Core default ve policy alanı
olmayan eski config şu etkin sonucu üretir:

```yaml
cli:
  integrations:
    opencode: true
    codex: false
    claude-code: false
```

`clients` yalnız executable envanteridir. Kurulum varlığı, PATH sonucu veya geçmiş artifact
opt-in sayılmaz. Değerler gerçek YAML boolean olmalı; bilinmeyen alan ve duplicate key reddedilir.

## Keşif ve sahiplik sınırı

- OpenCode proje skill'i `.opencode/skills`, Codex `.agents/skills`, Claude Code
  `.claude/skills` kullanır.
- OpenCode kullanıcı-genel skill'i `.config/opencode/skills` altındadır.
- Zekam agent/plugin dosyaları yalnız exact shipped template digest'iyle; skill'ler ise
`.zekam-managed.json`, izinli relative path ve gerçek artifact digest'i birlikte doğrulanınca
yönetilmiş kabul edilir. Skill cleanup'ı ayrıca canonical `state/learning.db` içindeki package
revision bağını ister; self-consistent marker tek başına sahiplik kanıtı değildir.
- Kullanıcının `AGENTS.md`, `CLAUDE.md`, özel skill'i, CLI executable'ı, hesabı, provider/model
  ayarı, credential'ı ve geçmiş kaydı temizlenmez.

Root `CLAUDE.md` compatibility stub'ı sürekli keşiften kaldırılmıştır. Opt-in kaynak şablonu
`config/client-templates/claude-code/CLAUDE.md` altında paketlenir.

## Status, plan ve apply

`zekam integration status --json` etkinlik, desteklenen yetenek, executable varlığı, yönetilen
artifact, cleanup ihtiyacı, conflict ve effective state alanlarını ayrı raporlar. Status ve
`sync` dry-run config, DB, spool, lock veya karantina oluşturmaz.

```bash
zekam integration sync --scope user --disable codex --json
zekam integration sync --scope user --disable codex --plan-digest <digest> --uygula --json
zekam integration sync --scope project --project-root <exact-kok> --json
```

User scope kalıcı policy ve bilinen kullanıcı-genel Zekam artifact'larını; project scope yalnız
registry'de bağlı exact project root projection'larını uzlaştırır. Project scope policy
değiştiremez. User-scope opt-in, yalnız etkin Codex/Claude Code için yönetilen instruction ve
reviewed lifecycle hook parçalarını üretir; disabled istemciye stub yazmaz. Apply, aynı
kaynak/config/root/ownership snapshot'ından üretilmiş exact digest ile mevcut claim/receipt
zincirinde çalışır.

User-scope plan, registry'de çözülen diğer gerçek proje köklerinde ayrı project-scope plan
gerektiren kalıntıları `pending_project_cleanup` altında yalnız root identity digest'i ve bounded
sayılarla listeler; bu projelere user apply sırasında yazmaz.

## Karantina, conflict ve rollback

Doğrulanmış kapalı-istemci artifact'ı `$ZEKAM_HOME/quarantine/client-integrations` altına,
native keşif ağaçlarının dışına taşınır. OpenCode shared JSON içinden yalnız exact Zekam plugin
referansı ve doğrulanmış Zekam default agent referansı çıkarılır; diğer semantic değerler
korunur. Symlink/junction, digest drift, değiştirilmiş managed dosya, belirsiz sahiplik veya
replacement eksikliği conflict'tir ve atomik kapsam uygulanmaz.

Başarılı apply immutable receipt üretir. Geri alma da önce planlanır ve ayrı exact digest ister:

```bash
zekam integration rollback --receipt <receipt-id> --json
zekam integration rollback --receipt <receipt-id> --plan-digest <digest> --uygula --json
```

Rollback yalnız terminal durum değişmemişse config backup'ını ve karantinadaki artifact'ı geri
yükler. Kullanıcı drift'i üzerine yazılmaz; kesinti telafi edilemiyorsa sahte başarı yerine
`recovery-required` üretilir. Hiçbir bu işlem provider/model çağrısı veya yeni yetki vermez.
