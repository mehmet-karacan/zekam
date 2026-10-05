# ADR-NATIVE-CLI-001: Native-first CLI ve yalın proje bağlamı

- Status: Accepted (kaynak implementasyonu); yerel geçiş ve CLI/model qualification açık
- Date: 2026-10-05
- Authority: `AKTIF_GOREV.md` (`ZEKAM-NATIVE-CLI-WORKSPACE-001`)

## Karar

Kullanıcının seçtiği CLI doğrudan çalışır. Zekam zorunlu koordinatör, default agent, geniş
`allow` izni, global kurulum veya wrapper değildir. Zekam kendi çalışma alanında ince bir
`AGENTS.md` girişi (Claude/Gemini için `@AGENTS.md` uyumluluk dosyaları) sunar; veri, ölçüm,
süreklilik ve doğrulama servisleri `zekam` komutlarıyla isteğe bağlıdır.

## Gerekçe (kaynak bulgusu, ölçülmüş performans iddiası değil)

- Codex, Gemini ve OpenCode proje/ancestor talimat dosyalarını kendisi keşfeder; Claude Code
  `CLAUDE.md` içindeki `@AGENTS.md` ile ortak dosyayı okur. Ayrı global dağıtım gerekmez.
- Agent Skills standardı metadata/gövde/referans ayrımı yapar; skill'ler ihtiyaçta açılır.
- Markdown bir güvenlik duvarı değildir; native sandbox/approval host'a aittir. Zekam'ın
  sorumluluğu kendi admission, claim/receipt ve kanıt kapılarıdır.

## Sonuçlar

- `cli.integrations` v2: yalnız açık seçimle kullanıcı-geneli yönetilen kurulum; varsayılan
  hepsi kapalı; surumsuz eski değerler global kurulum üretmez (legacy uzlaştırma planı).
- Typed identity'ye `gemini` eklendi; eski üç istemcinin digest/receipt yorumu değişmedi.
- Proje-yerel skill projection `.agents` + `.claude`; `.opencode`/`.gemini` kopyası yalnız
  açık ihtiyaçta.
- `opencode.json` ve `.opencode/agents` coordinator katmanı kaldırıldı (metinler
  `docs/archive/opencode-agents` altında keşif dışı). Opt-in global OpenCode bootstrap
  `default_agent`/`permission` yazmaz; lifecycle plugin v3 çalışma alanı guard'ı taşır.
- Zorunlu alt ajan sayacı kaldırıldı (`agentic_operations_minimum: 0`); riskli/yıkıcı
  değişiklik ve Work kapanışı bağımsız doğrulamayı korur.
- Test ölçümü ve süreklilik OpenCode executable'ından ayrıldı (`test measure/evidence`,
  `continuity native ...`); eski explicit OpenCode batch yolu korunur.

## Açık riskler

- Dört CLI için gerçek keşif/davranış kabulü koşulmadı; `.agents`+`.claude` çift keşfi
  OpenCode'da ölçülmedi.
- Gerçek kullanıcı home'u ve diğer projelerdeki global kalıntıların temizliği ayrı exact yetki
  bekler. Eski `permission` birleştirmesi otomatik geri alınamaz.
- Sadeleşmenin kalite etkisi paired değerlendirmeyle ölçülmedi; iyileşme iddiası yoktur.
