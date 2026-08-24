# Geliştirme Kurulumu

Bu belge çalışan Zekam kodunun yerel kurulumunu anlatır. Ürün kuralları
`NIHAI_UYGULAMA_PROMPTU.md` ve referans verdiği sözleşmelerdedir; bu belge onların
yerine geçmez.

Repository commit kimliği ve ASCII mesaj politikası için clone başına bir kez:

```powershell
git config core.hooksPath .githooks
```

Hook'lar `mehmet-karacan <karacan.mehmet@hotmail.com>` dışındaki author/committer
kimliklerini, AI ortak-yazar attribution satırlarını ve ASCII dışı commit mesajlarını
push öncesinde reddeder.

## 1. Gereksinimler

| Bileşen | Sürüm | Not |
|---|---|---|
| Python | >= 3.12 | `pyproject.toml` içinde tanımlı |
| Docker + Compose | v2+ | PostgreSQL 18 + pgvector için |
| Git | 2.40+ | source binding ve worktree için |

## 2. Sanal ortam ve bağımlılıklar

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[db,api,dev]"   # Windows
# veya
.venv/bin/python -m pip install -e ".[db,api,dev]"       # Linux/macOS
```

## 3. PostgreSQL 18 + pgvector

```bash
cd compose
cp .env.example .env      # ZEKAM_DATABASE_PASSWORD degerini kendiniz doldurun
docker compose up -d
```

`compose/.env` dosyası Git'e eklenmez. Container yalnız `127.0.0.1` üzerinde dinler.
İlk açılışta `compose/initdb/010-extensions.sql` şu eklentileri kurar:
`vector`, `pg_trgm`, `btree_gin`, `pgcrypto`.

Varsayılan port `5433`'tür. Port kullanımdaysa `compose/.env` içinde
`ZEKAM_DATABASE_PORT` değerini değiştirin ve aynı değeri `$ZEKAM_HOME/config.yaml`
dosyasına yazın.

## 4. ZEKAM_HOME

`ZEKAM_HOME` kullanıcı verisinin köküdür ve source ağacının **dışında** olmalıdır.
Tanımlı değilse `~/.zekam` kullanılır.

```bash
export ZEKAM_HOME=~/.zekam
zekam init
```

`zekam init` idempotenttir; var olan veriyi silmez. `--dry-run` ile yalnız plan yazdırılır.

## 5. Yapılandırma önceliği

```text
config/zekam.default.yaml   (core varsayilani, secret icermez)
  -> $ZEKAM_HOME/config.yaml (kullanici override'i, secret icermez)
    -> ZEKAM_* ortam degiskenleri
```

Parola yalnız `ZEKAM_DATABASE_PASSWORD` ortam değişkeninden okunur. Yapılandırma
dosyasında `password`, `token`, `secret`, `api_key` gibi bir anahtar bulunursa yükleme
hata verir.

## 6. Doctor

```bash
zekam doctor
zekam doctor --json
```

Doctor **salt okunurdur**: migration uygulamaz, kuyruktan iş almaz, model çağırmaz,
policy değiştirmez.

| Kategori | Kontroller |
|---|---|
| `core` | sürüm, Python, yapılandırma, `ZEKAM_HOME` yerleşimi, git istemcisi |
| `postgres` | sürücü, bağlantı + pgvector, migration head ve drift |
| `storage` | içerik adresli depo bütünlüğü |
| `runtime` | kuyruk derinliği + recovery, model envanteri, policy, scheduler tanımları, istemciler, komut yüzeyi |

**Kanonik kayıt okunamıyorsa kontrol `skipped` döner — sahte `passed` üretmez.**
Yapılandırılmış istemci yoksa istemci kontrolü de atlanır.

`runtime` sayıları realm kapsamı olmadan okunur, yani **bütün realm'leri** kapsar;
bu kanıt alanında `cross_realm` olarak işaretlenir. Operasyonel soru "bu kurulumda
envanter/policy var mı" olduğu için bu bilinçlidir.

Çıkış kodları: `0` healthy, `1` degraded, `2` blocked, `3` recovery-required.

## 7. Migration'lar

```bash
zekam db status              # head, bekleyen, drift
zekam db plan                # uygulanacaklar ve geri alma dosyası durumu
zekam db upgrade             # dry-run
zekam db upgrade --uygula    # gerçekten uygular
```

Kurallar:

- Migration'lar forward-only'dir ve numaraları boşluksuz artar.
- Uygulanmış bir dosyanın içeriği değişirse bu **drift**'tir; `upgrade` engellenir.
  Çözüm eski dosyayı geri getirmek veya yeni bir migration eklemektir.
- Her migration tek transaction içinde çalışır; ledger aynı transaction'a yazılır.
- Aynı anda tek migration süreci çalışır (PostgreSQL advisory lock).
- `NNNN_ad.down.sql` dosyaları otomatik çalışmaz; yalnız exact authorization ile.

Uygulama superuser olarak çalışmaz: migration `zekam_app` rolünü oluşturur ve
oturumlar `set role zekam_app` ile bu role geçer. Superuser row-level security'yi
atladığı için realm yalıtımı ancak bu rol altında gerçekten uygulanır.

## 8. Yedek manifesti

```bash
zekam backup create --cikti yedek.json
zekam backup verify yedek.json
```

Manifest migration head'i, her migration'ın checksum'ını, nesne deposundaki her
artifact'ın digest ve boyutunu, sanitize edilmiş yapılandırma digest'ini ve kendi
`manifest_digest` değerini taşır. Doğrulama sonucu `valid`, `altered`, `incomplete`
veya `corrupt` olur; geçersizse çıkış kodu 2'dir.

## 9. Proje entegrasyonu

```bash
zekam project add /kaynak/proje --slug gpu --name "GPU Fusion" --alias "gpu projesi"
zekam project add /kaynak/proje --slug gpu --uygula     # gercek kayit
zekam project list --json
zekam project remove gpu                    # dry-run; kaynak ve gecmis silinmez
zekam project remove gpu --uygula           # projeyi arsivler
zekam project list --include-archived --json
zekam project restore gpu --uygula          # arsivden geri getirir; rebind gerekebilir
zekam project resolve "gpu projesi"
zekam project scan gpu --uygula
zekam project show gpu
zekam project resume gpu
zekam project rebind gpu /yeni/konum --uygula
```

Kurallar:

- Harici kaynak kökü exact project binding, açık mutation yetkisi, allowlist ve
  tek-writer kilidiyle doğrudan düzenlenir; kopya, mirror veya worktree üretilmez.
- `add`, `scan` ve `rebind` varsayılan olarak dry-run'dır; `--uygula` gerekir.
- Kanonik kayıtta absolute path bulunmaz. Makineye özel yol yalnız
  `projects.source_binding_local` tablosundadır ve export kapsamı dışındadır.
- Tarama `.gitignore`, `.zekamignore` ve sistem deny list'ini birlikte uygular;
  symlink izlenmez, secret içeren dosya indekse girmez, secret **değeri** hiçbir
  rapora yazılmaz.
- Capability profili deterministiktir: aynı kaynak sürümü + aynı üretici sürümü
  aynı `profile_digest` değerini verir.

Çıkış kodları:

| Durum | Kod |
|---|---|
| Başarılı | 0 |
| Bulunamadı (proje veya realm) | 4 |
| Belirsiz ifade (kullanıcı seçmeli) | 5 |
| Çalışma zamanı hatası | 70 |

### Realm

Her kayıt bir realm içinde tutulur ve realm'ler birbirini göremez (RLS). Varsayılan
realm `yerel`'dir; `--realm` ile değiştirilir. Okuma komutları realm'i kendiliğinden
oluşturmaz; `project add --uygula` oluşturur.

## 10. Work Graph

```bash
zekam work create gpu "Kok neden analizi" --tur defect --numara 123 --uygula
zekam work list --json
zekam work show gpu 123
zekam work history gpu 123
zekam work relate gpu 123 blocks 124 --uygula
zekam work transition gpu 123 ready --uygula
zekam work transition gpu 123 completed --kanit "test=pytest 482 passed" --uygula
zekam work next
zekam work today
zekam work resume --json
```

Kurallar:

- **Work Graph tek yetkili kaynaktır.** `today`, `next` ve `resume` yanıtları
  kanonik kayıttan gelir; semantic index veya vector store kullanılmaz.
- Durum geçişleri kapalı bir kümeyle tanımlıdır. Tanımsız geçiş reddedilir
  (çıkış kodu 6).
- `completed` durumu acceptance evidence olmadan yazılamaz. Bu kural hem alan
  modelinde hem veritabanı constraint'inde uygulanır — uygulama katmanı atlansa
  bile geçmez.
- Her değişiklik yeni bir revision üretir ve `core.revision` hash zincirine
  eklenir. `work history` zincir bütünlüğünü (`chain_valid`) raporlar.
- Aynı kaydı iki yazar aynı anda güncellerse yalnız biri kazanır (optimistic
  concurrency); diğeri `ConcurrencyConflict` alır.
- `depends-on` ve `parent-of` grafikleri döngüsüz kalır (veritabanı trigger'ı).
- Cross-project ve cross-realm ilişki hem alan modelinde hem bileşik foreign key
  ile reddedilir.
- Talep/defect numarası **exact** aranır; benzerlik numarayı değiştiremez.
- Task Plan yetki vermez: `grants_authority` her zaman `false` ve bunu değiştirmeyi
  denemek veritabanı constraint'ine takılır.

## 11. Governance: policy, secret, yetki

```bash
zekam policy init --uygula        # varsayilan policy + temel capability'ler
zekam policy show
zekam policy capabilities
zekam secret add anthropic-api --provider anthropic --amac chat --locator ANTHROPIC_API_KEY --operasyon chat --uygula
zekam secret list --json
zekam secret revoke anthropic-api --uygula
zekam auth list --json
zekam auth show <yetki-kimligi>
zekam auth revoke <yetki-kimligi> --gerekce "artik gerekmiyor" --uygula
zekam auth audit --adet 20
```

### Ayrım

| Kavram | Sorusu |
|---|---|
| Policy | Neye izin verilebilir? |
| Capability | Adapter teknik olarak ne yapabilir? |
| Authorization | Bu exact effect için izin var mı? |

Biri diğerinin yerine geçmez. Bir yeteneğin kayıtlı olması izin değildir; policy'nin
izin vermesi de exact authorization yerine geçmez. Her ikisi de negatif testle
doğrulanır.

### Hard gate sırası

```text
capability -> policy -> risk -> scope -> authorization
```

İlk reddedende durur, her karar denetim kaydına yazılır. Sessiz izin veya sessiz
red yoktur.

### Risk

Risk seviyesi effect türü, veri sınıfı, blast radius, geri alınabilirlik ve
yıkıcılıktan **türetilir**. `EffectRequest` üzerinde risk alanı yoktur — model veya
istemci kendi beyanıyla riski düşüremez. Yalnızca yukarı çıkabilir.

| Effect | Taban risk |
|---|---|
| `none` | none |
| `file-write`, `process-run`, `network-call`, `provider-call`, `git-commit` | medium |
| `database-write`, `git-push` | high |
| destructive | critical |

`high` ve üzeri bağımsız verifier gerektirir.

### Secret

- Kanonik kayıtta secret **değeri** yoktur; yalnızca ad, sağlayıcı, amaç, izinli
  operasyonlar, sürüm ve arka uçtaki *locator* (değerin adı) tutulur.
- Değer yalnızca broker tarafından, çağrı anında, process belleğine çözülür ve
  blok bittiğinde temizlenir.
- `SecretValue` sınıfının varsayılan görünümü maskelidir: `repr`, `str`, f-string,
  `%`-format, log ve exception hepsi `***` verir. JSON serileştirme, pickle ve
  hash **hata verir**. Gerçek değere yalnızca `reveal()` ile ulaşılır.
- Bir sızıntı taraması testi, `core`/`projects`/`work`/`security` şemalarındaki
  bütün metin ve jsonb kolonlarını tarayıp değerin hiçbir yerde olmadığını doğrular.

### Exact one-shot authorization

- Her yetki tek bir `plan_digest` + `effect_digest` çiftine bağlıdır.
- Süresizlik yoktur (`expires_at > issued_at` veritabanı constraint'i).
- Tüketim **tek atomik UPDATE**'tir; iki süreç aynı yetkiyi tüketemez.
- Terminal duruma geçen yetki `issued`'a döndürülemez, digest'i değiştirilemez,
  kapsamı genişletilemez, silinemez — hepsi veritabanı trigger'ı ile.
- Kapsam genişletme denemesi yetkiyi **tüketmez**; reddedilir ve denetime yazılır.

### Outbound (Provider Gate)

`prepare` ağ çağrısı yapmaz ve secret çözmez; yalnızca kaydeder. `secret` ve
`local-only` sınıfı veri hazırlık aşamasında reddedilir. `apply` isteği yetkiyle
sağlayıcı, endpoint ve veri sınıfı düzeyinde **yeniden** eşleştirir; `restricted`
veri gözden geçirilmiş disclosure ister.

## 12. Execution Plane: kuyruk, lease, kilit, claim

### prepare / apply

```text
prepare : salt okunur. Provider cagrisi yok, secret cozumu yok, ag yok,
          mutation yok. Yalniz policy/risk degerlendirmesi ve digest.
apply   : once drift'i yeniden dogrular, sonra exact yetkiyi tuketir.
```

Drift kaynaklari: `plan_digest`, `source_revision`, `policy_digest`, `effect_digest`.
Biri değiştiyse eski hazırlık kullanılamaz; yeni plan revision gerekir. Reddedilen
`apply` yetkiyi **tüketmez**.

### Route planner

Sabit global maksimum yoktur. Paralellik şu değerlerin en küçüğüdür:

```text
min(hazir_bagimsiz_adim, worker_slot, quota_slot,
    token_slot, cost_slot, provider_rate_slot, policy_limit)
```

Sonuç 1 olabilir ve bu geçerli bir karardır. Karar türleri: `direct`, `single`,
`sequential`, `parallel`, `blocked`, `recovery`. `recovery-required` bir adım varsa
diğer her şeyin önüne geçer.

### Durable queue

- Aynı `idempotency_key` ile ikinci enqueue **yeni job üretmez** (veritabanı unique).
- Enqueue ile outbox olayı aynı transaction'da yazılır.
- Claim seçimi yalnız kuyruk sorgusunda `for update skip locked` kullanır.
- Her claim fencing token'ı artırır.
- Worker'ın yetenek kümesi job'ın gereksinimini kapsamalıdır (`required_capabilities <@ worker`).

### Lease ve fencing

- Lease **yetki değildir**; yalnız "şu an kim yürütüyor" sorusunu yanıtlar.
- Owner token'ın kendisi hiçbir zaman saklanmaz, yalnız digest'i.
- `heartbeat`, `complete` ve `fail` owner digest + fencing token + `running` durumu
  üçlüsüyle eşleşmezse 0 satır etkiler; eski sahip sonuç yayımlayamaz.

### Logical lock

Çakışma kuralları hem alan modelinde hem veritabanı trigger'ında:

| Durum | Sonuç |
|---|---|
| İki okuma | çakışmaz |
| Aynı kaynak, en az biri yazma | çakışır |
| `project:<id>` yazma | o projedeki her şeyle çakışır |
| Path parent/child, en az biri yazma | çakışır |
| Farklı projeler | çakışmaz |

Kilitler deadlock'u önlemek için lexical sırada alınır. `..`, ters bölü ve mutlak
yol reddedilir.

### Claim-before-effect

```text
admission -> job claim -> logical lock -> effect claim -> effect -> receipt
```

- Claim, effect'in **gerçekleştiğini kanıtlamaz**; yalnız başlatma niyetini kanıtlar.
- Aynı exact effect için ikinci claim reddedilir (unique idempotency key).
- Bir claim için en fazla bir receipt yazılabilir.
- Terminal receipt'i olmayan claim varsa iş `completed` yapılamaz.
- Lease süresi dolduğunda: bekleyen claim varsa `recovery-required`, yoksa attempt
  limiti içinde `ready`.
- **Sessiz retry yasaktır.** `assert_no_silent_retry` her non-trivial durumda hata verir.

### Agent Result Envelope

- Strict şema: eksik alan, bilinmeyen alan ve serbest metin reddedilir.
- `partial`, `failed`, `blocked`, `recovery-required` ve `abstained` fan-in'de
  **kaybolmaz**; toplam durum en ağır basan durumdur.
- Agentic işte en az bir gerçek subagent gerekir; **koordinatör sayılmaz**.
- Aynı yazılabilir kaynakta iki builder olamaz.
- `high`/`critical` riskte verifier kimliği builder'dan farklı olmalıdır; kendi işini
  doğrulama denemesi `PolicyViolation` verir.

## 13. Model envanteri ve sağlık

```bash
zekam model inventory                       # doğrula, aktarma
zekam model inventory --uygula              # kanonik store'a aktar
zekam model list --json
zekam model health --uygula                 # sentetik probe
zekam model report --uygula --cikti rapor.md
```

### Envanter kuralları

- **20 Model ID bağımsız yönetim nesnesidir.** Aynı backend adını paylaşan iki
  kayıt birleştirilmez; `BAAI/bge-reranker-v2-m3` iki ayrı Model ID olarak durur.
- **19 teknik profil ile 20 kanonik kayıt farkı gizlenmez.** Profili olmayan kayıt
  görünür `verification_note` taşır ve raporda ayrı başlıkta listelenir.
- Envanter kaydı **ham endpoint veya credential değeri taşımaz**; yalnız
  `model-endpoint:<kimlik>` ve `model-credential:<kimlik>` mantıksal referansları.
  URL, IP, `Bearer`, `sk-`, AWS anahtarı ve uzun opak token desenleri hem alan
  modelinde hem veritabanı constraint'inde reddedilir.
- Bilinmeyen alan tahmin edilmez; `None` kalır. Bilinmeyen protokol `unknown` olur.
- `declared_mode` ile `declared_category` farklı modalite gösteriyorsa bu bir
  **çakışmadır**: probe modu esas alır, çakışma `modality-conflict` olarak raporlanır.

### Sağlık probe'ları

Her modalitenin kendi sentetik fixture'ı var; fixture proje içeriği taşımaz.

| Modalite | Probe | Beklenen şekil |
|---|---|---|
| chat / code / completion | minimal mesaj | boş olmayan metin |
| embedding | tek girdi | sabit boyutlu, sonlu vektör |
| rerank | sorgu + pasajlar | pasaj sayısı kadar skor |
| audio_transcription | kısa sentetik ses | boş olmayan transkript |
| guardrail | güvenli + güvensiz örnek | iki örnek için de doğru etiket |
| vision_language | üretilmiş küçük görsel | `image_received` + metin |

**Prompt ve yanıt içeriği hiçbir zaman saklanmaz** — yalnız durum, gecikme, hata
kategorisi ve digest. Yanıt bir secret canary yansıtırsa probe `secret-echo` ile
başarısız olur.

### Yaşam döngüsü

```text
untested -> health-passed -> contract-passed -> benchmark-eligible
                                             -> project-qualified -> active-candidate
başarısızlık -> quarantined -> cooldown -> untested
```

**Sağlık başarısı yetenek kanıtı değildir**; yalnız benchmark uygunluğu sağlar.
İlan edilmiş bir yetenek doğrulanmadıkça `capabilities_verified` listesine girmez.

### Karantina ve staleness

- İki ardışık başarısızlık karantinaya alır (eşik policy ile sürümlüdür).
- Cooldown dolunca model aday havuzuna döner ve olay kaydedilir.
- Envanter digest'i, policy digest'i veya yaş sınırı değişirse sonuç `stale` olur
  ve yeniden test gerekir.

### Günlük rapor

Türkçe Markdown ve JSON **aynı `evidence_digest`** değerine bağlanır; ikisi de
secret, ham endpoint ve prompt içeriği taşımaz. Rapor günde bir kez saklanır.

## 14. Benchmark, routing ve kota

Ayrıntı: [docs/MODEL_BENCHMARK_VE_ROUTING.md](MODEL_BENCHMARK_VE_ROUTING.md)

```bash
zekam model benchmark --json                # registry sözleşmesi, salt okunur
zekam model benchmark --model <id> --json   # digest'e bağlı plan hazırla
zekam model decide --json                   # routing kararı ve gerekçesi
```

Kurallar:

- Benchmark kaydı ham prompt/yanıt tutmaz; sürümlü fixture metadata'sı, metrik ve
  SHA-256 provenance tutar.
- Her case `local-only` veya `remote-allowed` olarak **açıkça** işaretlidir; absolute
  path, traversal, endpoint ve secret benzeri metadata fail-closed reddedilir.
- En az **beş repetition**; aynı plan digest'i ikinci kez hazırlanırsa mevcut plan
  döner, yeni sağlayıcı maliyeti oluşmaz.
- Her provider trial'ı mevcut bir Effect Claim ve terminal receipt'e bağlanmadan
  kaydedilemez.
- Tek `unsafe` trial yüksek ortalamayla gizlenemez; sonuç fail eder.
- Tested model ile bağımsız verifier kimliği aynı olamaz.
- Kota fallback yalnız güvenilir gözlemle çalışır: bilinmeyen kota **tahmin edilmez**.

## 15. Context compiler ve continuity

Ayrıntı: [docs/CONTEXT_COMPILER_VE_CONTINUITY.md](CONTEXT_COMPILER_VE_CONTINUITY.md)

Bu katman "model, istemci veya oturum değişince iş nasıl kaldığı yerden sürer"
sorusunu **transcript olmadan** yanıtlar.

### Context manifest

Adaylar logical kimlik, source revision, içerik/kanıt digest'i, authority sınıfı,
tazelik ve token maliyetiyle değerlendirilir.

- `required` adaylar önce yerleştirilir; toplamı bütçeyi aşarsa işlem **fail-closed**
  biter — sessiz kırpma yok.
- Diğerleri authority-first, freshness-second, kimlik tie-break sırasıyla seçilir
  (deterministik).
- Her dışlama gerekçe taşır: `budget-exhausted`, `stale`, `insufficient-authority`,
  `superseded`.
- Ham transcript ve model çıktısı manifest'e giremez.

### WorkJournal

Append-only hash zinciri. Sequence, önceki digest, payload digest ve truncation
bayrağı entry digest'ine dahildir. Ekleme, silme, sıra değiştirme ve içerik değiştirme
denemelerinin dördü de yakalanır. PostgreSQL optimistic head kontrolü eşzamanlı stale
writer'ı reddeder.

### Checkpoint zorunluluğu

Checkpoint, bağlı olduğu plan adımlarını `completed` ve `pending` arasında **exact
partition** eder; her completed adım result digest ister; plan ve checkpoint aynı
source revision'a bağlanır.

`payload.meaningful_step = true` işaretli bir job, kendisine bağlı checkpoint
bulunmadan `completed` olamaz — bu bir veritabanı trigger'ıdır, uygulama katmanı
atlansa bile geçmez.

### Continuity snapshot ve finalized handoff

Handoff **hiçbir koşulda** taşımaz: transcript, authority, aktif lease, approval,
authorization, secret, absolute path. Veritabanı constraint'i bu beş bayrağı birlikte
zorlar (`handoff_no_authority`).

Client/model değişiminde yalnız bounded first reads, safe actions ve evidence
digest'leri aktarılır. Yeni worker Work, lease ve authorization durumunu kanonik
repository'den **yeniden edinmek zorundadır**; handoff bunu devretmez.

## 16. Doğal dil ve kanıtlı araştırma

Ayrıntı: [docs/DOGAL_DIL_VE_ARASTIRMA.md](DOGAL_DIL_VE_ARASTIRMA.md)

```bash
zekam ask "gpu projesindeki 123 numarali defectin kok nedenini arastir" --json
zekam research dag --json                    # kanonik rol DAG'i, salt okunur
zekam research start <proje> <is> "<soru>" --intent-digest <d> --kaynak-revizyon <r>
```

`ask` salt okunurdur ve belirsizlikte `5` çıkış kodu döndürür — belirsiz istek asla
sessizce işe dönüşmez.

Kurallar:

- Exact identifier semantic benzerlikle **değiştirilemez**; kanonik kayıtta yoksa
  `identifier-unknown` olarak görünür kalır.
- İşaret zamiri yalnız taze ve bounded bir konuyla çözülür; konu uydurulmaz.
- İki proje adayı varsa mutation başlamadan seçim istenir.
- Araştırma sorusu project/work/intent scope'una bağlıdır; source revision veya
  intent digest değişirse **stale** olur.
- HTTPS kaynağı exact host allowlist olmadan hiç etkinleşmez; absolute path ve
  traversal hem Python'da hem check constraint'inde reddedilir.
- **Koordinatör subagent sayılmaz**; DAG'da gerçek builder rolü yoksa çalışmaz.
  Bağımsız ilk roller aynı paralel grupta yürür.
- Direct contradiction yalnız verifier veya insan review ile çözülür; unresolved
  kaldığı sürece rapor `answered` olamaz (veritabanı constraint'i).
- Citation verifier kimliği araştırmacılarla aynı olamaz (trigger).
- Non-success child sonucu fan-in tarafından yutulamaz.
- Plan candidate yalnız `answered` rapordan türer ve daima
  `requires_authorization = true`, `approval_inherited = false`,
  `grants_authority = false` taşır.

## 17. Sandbox teslim ve istemci adaptörleri

Ayrıntı: [docs/SANDBOX_VE_ISTEMCILER.md](SANDBOX_VE_ISTEMCILER.md)

```bash
zekam sandbox policy --yol src/zekam --json
zekam git commit-check --dosya .git/COMMIT_EDITMSG --json
zekam git push-check origin main <head> --kullanici-istedi --yetki-digest <d>   --test-gecti --verifier-gecti --json
```

Kurallar:

- Entegre kaynak registry'de bagli **gercek source root**'tur; her builder exact path allowlist,
  authorization ve tek-writer kilidiyle dogrudan burada calisir. Kopya/mirror/worktree
  uretilmez. Source tree'nin HEAD ve tree parmak izi işlem
  öncesi/sonrası karşılaştırılır.
- `PathAllowlist` boş olamaz. Önek eşlemesiyle kaçılamaz (`docs` izinliyken
  `docs-gizli/` izinli değildir); absolute path, traversal ve symlink kaçışı
  reddedilir.
- Network **default-deny**; izin exact host **ve** exact operasyon listesi ister.
- Typed runner shell kullanmaz: argv listesi, zorunlu timeout, ortam allowlist'i,
  çıktı bayt sınırı. Ham çıktı değil digest saklanır.
- Teslim: drift kontrolü → bağımsız test → verifier. Yalnız
  `applied` teslim receipt'e uygundur ve bu bile mutation izni değildir.
- İstemciler exact çalıştırılabilir dosya ve **açık yetenek beyanı** ile çağrılır;
  beyan edilmeyen yetenek varsayılmaz. Sonuç strict JSON envelope'dur; ayrıştırılamayan
  çıktı sessizce kabul edilmez.
- Komut satırı talimat metni değil `instruction_digest` taşır.
- Commit mesajı ASCII-only ve zorunlu bölümlüdür; push varsayılan olarak reddedilir
  ve force push hiçbir koşulda otomatik izinli değildir.

## 18. Knowledge Plane ingestion

Ayrıntı: [docs/KNOWLEDGE_INGESTION.md](KNOWLEDGE_INGESTION.md)

```bash
zekam knowledge scan <dizin> --json
zekam knowledge inspect <arsiv> --json
zekam knowledge ingest <belge> --slug <ad> --uygula --json
```

Kurallar:

- Orijinal kaynak **değiştirilemez artifact**'tir; update/delete trigger ile reddedilir.
- Aşamalar sıralı: `validated → stored → parsed → normalized → indexed → activated`.
  Atlama ve geri alma reddedilir; her aşama kalıcılaştırılır, crash sonrası devam edilir.
- Aynı `idempotency_key` ikinci kez iş yaratmaz.
- **Tamamlanmamış ingestion aktif sürüm üretemez** (alan + trigger). Bir kaynağın
  aynı anda yalnız bir aktif sürümü olur.
- Parser doğrudan vector üretmez; locator taşıyan içerik birimi üretir. Locator'sız
  birim veritabanına yazılamaz; OCR birimi confidence ister.
- DOCX için **uydurma sayfa numarası üretilmez**; bilinmeyen format sessizce metin
  sayılmaz.
- Tarama sırasında build/hook/kod **çalıştırılmaz**; deny list, symlink, traversal,
  ikili dosya ve zip bomb fail-closed.
- Veritabanı kaynakları metadata-only; satır verisi ayrı authorization ister.

## 19. Hibrit retrieval ve citation

Ayrıntı: [docs/HIBRIT_RETRIEVAL.md](HIBRIT_RETRIEVAL.md)

Kurallar:

- Chunk yapıyı korur; tablo ve kod bütün kalır, büyük birim parent-child üretir.
  Locator'sız chunk yazılamaz.
- İlk embedding profili BGE-M3 **1024 cosine**. `NaN`/`Inf` ve boyut uyuşmazlığı
  indekslenmez; farklı prefix ayrı profildir; profil digest uyuşmazlığı trigger ile
  reddedilir.
- Üç kanal: exact identifier (trigram), lexical (FTS `simple` sözlüğü), dense
  (pgvector HNSW cosine).
- **RRF ham skorları toplamaz** — dense mesafesi ile `ts_rank` aynı ölçekte
  değildir; yalnız sıra kullanılır. Exact eşleşme düşük dense skorla elenemez.
- Reranker hata verir veya sonuç düşürürse fusion sırasına geri dönülür.
- Aynı içerik iki kez bağlama girmez; çocuk seçilirse ebeveyn de alınır.
- Kanıtsız cevap üretilmez: `answered` / `abstained-no-hit` /
  `abstained-low-evidence`. Her cevap kanal ve eleme açıklaması taşır.
- Değerlendirme Recall@k, MRR ve nDCG@k üretir; iyileşme ancak hiçbir metrik
  gerilemeden kabul edilir.

## 20. Bellek: native motor ve Mem0

Ayrıntı: [docs/BELLEK.md](BELLEK.md)

Kurallar:

- **Bellek otorite değildir**; `grants_authority` check constraint'i ile `false`.
- `run` ve `agent` kapsamları geçicidir: kalıcı bellek üretemez, aramada görünmez.
- Cross-project sonuç açık izin ister; farklı realm hiçbir koşulda görünmez.
- **Ham model çıktısı doğrudan aktif olamaz**: kanıt zorunlu; `semantic`,
  `procedural` ve `failure` bağımsız review ister ve review yazarla aynı kimlik
  olamaz. Failure dersi en az iki bağımsız gözlem ister.
- Mevcut bilgi sessizce ezilmez: supersession eski içeriği korur, ilişki kurar.
  İçerik/sınıf/kapsam değiştirilemez (sütun yetkisi + trigger); kayıt silinemez.
- Arama exact/FTS/vektör/varlık/zaman bileşenlerini birleştirir ve **her sonucun
  gerekçesini** taşır.
- Hijyen salt okunurdur; otomatik silme yoktur.
- Mem0 opsiyonel adaptördür ve otorite değildir: drift durumunda native kayıt
  geçerlidir, senkron hatası native kaydı etkilemez.

## 21. Öğrenme, skill ve ölçülü döngü

Ayrıntı: [docs/OGRENME_VE_SKILL.md](OGRENME_VE_SKILL.md)

Kurallar:

- **Aynı kanıt iki kez sayılmaz**: iki run aynı `evidence_digest` üretiyorsa tek
  gözlemdir (alan + unique constraint).
- Doğrulanmış kök neden olmadan ders üretilmez; kök neden üçlüsü ya birlikte
  doldurulur ya hiç. En az iki bağımsız gözlem — tek olay ancak `critical` ise.
- Ders verifier'ı yazarla aynı kimlik olamaz.
- **Skill kendi kendini aktive edemez.** Aktivasyon dört kapıyı birden ister:
  ölçüm, baseline'ı geçme, bağımsız onay ve boş olmayan rollback planı. Dördü de
  hem alanda hem veritabanında zorlanır.
- Değerlendirme en az beş deneme ister; değerlendiren ve doğrulayan ayrıdır.
  Aynı gövdeli skill adayları tekilleştirilir.
- Döngü sınırsız dönmez: `goal-reached`, `iteration-budget`, `cost-budget`,
  `no-progress`, `blocked`. **Doğrulanmamış başarı hedefi kapatmaz.**
- Bağlam etkinliği route kararına girer ama authority üretmez.

## 22. Scheduler, gelen belgeler ve raporlar

Ayrıntı: [docs/SCHEDULER_VE_RAPORLAR.md](SCHEDULER_VE_RAPORLAR.md)

```bash
zekam scheduler init --uygula
zekam scheduler list --json
zekam scheduler required --json
zekam scheduler plan <is> --json
zekam report sections --json
zekam report today --kapsam genel --json
```

Kurallar:

- Zamanlama tanımı kalıcıdır ve sohbet sürecine bağlı değildir; süreç yeniden
  başladığında durum veritabanından okunur.
- **Aynı tetikleme iki kez iş üretmez**: idempotency anahtarı iş + planlanan an
  (UTC) + payload digest'inden türer ve veritabanında unique'tir.
- Kaçırılan çalışma sessizce yutulmaz: `run-once` tek telafi çalıştırır,
  `skip-visible` atlar ama sayıyı raporlar.
- Bir tanımın aynı anda tek aktif çalışması olabilir.
- Gelen belge: hâlâ yazılan dosya ingest edilmez, aynı içerik ikinci kez işlenmez,
  **birden fazla hedefte tahmin edilmez — seçim istenir**.
- Gece işleri bounded bütçe ister; **kota bilinmiyorsa çalışmaz**.
- Günlük raporda on bölüm zorunludur (alan + constraint); boş bölüm "kayıt yok"
  yazar. Rapor authority taşımaz.
- Her scheduler olayı `next_safe_action` bildirmek zorundadır.

## 23. Yüzeyler, telemetri ve projeksiyonlar

Ayrıntı: [docs/YUZEYLER_VE_TELEMETRI.md](YUZEYLER_VE_TELEMETRI.md)

```bash
zekam surface contract --json
zekam surface check --json
```

Kurallar:

- CLI, API ve MCP aynı use-case'i çağırır; yüzey kendi ürün kuralını yazmaz.
- **Mutasyon yapan her komut açık `--uygula` bayrağı ister** — sözleşme bunu alan
  düzeyinde zorlar.
- `surface check` sözleşme ile kayıtlı komutları karşılaştırır; sapma varsa
  çıkış kodu 1.
- Telemetri correlation zorunlu, içerik yasak: `prompt`, `response`, `content`,
  `body` ve secret benzeri alanlar **alan oluşturulurken** reddedilir.
- Dashboard salt okunur, authority üretmez; altı projeksiyon zorunlu ve **her kare
  kanonik kayda drill-down bağlantısı taşır**.
- Türetilmiş graf `derived` bayrağını kapatamaz; kaybolursa yeniden üretilir.
- MCP bir adapter sınırıdır; authority Zekam'de kalır ve mutasyon yapan araç
  authorization ister.

## 24. Worker süreci

Ayrıntı: [docs/WORKER.md](WORKER.md)

```bash
zekam worker settings --json
zekam worker tick --json            # salt okunur plan
zekam worker tick --uygula --json   # tek dongu
zekam worker run --uygula           # uzun omurlu
```

Kurallar:

- Worker sohbet sürecinden bağımsızdır; durum veritabanından okunur.
- Döngü sırası: kapasite → zamanlama tetiklemeleri → kuyruktan iş alma → işleme.
- `tick` ve `run` mutasyon yaptığı için **açık `--uygula` bayrağı ister**;
  bayraksız `tick` salt okunur plan üretir.
- Kapasite dolduğunda iş alınmaz ve gerekçe döner.
- Aynı tetikleme pencere başına bir kez iş üretir; kaçırılan çalışma olay olarak
  kaydedilir ve `next_safe_action` taşır.
- **İşleyicisi olmayan iş `failed` olur** — sessiz başarı yok. Handler hatası
  sanitize edilmiş kategoriye çevrilir.
- Terminal receipt'i olmayan claim varsa `completed` reddedilir.
- İptal edilen iş terminal sonuç yayımlayamaz.
- SIGINT/SIGTERM zarif kapanma: mevcut iş biter, yeni döngü başlamaz.

## 25. Kalite kapıları

```bash
python scripts/kalite.py                  # alti kapinin hepsi
python scripts/kalite.py lint tip         # secili kapilar
python scripts/kalite.py --cevrimdisi     # ag isteyen kapilari atla
```

| Kapı | Komut | Not |
|---|---|---|
| `bicim` | `ruff format --check` | |
| `lint` | `ruff check` | |
| `tip` | `mypy` | strict |
| `test` | `pytest -q` | PostgreSQL hedefi verilmezse kabul testleri atlanır |
| `bagimlilik` | `pip-audit --skip-editable` | **ağ erişimi ister** |
| `olu-kod` | `vulture --min-confidence 80` | protokolün dayattığı parametreler `_` önekli |

`--cevrimdisi` yalnız ağ isteyen kapıları atlar ve **atlananları görünür kılar**:
hem ekrana `[ATLANDI]` yazar hem kanıt dosyasına `skipped_gates` alanı ekler.
Sessizce geçmiş sayılmaz.

Migration drift kapısı ayrı bir komut değildir; `tests/integration/test_migrations_postgres.py`
içinde checksum drift, temiz kurulum ve rollback ile birlikte doğrulanır.

## 26. PostgreSQL kabul testleri

Gerçek sunucu tanımlı değilse `postgres` işaretli testler atlanır; taklit edilmez.

```bash
export ZEKAM_TEST_DATABASE_HOST=127.0.0.1
export ZEKAM_TEST_DATABASE_PORT=5434
export ZEKAM_TEST_DATABASE_NAME=zekam
export ZEKAM_TEST_DATABASE_USER=zekam
export ZEKAM_DATABASE_PASSWORD=...      # compose/.env icindeki deger
.venv/Scripts/python -m pytest -q -m postgres
```

## 27. Faz kanıtı ve devamlılık

```bash
python scripts/faz_kaniti.py \
  --faz ZEKAM-P00 \
  --gorevler ZEKAM-P00-T01 ZEKAM-P00-T02 \
  --bekleyen ZEKAM-P01-T01 \
  --sonraki "ZEKAM-P01-T01 kanonik JSON ve digest kutuphanesini uygula"
```

Üretilen checkpoint ve continuity packet kayıtları `grants_authority: false` taşır;
yetki devretmez. Runtime uygulandığında kanonik kayıt PostgreSQL Work Graph olur.
