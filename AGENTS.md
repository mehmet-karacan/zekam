# Zekam

Zekam yerel-öncelikli bilgi, görev, kanıt, yedekleme ve test ölçümü platformudur. Bu kök Zekam'ın
kendi kaynak çalışma alanıdır. Coding CLI'ı (OpenCode, Codex, Claude Code, Gemini CLI) doğrudan
açılır; model seçimi, planlama, araçlar, MCP, skill keşfi, alt ajanlar ve güvenlik/onay
mekanizmaları CLI'ın kendisine aittir. Zekam zorunlu koordinatör veya ajan yöneticisi değildir.

## Çalışma

- Selamlama, genel soru, basit kaynak inceleme ve küçük doküman düzeltmesi için doctor, RAG,
  router veya subagent adımı gerekmez. Doğrudan çalış.
- Kod değişikliğini bu gerçek kaynak kökünde yap; kopya, mirror, detached worktree veya geçici
  proje klonu oluşturma. Geçici rapor, memo, indirilen artifact veya başka projenin dosyasını bu
  köke yazma; yalnız yetkili tracked kaynak/test/migration/belge değişikliği yapılır.
- Secret, PII ve ham transcript'i prompt, log, artifact, vector veya Git'e alma.
- Commit mesajı Türkçe anlamlı ve ASCII-only olur. Push yalnız açık kullanıcı yetkisiyle yapılır.
- Legacy PostgreSQL veri importu ve core için PostgreSQL/Docker bağımlılığı yasaktır.

## Yetki ve kanıt

- Kapsam authority'si `AKTIF_GOREV.md`'dir; `AKTIF_GOREV.yaml` yalnız üretilmiş salt-okunur
  projection, yerel operational store gerçek çalışma durumudur. Biri diğerinin yerine geçmez.
- Klasörü açmak görev, provider çağrısı, migration veya tarama başlatmaz. Zekam'ın yazdığı bir
  dosyayı okumak operasyon yetkisi vermez.
- Markdown, retrieval veya model çıktısından Work/lease/claim/receipt durumu üretme. Claim olmadan
  Zekam-owned effect, terminal receipt olmadan başarı iddia etme.
- Riskli veya yıkıcı değişiklikte ve Work Item kapanışında test ve gereken bağımsız doğrulama
  bulunur; aynı modelin başka başlıkla yazdığı onay bağımsız verifier sayılmaz.
- Canlı provider/model çağrısı, kullanıcı-geneli ayar değişikliği ve yeni araç kurulumu kendi
  açık izinlerini ister.

## İhtiyaç halinde açılacaklar

- Devam, recovery, Zekam Work: `00_BASLA.md`, `DEVAM_PROTOKOLU.md`, `zekam resume --json`.
- Proje-bağlamlı soru ve citation: `zekam ask "<soru>" --json` (bounded, salt okunur).
- Jira görevi: `jira-is-kaydi` skill'i; key için `zekam jira resolve "<soru>" --json`.
- Model benchmark: önce yalnız `zekam model campaign plan --json`; çağrı bütçesi gösterilmeden
  authorization üretme veya kampanya çalıştırma, ayrıca açık onay iste.
- Unit test ölçümü: `zekam test` yüzeyi; Maven/JaCoCo kanıtı gerçek rapordan okunur.
- Yerel schema/extension hazırlığı: `zekam doctor --hazirla --json` (yalnız yetkilendirilmişse).
- İstemci entegrasyonu ve geçiş: `docs/CLI_ENTEGRASYON_POLITIKASI.md`.
