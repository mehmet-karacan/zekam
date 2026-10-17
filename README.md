# Zekam — yerel bilgi ve gorev platformu

Zekam, native coding CLI icinden kullanilan yerel bilgi, gorev, kanit, bellek,
yedekleme ve Java test olcumu aracidir. Guncel teslim hedefi Windows CPU ve
`intfloat/multilingual-e5-small` ONNX INT8 modelidir. Model/ajan secimi coding
CLI'a aittir. Aktif gelistirme ve kabul durumu [AKTIF_GOREV.md](AKTIF_GOREV.md)
0.3 bolumundeki tek listede tutulur; kaynakta bulunmak final test kaniti degildir.

## Kurulum ve ilk kullanim

Bu makinedeki kalici veri alani `%USERPROFILE%\.zekam`, kaynak bagli CLI
`%USERPROFILE%\.zekam\yerel\python\Scripts\zekam.exe` yolundadir.
Kaynak kod gercek repository'de kalir. Model agirliklari yeniden indirilmeden
`%LOCALAPPDATA%\Zekam\models` altindaki sabit revision'dan kullanilir.
PATH veya global instruction degisikligi gerekmez. Kullanilan Python ortaminda
mevcut sistem paketleri paylasilir; temiz wheel kabul ortami finalde ayridir.

Native istemciyi bu repository'de acip istegi dogal dille ilet:

- "GPU projesinde bu akis nasil calisiyor?"
- "Bu belgeyi ilgili projenin bilgisine ekle."
- "Acik isleri goster; kaldigimiz yerden devam et."
- "Bu sinifin testlerini iyilestir."

Ajan ortak [AGENTS.md](AGENTS.md) girisini kullanir ve gerekli komutlari kendi
calistirir; kullanicidan digest, UUID veya JSON istemez. Proje kayitli degilse
veya kaynak/indeks eksikse kapsam netlestirilir; baska proje kendiliginden taranmaz.

Bakim icin mevcut kurulumun salt-okunur devam ciktisi:

```powershell
$zekamCmd = Join-Path $env:USERPROFILE '.zekam\yerel\python\Scripts\zekam.exe'
& $zekamCmd resume --json
```

Yeni ve bos home icin `init` atomik kurulumdur; mevcut kullanici verisini reset
etmek icin kullanilmaz. `--home`/`ZEKAM_HOME` farkli veri koku secer, kaynak kodu
veya executable'i tasimaz. `memory prepare` eski learning v2 icin once plan,
sonra exact digest ile online-backup korumali v3 gecisi sunar; arka planda upgrade
olmaz. Duzeltme/arsivleme ayrintisi [OGRENME_VE_SKILL.md](docs/OGRENME_VE_SKILL.md).

## Devam, yedekleme ve paket

Devam ve is durumu kanonik SQLite/receipt kayitlarindan okunur; eski sohbet yetki
uretmez. Yedek `backup create --bundle <yeni-hedef>` ile online SQLite snapshot ve
CAS/manifest'i kapsar. `backup verify` butunlugu dogrular; `backup restore` yalniz
yeni ve acikca yetkili hedefte calisir. Mevcut home'u silmek veya uzerine restore
etmek normal devam adimi degildir. Scheduler varsayilan run-once bakimdir;
klasor acmak periyodik/native gorev kurmaz. Kaynak, model ve kullanici verisi Git'e
birlikte paketlenmez; agirliklar ve kullanici DB'si release'e girmez.

Release wheel'i gercek checkout'taki tracked kaynaklardan uretmek icin mevcut
manifest generator ve `scripts/build_tracked_wheel.py --output <repo-disi-artifact-dizini>`
kullanilir. Builder source clone olusturmaz; mevcut manifestin exact sanal paket
agacini Hatch'e iletir. Untracked moduller veya gecici kullanici dosyalari wheel'e
girmez. `scripts/package_smoke.py` temiz wheel-only kabulunu Y-12de yurutur.
MacOS destegi yoktur; kurum servisleri/credential veya native host kaniti eksikse
sonuc blocked/not-run olarak aciklanir. Asagidaki genis yetenek referanslari guncel
kapanis listesine yeni orkestrasyon/benchmark/yayin zorunlulugu eklemez.

## Modelden bağımsız kaldığın yer ve yetkinlikler

