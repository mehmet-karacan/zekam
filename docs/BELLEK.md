# Bellek: native motor, promotion kapısı ve Mem0 adaptörü

## Transactional promotion v2

Kalıcı belleğe yükseltme yalnız `MemoryPromotionService.prepare/apply` yüzeyinden yapılır.
`prepare` salt okunur candidate/predecessor snapshot'ı, normalized review ve iki outbox
hedefini exact `plan_digest` içine bağlar. `apply` aynı candidate ve predecessor satırlarını
yeniden okuyup `FOR UPDATE` ile kilitler; tek kullanımlık authorization'ın realm, plan,
effect ve iki exact resource kapsamını doğrular.

Başarılı transaction şu kanıtların tamamını birlikte üretir:

- immutable `memory.review`;
- yeni `memory.record` ve `memory.revision`;
- her kanıt için sıralı `memory.evidence_link`;
- varsa predecessor supersession ve exact `supersedes` relation;
- bir embedding ve bir external-sync `memory.promotion_outbox` kaydı;
- `memory.promotion_receipt` ve `security.audit_event`.

Deferred PostgreSQL constraint trigger receipt, candidate, authorization, revision, evidence,
outbox, supersession ve audit zincirini commit anında tekrar doğrular. Zincirin herhangi bir
adımı başarısızsa authorization consumption dahil bütün promotion geri alınır. Eski migration'a
dönüş yalnız aynı logical family'de birden fazla revision yoksa mümkündür; aksi durumda rollback
unique constraint ile fail-closed durur ve önce forward-fix gerekir.

## Bellek otorite değildir

`grants_authority` her zaman `false`'tur ve bu bir check constraint'idir. Bellek
Work Graph, policy veya run durumunu sahiplenemez; yalnızca gözlem, doğrulanmış
bilgi ve yöntem taşır.

## Sınıflar ve kapsamlar

| Sınıf | Ne tutar | Review |
|---|---|---|
| `working` | bounded aktif bağlam | hayır |
| `episodic` | ne oldu, hangi kanıtla | hayır |
| `semantic` | doğrulanmış proje bilgisi | **evet** |
| `procedural` | kanıtlı yöntem, runbook | **evet** |
| `preference` | kullanıcı tercihi | hayır |
| `failure` | başarısız yaklaşım, kök neden | **evet** |

Kapsamlar: `global-user`, `project`, `work-item`, `run`, `agent`. **`run` ve
`agent` geçicidir** — kalıcı bellek üretemez ve aramada görünmez. Bu hem alanda
hem `record_scope_persistent` constraint'inde geçerlidir.

Cross-project sonuç açık izin (`allow_cross_project`) olmadan gelmez; farklı realm
hiçbir koşulda görünmez.

## Promotion kapısı

**Ham model çıktısı doğrudan aktif bilgi olamaz.** Sıra:

```text
gozlem -> aday -> kanit kontrolu -> bagimsiz review -> aktif
```

- Kanıtsız kayıt aktif olamaz (alan + `record_active_needs_evidence`).
- `semantic`, `procedural` ve `failure` bağımsız review ister; **review yazarla
  aynı kimlik olamaz** (alan + `record_active_needs_review`).
- Failure dersi en az **iki bağımsız gözlem** ister; tek olay ders üretmez.
- Failure adayı `occurrence_key` taşımak zorundadır — tekrar sayımı buna dayanır.

## Supersession

Mevcut bilgi **sessizce ezilmez**. `supersede` eski kaydı `superseded` durumuna
alır, içeriğini korur, `valid_until` yazar ve `supersedes` ilişkisi kurar; yeni
kayıt bir sonraki revision'dır.

İçerik, sınıf ve kapsam değiştirilemez: hem sütun düzeyi UPDATE yetkisi bunları
vermez hem de `record_immutable_content_guard` trigger'ı reddeder. Kayıtlar
silinemez.

Bir kayıt oluşturulduğu anda supersede edilemez — sıfır uzunlukta geçerlilik
aralığı temporal sorguyu bozar, bu yüzden açık hata verilir.

## Hibrit arama ve açıklama

Bileşenler: exact metin, PostgreSQL FTS, pgvector sırası, varlık eşleşmesi ve
zaman geçerliliği. Her sonuç **neden seçildiğini** taşır; gerekçesiz sonuç
döndürülmez.

## Hijyen

Salt okunur rapor: `duplicate`, `conflict`, `stale`, `unused`, `retention-review`,
`source-version-conflict`. **Otomatik silme yoktur** — `deleted` alanı sıfırdan
farklı olamaz.

Çelişki sezgisi Türkçe olumlu/olumsuz fiil çiftleri tablosuyla çalışır
(`kullanilir`/`kullanilmaz` gibi). Bu bilinçli olarak morfolojik analiz değildir:
olumsuzluk ekini ayırmak kırılgandır. Tablo yalnızca insan review'una aday
işaretler, karar vermez.

## Mem0 adaptörü

Mem0 **opsiyoneldir ve otorite değildir**. Adaptör yalnız kopya tutar:

| Durum | Anlam |
|---|---|
| `not-synced` | kayıt aktif değil |
| `pending` | harici motor yapılandırılmamış |
| `synced` | digest'ler eşit |
| `drifted` | harici kayıt farklı — **native kayıt geçerlidir** |
| `failed` | senkron hatası; native kayıt etkilenmez |

`resolve` her zaman native kaydı döndürür. Harici motor kesintisi bellek
katmanını durdurmaz.