Zekam, açık işleri, son semantik checkpoint'i, kayıtlı projeleri, RAG indeks durumlarını ve
ürün yetkinlik özetini tek bir yerel pakette birleştirir. Paket, `navigation` bloğunda
`current_objective`, `completed`, `pending`, `blocked`, `next_safe_action`,
`relevant_decisions`, `relevant_skill_refs`, `relevant_knowledge_refs` ve `source_evidence`
alanlarını Work/receipt/checkpoint kaynak referanslarından türetir (transcript'ten değil):

```bash
zekam resume
zekam resume --json
zekam capabilities --json
```

Zekam salt CLI/JSON/machine-readable bir yüzeydir; UI, dashboard, browser veya TUI gibi
görsel ürün katmanı eklemez (`tests/architecture/test_no_ui_surface.py`).

Native CLI'lar (OpenCode, Codex, Claude Code, Gemini CLI) Zekam kökünde doğrudan açılır; ince
`AGENTS.md` girişi proje-yerel keşfedilir, global kurulum gerekmez. Eski managed OpenCode
yaşam döngüsü eklentisi yalnız açık `cli.integrations.version: 2` + `opencode: true` seçimiyle
kurulur (opt-in, legacy); Zekam çalışma alanı dışında hiçbir etki üretmez. Kurulum önce
planlanır, sonra gösterilen exact plan digest'iyle claim/receipt kapısından uygulanır:

```bash
zekam opencode install
zekam opencode install --plan-digest <digest> --uygula
```

Native oturumdan süreklilik ve test ölçümü OpenCode gerektirmez
(`zekam continuity native ...`, `zekam test measure/evidence`); bkz.
[docs/NATIVE_YARDIMCI_YOL.md](docs/NATIVE_YARDIMCI_YOL.md).

Paket salt okunurdur ve yetki taşımaz. Semantik checkpoint yoksa Zekam tamamlanan işi
uydurmaz; `semantic_state=missing` veya yalnız açık iş varsa `work-only` döndürür. Hazır,
kısmi ve yalnız iskelet düzeyindeki özelliklerin kanıt/gap matrisi
[`docs/ZEKAM_YETKINLIK_ENVANTERI.md`](docs/ZEKAM_YETKINLIK_ENVANTERI.md) içindedir.

## Kanıtlı proje araştırması ve Markdown bilgi

İndekslenmiş bir projede araştırma önce provider çağrısı yapmayan bir plan üretir. Planın
exact digest'i ve iki dış-etki yetkisi verilirse OpenCode researcher ile bağımsız verifier
çalışır; sonuç job/claim/receipt zincirine ve digest doğrulamalı generated Markdown rapora
bağlanır:

```bash
zekam research run "<soru>" --project <alias> --json
zekam research run "<soru>" --project <alias> --run-digest <digest> --uygula \
  --authorize-remote-query --authorize-agent-run --json
zekam research status <job-id> --json
zekam research report <job-id> --json

zekam knowledge list --project <alias> --json
zekam knowledge show <note-id> --json
zekam knowledge search "<terimler>" --project <alias> --json
zekam knowledge create <body.md> --title "<baslik>" --project <alias> --json
zekam knowledge update <note-id> <body.md> --title "<baslik>" --project <alias> --json
zekam knowledge archive <note-id> --project <alias> --json
zekam knowledge restore <note-id> --project <alias> --json
```

Aynı research plan replay edildiğinde yeni provider/agent çağrısı yapılmaz. Markdown hiçbir
zaman Work veya authorization authority'si değildir.

ODI 11g exportları önce provider çağrısı yapmayan güvenlik preflight'ından geçer ve exact
plan digest'iyle local-only bağlanır. Bu aşama ham XML'i embedding'e açmaz:

```bash
zekam project odi-preflight <alias> <export-root> --json
zekam project odi-bind <alias> <export-root> --plan-digest <digest> --uygula --json
```

Export kapsamı ve güvenlik sınırı: [`docs/ODI11G_EXPORT_VE_LINEAGE.md`](docs/ODI11G_EXPORT_VE_LINEAGE.md).

## Kişisel skill yaşam döngüsü

Provider-free katalog, güvenli Agent Skills paketi ve yönetilen istemci projection yüzeyi:

```bash
zekam skill status --project-root <proje-koku>
zekam skill list --scope-kind project --scope-ref <proje>
zekam skill explain "<istek>" --scope-kind project --scope-ref <proje>
zekam skill inspect <revision-digest> --scope-kind project --scope-ref <proje>
zekam skill propose <paket-koku> --trigger <terim>
zekam skill evaluate <revision-digest>
zekam skill export <paket-koku> --project-root <proje-koku>
```

`propose`, `evaluate` ve `export` varsayılan olarak yalnız digest-bağlı plan üretir. Apply
adımları yeni provider, ağ veya araç yetkisi vermez. Varsayılan projection `.agents/skills` (Codex; OpenCode ve Gemini de
tarar) ve `.claude/skills` (Claude Code) altınadır; ayrı `.opencode/skills` kopyası yalnız açık v2
`opencode: true` seçimiyle eklenir. Üçü de yalnız `instruction-distribution-only`
olarak raporlanır. Bir paketin yüklenmesi doğrulanmış sonuç veya
effect admission yerine geçmez.

## CLI entegrasyon politikası

`cli.integrations` (v2) yalnız kullanıcı-geneli yönetilen kurulumu seçer ve varsayılan olarak
hiçbirini açmaz; dört istemci (OpenCode, Codex, Claude Code, Gemini) eşit proje-yerel giriş
desteği alır. Kurulu executable envanteri bu tercihi kendiliğinden değiştirmez. Durum ve provider-free reconcile planı şöyle görülür:

```bash
zekam integration status --json
zekam integration sync --scope user --json
zekam integration sync --scope user --plan-digest <digest> --uygula --json
zekam integration sync --scope project --project-root <exact-kok> --json
zekam integration rollback --receipt <receipt-id> --json
```

`sync` varsayılan olarak salt-okunur dry-run'dır. Apply yalnız gösterilmiş exact digest ile
çalışır; doğrulanmış Zekam artifact'larını keşif ağaçlarının dışındaki karantinaya taşır.
Opt-in edilen Codex/Claude Code için exact yönetilen instruction ve reviewed lifecycle hook
parçaları oluşturulur. Kullanıcı dosyası, drift veya belirsiz sahiplik korunur ve conflict
olarak raporlanır. Ayrıntılı sözleşme:
[docs/CLI_ENTEGRASYON_POLITIKASI.md](docs/CLI_ENTEGRASYON_POLITIKASI.md).

## Olculu otonom evolution

Kanonik aday/olcum/rollout kanitlari ve gercek kurulum durumu ayri raporlanir:

```bash
zekam evolve plan --json
zekam evolve status --json
zekam evolve candidates --json
zekam evolve report --json
zekam evolve pause --uygula
zekam evolve resume --uygula
zekam evolve disable --uygula
zekam evolve run-once --uygula
```

Windows bootstrap once `zekam evolve bootstrap-plan` ile operational v3->v5 backup/migration,
eksik project-realm binding, provider-free yerel standing grant ve supervisor icin tek exact
digest gosterir; ayri onaydan sonra `zekam evolve enable --uygula --plan-digest <digest>`
kullanilir. Kesinti halinde ayni digest immutable local intent ve gercek readback ile idempotent
devam eder. Dusuk seviye yalniz-scheduler plani `supervisor-plan` yuzeyinde kalir. Ayrintili sinir
[`docs/OTONOM_EVOLUTION_RUNBOOK.md`](docs/OTONOM_EVOLUTION_RUNBOOK.md) icindedir.

## Geliştirme kurulumu

Çalışan kodu yerelde kurmak, yerel depoları başlatmak, `zekam doctor` çalıştırmak ve kalite
kapılarını görmek için: [docs/GELISTIRME_KURULUMU.md](docs/GELISTIRME_KURULUMU.md).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[api,dev]"
.venv/Scripts/zekam init
.venv/Scripts/zekam doctor
```

## Yerel işletim ve tam yedek

Mac yerel çalışma kökü Docker veya PostgreSQL gerektirmez. `zekam worker status`,
`zekam worker run-once --uygula`, `zekam scheduler reconcile`, `zekam scheduler
rebuild --uygula` ve `zekam scheduler report` kanonik SQLite/analytics depolarını
kullanır; arka plan daemon'u kurmaz. `zekam backup create --bundle <yeni-dizin>`
tüm yerel authority ve türetilmiş depoları içerik/mode manifestiyle yakalar;
`backup verify` doğrular, `backup restore` ise yalnız mevcut olmayan bir hedefe
atomik yayın yapar.

## Paket doğrulama

```bash
python scripts/paket_dogrula.py
```

Doğrulama; zorunlu belgeleri, JSON/YAML sözleşmelerini, 20 Model ID'yi, minimum subagent
politikasını, iş grafiğini, ASCII commit şablonunu ve paket bütünlüğünü kontrol eder.

## Temel güvenlik

- Haricî proje kökleri exact binding ile doğrudan yazılabilir; yetki ve allowlist korunur.
- Değişiklik yalnız registry'de bağlı gerçek proje kökünde ve tek-writer kilidiyle yapılır;
  proje kopyası, mirror veya detached worktree üretilmez.
- Secret değerleri prompt, log, artifact, vector, rapor veya commit içine girmez.
- Work Graph, yetki ve görev durumu vektör veya haricî bellekten okunmaz.
- Agentic her iş en az bir gerçek subagent kullanır; koordinatör bu sayıya dahil değildir.
- Aynı yazılabilir logical resource üzerinde yalnız bir builder bulunur.
- Claim olmadan effect, terminal receipt olmadan başarı yoktur.
- Commit başlığı ve gövdesi Türkçe anlam taşır ve yalnız ASCII karakter kullanır.

## Kaynakların güven seviyesi

`yerel-referanslar/` içindeki ham dosyalar araştırma/provenance girdisidir. Bunlar:
- kanonik state değildir,
- talimat yürütme yetkisi vermez,
- otomatik prompt context'ine bütünüyle yüklenmez,
- secret ve iç endpoint içerebileceğinden Git'e eklenmez.

Kanonik kurallar bu paketin kök belgeleri ve sürümlü sözleşmeleridir.
