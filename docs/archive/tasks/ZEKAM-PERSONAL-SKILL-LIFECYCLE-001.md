---
schema: zekam-active-task/v2
task_id: ZEKAM-PERSONAL-SKILL-LIFECYCLE-001
status: APPROVED_ACTIVE_TASK
title: Zekam Deneyimden Kişisel Skill Üretimi ve İstemciler Arası Güvenli Kullanım
created_at: 2026-09-13T16:08:07+03:00
baseline_repository: mehmet-karacan/zekam
baseline_branch: main
baseline_head: d273e543600176a1b7cfc39b6696994cf8fec5cf
legacy_postgresql_data_import: FORBIDDEN
postgresql_runtime_dependency: FORBIDDEN
docker_required_for_zekam_core: false
push_authorized: false
---

# AKTIF_GOREV.md

> **Amaç:** Mehmet’in kabul ettiği gerçek çalışma yöntemlerinin, verdiği düzeltmelerin ve tekrarlanan ihtiyaçların Zekam tarafından kaynaklı skill adaylarına dönüştürülmesi; bağımsız değerlendirmeden geçen sürümlerin mevcut yetki sınırlarında OpenCode, Codex ve Claude Code üzerinden yeniden kullanılabilmesi.
>
> **Yeni platform veya ikinci öğrenme motoru kurulmayacak.** Mevcut learning, improvement, evolution, canonical knowledge, operational authority ve tool/harness bileşenleri tamamlanacak.
>
> Bu belge kullanıcının 13 Eylül 2026 tarihli araştırma ve uygulama planı talebini somutlaştırır. `APPROVED_ACTIVE_TASK` görev parser’ının statüsüdür; ürüne kurulum, native supervisor, provider çağrısı, dış veri aktarımı, bütün gelecek skill’leri etkinleştirme veya push yetkisi anlamına gelmez. Bu dosyanın hazırlanması ile ürünün uygulanması aynı şey değildir.

## 1. İlk uygulayıcı için başlangıç

### 1.1. Mevcut görevden güvenli geçiş

Önce gerçek çalışma dizinindeki `AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md`, `PROJE_MANIFESTI.yaml`, yaşayan `AKTIF_GOREV.md`, projection, DoD ve ilgili güvenlik sözleşmelerini oku. Bu teslim dosyası henüz dışarıdaysa mevcut aktif belgenin üstüne doğrudan yazma.

İncelenen önceki görev kimliği `ZEKAM-AUTONOMOUS-EVOLUTION-001` ve Git blob’u `d976cc570cb993787842a2b1a857206db7173c4f` idi. Git blob kimliği SHA-256 değildir. Kullanıcının yerel dosyası farklıysa gerçek bytes/digest’i esas al ve farkı kaydet. Önceki görevin bütün işlerini tamamlanmış varsayma.

**Özel bağımlılık:** `src/zekam/application/evolution_runtime.py` içinde görev kimliği, transition anahtarı ve tarihsel digest bağları sabit. Yeni MD’nin devreye alınması bu bağımlılık çözülmeden evolution çalışıyor sayılmamalı. [R03–R05]

İki aşamalı benimseme uygula:

1. Yeni belgeyi izinli artifact alanında incele. Eski MD/YAML ve operational açık iş/claim/lease/recovery durumunu exact kaydet. Çalışan iş varsa mevcut kontrollü pause/checkpoint yöntemini kullan; kullanıcı verisini değiştirmeyen analiz devam edebilir.
2. Kullanıcının yeni görev talebine dayanan **yalnız görev benimseme kapsamındaki** geçiş planını hazırla: önceki/yeni task ve digest, eski work referansları, arşiv yeri, yeni projection, etkilenen validator ve yeniden doğrulanması gereken grant’ler. Mevcut protokolün izin verdiği transition yolunu kullan. Eski onayı genişleten veya doğrulayıcıyı atlayan bir bypass yazma.
3. Mevcut runtime yeni kimliği kabul edemiyorsa bunu açık adoption blocker yap. Gereken sınırlı geçiş desteğini, bu yeni kullanıcı talebiyle yetkilendirilmiş değişiklik olarak ve normal kaynak-kök/tek-writer/test/verifier kuralları altında hazırla. Çalışan eski job’ların kimliğini geriye dönük dönüştürme.
4. Tarihsel MD/YAML’yi `docs/archive/tasks/` altında referans olarak koru. Açık maddeler için carry-forward tablosu çıkar; yeni task altında aynı işi tekrar enqueue etme. Yeni görev ilgisiz bütün eski işleri üstlenmez; açık olanlar kendi kayıtlarında kalır.
5. Yalnız güvenli transition tamamlandığında tek yaşayan `AKTIF_GOREV.md` bu belge olsun. YAML’yi mevcut generator ile exact bytes’tan üret; checksum/package manifestlerini mevcut araçlarla yenile.
6. Yeni görev mevcut grant’leri otomatik geçerli veya kapsamı genişlemiş saymasın. Source/implementation/task digest değişiminin doğurduğu drift’i gerçek readback ile raporla. Eski immutable receipt’leri yeniden yazma.

Bu başlangıç, “eski görevi tamamen yeniden uygula” veya yeni task kimliğini her yere metin değişikliğiyle yay talimatı değildir.

### 1.2. Baseline ve yerel değişiklik güvenliği

Branch, HEAD, origin, son beş commit, tracked/untracked durum, kayıtlı gerçek kaynak kökü ve tek-writer/recovery bilgilerini kaydet. GitHub baseline’ı yukarıdaki SHA’dır. Daha yeni HEAD varsa eskiye dönme; bu görevle ilgili değişiklikleri karşılaştır ve başlangıç fingerprint’ini güncelle.

Remote hizalama yalnız mevcut yetki içinde temiz, çakışmasız fast-forward olarak yapılabilir. Dirty/diverged durumda reset, zorla checkout, otomatik stash, rebase veya force-push yapma. Engel yalnız etkilenen mutation yolunu durdursun.

`python scripts/paket_dogrula.py` ile mevcut tanımlı hedefli testleri çalıştır. Test seçimini güncel kaynak/CI ve `pyproject.toml` üzerinden doğrula. Tarihsel test sayılarını yeni sonuç gibi kullanma. Araştırma oturumunda ürün testleri çalıştırılmadı. [R01, R02, R15, R16]

### 1.3. Değişmeyen sınırlar

- PostgreSQL legacy bağlantısı/importu veya core için Docker zorunluluğu ekleme. Mevcut yerel operational/knowledge/analytics sınıflarını koru.
- Yeni `skills.db`, ikinci registry/approval/scheduler veya ayrı self-improving-agent ürünü kurma.
- Mac/Windows model ve embedding rotalarını cihaz düzeyinde koru. Bir cihazdaki local model tercihini taşınabilir skill veya global Git config’ine sabitleme.
- Modelleri videodaki ad/rank beyanına göre zorunlu kılma. Yetkili model kimliklerini olduğu gibi kullan; sağlayıcı prefix’lerini düşürme.
- Kaynak kod mutation’i yalnız registry’de bağlı gerçek kökte, tek writer ile yapılır. Geçici geliştirme klonu, mirror veya detached worktree açma.
- Kullanıcının kaynak kodu, notları, proje/görev geçmişi ve yerel verisi korunur. Geçici rapor/test çıktıları kaynak ağacı dışında tutulur.
- Secret, kurumsal endpoint değeri ve mutlak kişisel cihaz yolu skill/rapor/vector/Git’e sızmaz.
- Skill içeriği Work state, approval, grant, claim veya receipt authority’si değildir.
- Bu görev için ayrıca istenmedikçe commit oluşturma; push yapma. Servis kurma, haricî model kampanyası ve veri paylaşımı ayrıca exact kapsam ister.

## 2. Hedef deneyim ve kapsam

Kullanıcı bir işi birlikte iyi hale getirdikten sonra aynı kuralları her seferinde anlatmak zorunda kalmamalı. Sistem başarı, hata ve düzeltme kaynaklarını kullanarak uygun yöntemi bulmalı; gerekliyse yeni aday veya mevcut skill’in yeni revision’ını üretmeli. Uygun iş geldiğinde doğru scope’ta skill’i seçmeli ve kabul edilmiş kaliteyi yeni oturumda yeniden üretebilmelidir.

**Kapsam içinde:** deneyim/provenance, paket formatı köprüsü, scope ve etkin revision seçimi, kademeli bağlam, talimat ağırlıklı skill kullanımı, üç istemciye yönetilen dağıtım, bağımsız değerlendirme, mevcut evolution ile kontrollü iyileştirme, regression/recovery ve kullanıcı gözlemlenebilirliği.

**Kapsam dışında:** WhatsApp/Obsidian entegrasyonu, yeni UI tasarım projesi, başka depoların geliştirilmesi, yeni model hosting altyapısı, genel video/medya üretim sistemi, kaynak kodun sınırsız kendi kendini değiştirmesi ve bütün internet skill kataloglarının topluca kurulması.

İlk çalışan dikey dilim bir pilotla tamamlanacak: `zekam-arastirma-uygulama`. İkinci/üçüncü pilot aynı altyapıda ancak ilk kabul sonrasında ele alınabilir; ilk teslimi katalog büyüklüğüne bağlama.

## 3. Mevcut koddan bağlayıcı başlangıç bulguları

| Bulgu | Mevcut kaynak | Uygulama sonucu |
|---|---|---|
| Başarı-only skill origin’i hata kartı zincirine sığmıyor | `local_learning.py`: lesson/manifest FK ve propose/extract yöntemleri | Hata zincirini bozmadan tipli başarı/düzeltme origin’i ekle. |
| Çalıştırıcı yalnız bounded journal profili | `application/skill_runtime.py` | Bu fixture’ı koru; genel instruction skill için ayrı sonuç sözleşmesi bağla. |
| Exact trigger, en çok 256 aktivasyon taraması | `local_learning.py::select_active_skills` | Doğal dil keşfi ve filtreli bounded katalog ekle; limit büyütmekle geçiştirme. |
| Seçicide açık scope/retirement filtresi görünmüyor | Aynı yöntem | Metadata görünürlüğünden önce scope ve etkin revision çöz. |
| Değerlendirme salt success_rate > baseline | `domain/learning.py::SkillEvaluation` | Sürümlü çok boyutlu kabul; başarısız/eşit ölçümü mevcut deney deposunda sakla. |
| Rollout local fixture olarak sınıflanmış | `domain/evolution_rollout.py` | Model/agent deneyini bu fixture kanıtıyla karıştırma; gerekli sözleşmeyi sürümle. |
| Görev transition kimlikleri sabit | `application/evolution_runtime.py` | WP-00 adoption kapısını çözmeden yeni task live sayılmaz. |
| Bazı tarihsel modüller wheel dışında | `pyproject.toml` | Üretim composition ve temiz wheel testi zorunlu. |

Bu bulgular [R04–R11, R15] kaynaklarına dayanır. İncelenmeyen yardımcıları tarayıp eşdeğer çalışan bir parça bulursan yeniden yazma; exact test/çağrı kanıtıyla yeniden kullanım kararını kaydet.

## 4. Mimari kararlar

### 4.1. Üç farklı nesne

**SkillPackage:** Taşınabilir yöntem metni ve kaynakları. Okunabilirlik/dağıtım nesnesidir; onay veya yetki taşımaz.

**SkillRevision / Activation:** Mevcut learning/improvement içinde paket digest’i, scope, origin, değerlendirme, review ve etkin sürüm bağları. Otorite mevcut kanonik kayıtlardadır; istemci kopyaları türetilmiştir.

**SkillInvocation / Outcome:** Belirli iş/oturum/istemci/harness içinde hangi sürümün kullanıldığı ve gerçek sonucun nasıl doğrulandığı. Paket yükleme olayı araç etkisi veya kaliteli sonuç yerine geçmez.

Bu isimler kavramsaldır. Mevcut modeller aynı işi yapıyorsa yeni sınıf/table üretme. Yeni nesne gerektiğinde domain/application/infrastructure ayrımını koru.

### 4.2. Öğrenme kaynağı

Origin türleri en az `failure_lesson`, `verified_success`, `user_correction` ayrımını taşısın. Kaynak sınıfı, immutable evidence referansı, scope, run/work, author/reviewer ve kabul edilen artifact revision’ı bağlansın.

Mevcut v1 failure zinciri aynı kimlik/digest’lerle korunacak. Yeni başarı kaynaklı adaya sahte failure_card veya root-cause yazılmayacak. Kullanıcı yorumu yalnız kaynağı gerçekten kullanıcı ise o tipte saklanacak. Yazarın “onaylandı” metni onay kanıtı değil.

Varsayılan otomatik adaylık önerisi: aynı yöntemin en az iki bağımsız deneyimde görülmesi veya açık “bunu skill yap” talebi. Bu adaylık eşiğidir; aktivasyon eşiği değildir. Olayın yeniden işlenmesi ikinci gözlem sayılmaz. Benzerlik/dedupe mevcut skill’in yeni revision’ını önermeyi öncelemeli.

### 4.3. Skill mi, not mu, tercih mi, kod mu?

Sınıflandırma kapısı koy:

- Tekil olgu/karar → knowledge/semantic memory.
- Kullanıcının kalıcı biçim tercihi → scope’lu preference.
- Doğrulanmış yazılım kusuru → kod düzeltmesi ve regresyon testi; skill ile üstünü örtme.
- Yeniden uygulanabilir çok adımlı yöntem → skill adayı.

İlk sürüm bu kararı anlaşılır gerekçeyle kaydedebilir; yeni model çağrısı zorunlu değildir. Model kararı kullanılıyorsa model yetkisi ve veri kapsamı mevcut gateway’den gelmelidir.

### 4.4. Paket biçimi ve güvenli içe alma

Ortak çekirdek standart `SKILL.md` ve göreli referanslardır. İsim/açıklama ve diğer alanları güncel standartla doğrula. `references/`, `assets/`, `scripts/` zorunlu klasör değildir. İlk pilot script içermeyecek. İstemciye özel davranış ortak yönteme gömülmeyecek. [S01]

Paket dosya manifesti; normalize edilmiş göreli yol, boyut, content digest, dosya türü, provenance/lisans ve ortak paket digest’i taşısın. Bilinmeyen/tehlikeli yol, duplicate canonical path, büyük arşiv, link kaçışı ve secret şüphesinde fail-closed davran.

İçe alma yalnız inceleme/staging/candidate oluşturur. Script/hook/dinamik shell çalıştırmaz; paket yöneticisi otomatik kurmaz; dış URL’leri izleyip kaynak indirmez. Dış bağımlılık gerekli ise ayrı plan/güvenlik incelemesi üretir.

İzin genişleten istemci alanları ortak paketten sessizce geçirilmeyecek. Export sırasında kayıp veya değişmiş semantik varsa capability raporunda görünür olacak. Byte-stable canonical manifest ile istemci artifact digest’i ayrı tutulacak.

### 4.5. Tek etkin sürüm ve scope

Her scope+skill kimliği için en fazla bir etkin revision seçilsin. Activation geçmişi append-only kalsın; etkin görünüm existing selector veya türetilmiş read model üzerinden çözülsün. Yeni ikinci authority registry’si kurulmasın.

Scope çözümü realm → project → izinli kullanıcı-genel bağlamı dikkate almalı; proje kuralı yetkisiz şekilde globalleşmemeli. Yetkisiz paketin adı ve açıklaması dahi ilk metadata listesine girmemeli. Legacy scope belirsizliği “global” diye doldurulmasın; gerekirse legacy-unbound olarak görünür blocker olsun.

Her invocation exact paket/revision digest’ine sabitlensin. Rollback/revoke sonrası daha önce yüklenmiş içerikten gelen yeni araç etkisi de admission kontrolüne tabi olsun.

### 4.6. Kademeli yükleme ve seçim

Exact programatik trigger yolunu koru. Genel discovery hizmeti metadata ile başlasın; body yalnız seçilince, referanslar ihtiyaç halinde yüklensin. Ölçülmüş token sayısı yoksa byte/karakter proxy’si açıkça etiketlensin; token diye sunulmasın.

İlk sürümde deterministik/lexical filtre ve açıklanabilir sıralama yeterli olabilir. Semantik router mevcut yetkili model/embedding üzerinden opsiyonel olarak devreye alınabilir; model yoksa seçim mekanizması sahte embedding üretmez. Seçmeme, açıklama istemeden güvenli varsayımla devam etme veya açık seçim isteme durumları risk bazında tanımlansın.

Yanlış pozitifleri azaltmak için kullanım dışı örnekler, gerekli araçlar, scope ve görev türü değerlendirmeye girsin. Belirli skor veya top-k eşiğini evrensel sabit kabul etme; konfigürasyonu evaluation ile gerekçelendir. Mevcut 256 sınırı indeksli filtre/pagination tasarımıyla çözülmeden büyüyen katalog hazır sayılmaz.

### 4.7. Yürütme ve kanıt

Önerilen gözlem durumları: `discovered`, `selected`, `loaded`, `invoked`, `completed`, `verified-success`, `verified-failure`; ayrıca `blocked`, `cancelled`, `unverified`. Bunları mevcut operasyonel enum’lara zorla eklemek yerine anlamları mevcut job/result olaylarıyla eşleştir. Domain geçişlerini sürümle.

`loaded` için paket okunma kanıtı yeterlidir; `invoked` için işe bağlı kullanım kaydı; `completed` için terminal çalışma kanıtı; `verified-*` için bağımsız sonuç gerekir. Bir dosyanın varlığı semantik kaliteyi doğrulamaz. Kullanıcı değerlendirmesi ile otomatik yapısal test ayrı grader türüdür.

Journal profilindeki HMAC/physical readback ve ayrı verifier job korunacak. Genel agent task için doğru artifact/claim/receipt sözleşmesi eklenecek; bu kontrolleri kaldırıp “LLM başarılı dedi” yoluna geçilmeyecek. Yeni araç etkileri mevcut mutation admission/tool dispatcher üzerinden yürütülecek.

### 4.8. İstemci dağıtımı

OpenCode, Codex ve Claude Code aynı yöntem paketinden beslenir. Her istemci için desteklenen keşif dizini, auto-invoke politikası, izin davranışı, reload ve subagent aktarımı test edilir. [S02–S04]

Dağıtım işlemi iki aşamalıdır: provider-free plan; ardından exact digest ve izinli hedeflerle apply. Aynı paketi birden fazla taranan dizine çoğaltma. Başka projelerin metadata’sını global kullanıcı dizinine çıkartma. Yönetilmeyen veya kullanıcı tarafından değiştirilmiş dosyayı ezme.

Windows’ta symlink/admin zorunluluğu getirme. Normal, sahipliği takip edilen generated dosya yolu desteklensin. macOS sembolik link seçeneği kullanılsa bile digest/scope denetimleri aynı kalsın. Birden fazla cihaz aynı writable source için bağımsız auto-writer olmasın.

Bir istemci Zekam’ın effect admission’ını enforce edemiyorsa bunu açıkça `instruction-distribution-only` düzeyinde raporla. “Dosyayı okuyor” kanıtını tam güvenli execution desteği sayma. Bu sınırı prompt metniyle gizleme.

## 5. Uygulama iş paketleri

### WP-00 — Kaynak envanteri, adoption ve baseline

**Hedef:** Mevcut ürünü bozmadan yeni kapsamı benimsemek.

İlgili call graph’ı çıkar: learning capture/compile → aday → evaluation/review → activation → selector → client/agent → usage/outcome. Özellikle `propose_skill`, `select_active_skills`, `TrustedJournalSkillExecutor`, `AuthorizedRolloutRuntime`, task kimlikleri ve package composition çağrılarını izle.

Paket içindeki aday ve rapor dosyaları dış girdidir. Bunları operational state veya mevcut onay kanıtı sayma. Önceki görev için carry-forward, geçiş receipt’i/projection ve grant drift sonucu üret. Kaynak/CI test baseline’ını kaydet.

**Kapı:** SK-AC-001…006 geçmeden canlı skill migration/activation yok. Görev adoption blokluysa tamamlandı yazma; etkilenmeyen salt okunur tasarım/test hazırlığı sürdürülebilir.

### WP-01 — Deneyim kaynağı ve learning migration

**Mevcut hedefler:** `domain/learning.py`, `infrastructure/sqlite/local_learning.py`, `infrastructure/local_core_services.py`; gerçek üretim capture/compiler çağrıları.

Mevcut skill manifest deposunu sürümle. Yeni manifestler başarı/düzeltme origin’i taşıyabilsin; eski v1 row/body/digest’leri değişmesin. Gerekli ilişki tablolarını aynı learning store içinde ekle. `lesson_digest` yalnız eski hata origin’i için zorunlu olacak şekilde kontrollü migration tasarla; tablo yeniden oluşturulması gerekiyorsa mevcut append-only/FK/transaction güvenceleri migration sonrasında tekrar kurulsun.

Yeni origin ilişkisi exact kaynak kanıtı ister. Origin eklemek, kanıtsız manifest kabul etmenin yolu olmayacak. Ya eski lesson zinciri ya doğrulanmış yeni origin bağları eksiksiz olmalı. Kullanıcı geri bildirimi ve artifact revision ilişkileri tutulmalı.

Backup, migration plan digest’i, schema fingerprint, integrity/FK kontrolü, kesinti sonrası idempotent readback ve geri dönüş kapsamı mevcut araçlarla uygulanmalı. Canlı SQLite dosyasını sıradan file-copy ile yedekleme. Sonradan yazılan kullanıcı verisini geri dönüşte kaybetme. Schema v1 kabulü/upgrade politikasını açıkça sürümle; belirsiz fingerprint’i sessizce kabul etme.

**Kapı:** Eski fixture’lar ve eski kayıt digest’leri korunur; başarı/düzeltme adayı sahte failure olmadan üretilebilir; replay tek olayı iki saymaz.

### WP-02 — Paket köprüsü ve kaynak güvenliği

**Mevcut hedefler:** skill manifest, mevcut knowledge-file/object-store ve secret/source-security yardımcıları. **Gerekirse yeni küçük modül önerileri:** `domain/skill_package.py`, `application/skill_packages.py`. İsimler mevcut düzenle çakışıyorsa mevcut servisi genişlet.

Standart paket parse/validate/import/export, dosya digest manifesti, ortak semantik hash ve istemci projection bağlantısını kur. `SKILL.md` kaynak adları ile canonical skill kimliğinin birebir/namespace kurallarını belirt. Bozuk içerik import sırasında candidate bile olmadan reddedilebilsin; güvenli ama ölçülmemiş içerik quarantine/candidate kalsın.

İçeriğin referans bağlantıları kök dışına kaçamaz. İçe alma ve inspect ağ veya script çalıştırmaz. Lisans bilinmiyorsa otomatik yayınlama kapalı kalır. Büyük kaynaklar sınırsız context’e kopyalanmaz.

**Kapı:** SK-AC-013…018, parse/export round-trip, değişen asset’in eski eval’ı geçersiz kılması, kullanıcı dosyası koruma.

### WP-03 — Scope’lu discovery ve etkin revision

**Mevcut hedef:** `SQLiteLocalLearning.select_active_skills`; gerçek çağıranlar ve mevcut context compiler/ranking servisleri. **Gerekirse yeni application hizmeti:** `skill_discovery.py`.

Mevcut exact trigger profilinin davranışını geriye uyumlu koru. Genel kişisel katalog için scope+effective-state filtreli sorgu ekle. Aktivasyon geçmişinden en yeni kaydı körlemesine almak yerine kanonik etkin revision ve revocation/supersession çözümünü kullan.

Metadata listesi, explicit selection ve implicit routing aynı güvenlik filtresini kullansın. Pagination/bounded query ve açıklanabilir seçim gerekçesi üret. “Çok skill olunca ilk 256’yı al” davranışı kabul edilmez. Kaynak görünürlüğü ve sonucu üretme maliyeti ayrı gözlemlensin.

**Kapı:** SK-AC-019…024; >256 aktivasyon, çakışan aynı isim, eski revision, TR/EN ve near-negative örnekleri.

### WP-04 — Instruction skill’in gerçek kullanımı ve kullanıcı yüzeyi

**Mevcut hedefler:** `application/skill_runtime.py`, güncel agent/tool dispatch, local runtime admission, doğru üretim composition; `interfaces/cli` komut kayıtları.

Talimat paketini gerçek işe/agent assignment’a exact digest ile bağla. Koordinatör ve alt ajan aynı revision’ı kullanabilsin; alt ajan yalnız kendi izinli bounded context’ini alsın. Genel skill use kaydı journal outcome’u zorunlu kılan eski profile zorla sokulmasın; onun yanında doğru tipli sonuç kanalı bağlansın.

Aşağıdaki kullanıcı sözleşmesini mevcut komutlara çakışmadan uygula. **Bu komutlar hedef tasarımdır; araştırma sırasında mevcut oldukları doğrulanmadı.**

| Önerilen yüzey | İşlev | Varsayılan etki |
|---|---|---|
| `zekam skill list / inspect / explain` | Scope’lu katalog, kaynak ve seçim gerekçesi | Read-only, provider-free |
| `zekam skill propose` | İzinli evidence’den yeni aday veya revision planı | Plan; semantic model gerekiyorsa ayrı izin |
| `zekam skill evaluate` | Exact eval planı ve sonuç bağlantısı | Plan; apply ile mevcut evaluation job yolu |
| `zekam skill export` | İstemci/kapsam hedefleri ve artifact farkı | Plan; apply yönetilen hedefe yazar |
| `zekam skill status` | Etkin sürüm, dağıtım, kullanım ve açık kabul | Read-only, provider-free |

Yeni `skill activate` komutuyla ikinci activation yolu açma; mevcut evolution/approval yüzeyine bağlan. Kısa komut adları mevcut CLI ile farklıysa aynı sözleşmeyi mevcut isimlerle gerçekleştir, belgeyi güncelle; synonym komut çoğaltma.

**Kapı:** Paket load ile outcome ayrıdır; mock başarı yoktur; model/tool yokluğu doğru durumdur; tekrar çalışma dış etki çoğaltmaz.

### WP-05 — İstemci adapter’ları ve yönetilen dağıtım

**Mevcut hedefler:** aktif OpenCode lifecycle/bootstrap, mevcut istemci hook/instruction yönetimi ve local file security. Tarihsel wheel dışı modüllere yeni üretim bağımlılığı kurma. **Gerekirse yeni application hizmeti:** `skill_client_projection.py`.

Bir ortak paket için OpenCode/Codex/Claude projection planı üret. Kurulu sürümü ve gerçek keşif yolunu çöz; doğru dosyaları ownership manifestiyle atomik/geri alınabilir yaz. İstemci yeniden yükleme gerekiyorsa açıkça bildir ve kanıtla. Aynı skill’in farklı taranan dizinlerde çoğalmasını engelle.

İstemci alanlarını ve politika farklarını diff’te göster. `allowed-tools` veya dinamik shell ortak paketten sürüklenmesin. İstemcinin bağımsız tool erişimi Zekam admission dışındaysa enforced execution iddiası üretme.

**Kapı:** SK-AC-031…036; üç istemcinin contract testleri, mevcut kullanıcının dosyalarını koruma, native testlerin doğru platformda yapılması.

### WP-06 — Soğuk oturum değerlendirmesi ve kişiselleştirme

**Mevcut hedefler:** `SkillEvaluation`, improvement deney kayıtları, model registry/benchmark ve mevcut bağımsız verifier.

Outcome/process/preference/efficiency/safety grader’larını ayır. Baseline ve aday aynı model/harness/tool/bütçe koşullarında çalışsın. Eski sohbetin başarıyı taşımasını engellemek için yeni oturum başlat; implicit testlerde skill adı kullanıcı prompt’una elle eklenmesin. Explicit test ayrı ölçülsün.

Yazarın gördüğü örnekler geliştirme setidir. Paketteki 20 smoke vakası yalnız başlangıç materyalidir; gizli holdout değildir. Bağımsız verifier yeni acceptance setini ayrı izinli artifact alanında oluşturup digest’ini dondursun; aday yazarına oracle/cevap anahtarı olarak vermesin.

Başarısız, eşit, blocked ve regressed sonuçları da mevcut deney deposunda sakla. Minimum beş denemeyi korumak tek başına yeterli kabul değildir. Eşikler, örnek sayısı, non-inferiority toleransı, birincil iyileşme metriği ve bütçe deney planında önceden kayıtlı olsun. Mevcut v1 salt-success kuralını sessizce değiştirerek eski evaluation kanıtlarının anlamını değiştirme; yeni sözleşmeyi sürümle.

Yüzde yüz baseline senaryosunda daha düşük kaynak maliyetinin nasıl değerlendirileceği açık olsun. Kalite/safety düştüğünde ucuzluk aktivasyon gerekçesi olmasın. Küçük örnek belirsizliği raporlansın; yetersiz kanıt `ready` yapmasın.

Canlı provider/istemci deneyi için kullanılan veri, model kimliği, azami request/token/maliyet, retry ve concurrency exact planla yetkili olmalı. Mevcut sıfır-provider standing grant bu aşamayı otomatik yetkilendirmez.

**Kapı:** SK-AC-037…042 ve pilot acceptance. Grader çıktısı gerçek artifact/readback’e bağlıdır; bağımsız verifier yalnız farklı isim taşıyan aynı öz onay değildir.

### WP-07 — Evolution bağlantısı, bakım, recovery ve teslim

**Mevcut hedefler:** evolution capture/queue/compiler, `AuthorizedRolloutRuntime`, rollout domain, capability/status/report ve backup/recovery.

Öğrenme adayını mevcut kalıcı kuyruğa bağla; yeni scheduler kurma. Olay replay, dedupe, bounded retry ve maliyet sınırı korunsun. Sürekli çalışma ancak mevcut canlı kurulum/grant readback’i doğrulanmışsa yapılabilir. Bunun yokluğu skill katalog inspect’i veya provider-free kontrolleri engellemesin.

Shadow/local canary önce çalışır; fixture sonucu model üretim başarısı diye etiketlenmez. Gerekli yeni workload sözleşmesini sürümle. Düşük riskli, geri alınabilir instruction revision ancak uygun standing grant ve bağımsız değerlendirme ile aktive olabilir. Yeni tool, ağ/veri kapsamı veya kullanıcı dosyası değişikliği bu otomatik kapsamın dışındadır.

Aktivasyon atomik/CAS olmalı. Effect sonrası receipt öncesi kesinti, lost response, tekrar apply, revoke, paralel writer ve external pointer drift testleri çalışmalı. Rollback yalnız yönetilen revision/selector içindir; oluşmuş dış etkilerin geri alındığını iddia etmez. Kullanıcı içeriği silinmez.

Envanterde `skill catalog`, `skill learning`, `client distribution`, `verified execution` ve `native acceptance` ayrı görünür olsun. UI yeni tasarım istemez; mevcut report/status yüzeylerini beslemek yeterlidir. Kaynak eskiyse/stale ise durum bunu söylesin.

**Kapı:** SK-AC-043…048, regression baseline ve gerçek teslim raporu.

## 6. Test, kalite ve canlı kabul

### 6.1. Mevcut regresyon dayanakları

İncelenen depoda aşağıdaki integration test yolları var. Bunlar yeni çalıştırma sonucu değil, başlangıç hedefleridir. Uygulayıcı kaynak/fixture ve güvenlik koşullarını okuyup gerçek ortamda çalıştırır:

```text
python scripts/paket_dogrula.py
python -m pytest tests/integration/test_local_learning_sqlite.py -q
python -m pytest tests/integration/test_authorized_rollout_runtime.py -q
python -m pytest tests/integration/test_local_improvement.py -q
```

Yeni kod için unit/security/integration/e2e testleri ekle. Ruff/mypy ve proje DoD kapılarını mevcut yapılandırmadan türet. Testlerin kullanıcı ZEKAM_HOME’una veya canlı provider’a varsayılan bağlanmaması zorunludur. Test verisi fixture’dır; gerçek kurumsal veri yayına alınmaz.

Özellikle schema migration, parser/import/export, scope filtre, origin provenance, discovery sınırı, gerçek usage/outcome ayrımı, external package policy, client projection drift ve crash recovery testleri ayrı görünür olsun.

### 6.2. Platform ve paketleme matrisi

| Ortam | Zorunlu doğrulama | Yetersiz kanıt örneği |
|---|---|---|
| Provider-free fixture | Parse, migration, güvenlik, receipt/CAS, mock contract | Modelin doğru skill seçtiği iddiası |
| Windows native | Gerçek path/ACL/reparse, kurulu CLI keşfi, yönetilen export/readback | Linux üzerinde Windows path string testi |
| macOS native | Gerçek izin/path/link ve kurulu istemci keşfi | Windows testlerinin geçmiş olması |
| Temiz wheel | Kaynak checkout olmadan import/CLI/resource erişimi | Yalnız editable install |
| Onaylı model + istemci | Soğuk oturum seçim/çıktı/düzeltme ve maliyet | Fixture canary veya modelin kendi başarı beyanı |

Yapılamayan kombinasyon `not_run`, `not_configured`, `blocked` veya `not_supported` olarak gerekçeli kaydedilir. Üç istemcide API/contract desteği ayrı, native kabul ayrı değerlendirilir. Eksik native kabul varken evrensel taşınabilirlik garantisi verilmez.

### 6.3. Kabul senaryoları

Aşağıdaki 48 senaryo `KABUL_TEST_PLANI.json` ile aynı kimlikleri kullanır. Tümü yeni uygulama kabul girdisidir; başlangıçta çalıştırılmamıştır. P0 maddeleri release blocker’dır; P1 maddeleri hedef ürün davranışıdır. P1 atlanırsa tam kapsam tamamlandı denmez.
| Kimlik | Öncelik | Senaryo | Beklenen kanıt |
|---|---|---|---|
| SK-AC-001 | P0 | Dirty çalışma ağacında dosyalar korunur | Yerel değişiklikler reset/stash/checkout ile kaybolmaz; mutation bekler. |
| SK-AC-002 | P0 | Aktif lease varken scope değişmez | Güvenli checkpoint/transition olmadan MD authority veya grant yeniden bağlanmaz. |
| SK-AC-003 | P0 | Eski açık iş devredilir | Aynı iş/claim yeniden oluşturulmaz; eski receipt zinciri korunur. |
| SK-AC-004 | P0 | Yeni görev kimliği evolution ile tutarlıdır | Sabit eski kimliğe bağımlılık kaldırılır veya sürümlü transition ile uzlaştırılır; fail-closed testleri geçer. |
| SK-AC-005 | P0 | Frontmatter ve projection exact eşleşir | Eksik/fazla alan ve byte digest farkı reddedilir. |
| SK-AC-006 | P0 | Grant kapsamı genişlemez | Task değişimi yeni provider veya otomatik yayın yetkisi üretmez. |
| SK-AC-007 | P0 | Başarıdan aday üretilir | Gerçek başarı kanıtı failure_card uydurmadan origin olarak saklanır. |
| SK-AC-008 | P0 | Düzeltmeden aday üretilir | Önceki çıktı, düzeltme ve kabul edilen revizyon ayrı kaynaklara bağlıdır. |
| SK-AC-009 | P0 | Geri bildirim yetki vermez | Onaylanan metin, aktivasyon veya araç yetkisi yerine geçmez. |
| SK-AC-010 | P0 | Tekrarlanan olay tek sayılır | Aynı olayın hook, resume ve compiler tekrarları aday sayısını artırmaz. |
| SK-AC-011 | P1 | Mevcut skill güncellenir | Aynı yöntem için gereksiz yeni kimlik yerine yeni revision üretilir. |
| SK-AC-012 | P0 | Scope çelişkisi korunur | Proje kuralı kullanıcının global tercihini sessizce değiştirmez. |
| SK-AC-013 | P0 | Standart metadata doğrulanır | Eksik/duplicate name-description, yanlış klasör adı ve bozuk UTF-8 reddedilir. |
| SK-AC-014 | P0 | Yollar güvenlidir | Traversal, mutlak yol, symlink/junction kaçışı ve Windows ADS girişi reddedilir. |
| SK-AC-015 | P0 | Dış paket karantinaya alınır | İçe alma hiçbir script, dinamik shell veya hook çalıştırmaz. |
| SK-AC-016 | P0 | İzinli araç alanı authority olmaz | allowed-tools ve istemci uzantıları güvenilir grant üretmez. |
| SK-AC-017 | P0 | Paket digest değişimi fark edilir | Referans veya asset tek byte değişirse eski eval/install bağı geçersiz olur. |
| SK-AC-018 | P1 | Round-trip anlamı korur | Export → parse → canonical manifest/asset digest eşitliği sağlanır. |
| SK-AC-019 | P0 | ACL metadata öncesinde uygulanır | Yetkisiz projenin adı, açıklaması ve örnekleri katalogda görünmez. |
| SK-AC-020 | P0 | Pasif sürüm seçilmez | Deprecated/revoked/retired veya eski revision etkin seçimde yer almaz. |
| SK-AC-021 | P1 | İlgili olmayan istekte abstain | Benzer kelime geçen ama farklı amaçlı istek skill çalıştırmaz. |
| SK-AC-022 | P1 | TR/EN tetikleme sınanır | Türkçe doğal istek ve eşdeğer İngilizce istek beklenen adaylara gider. |
| SK-AC-023 | P0 | Katalog sınırı kontrollüdür | 256+ kayıt sessiz eksilme yaratmaz; filtreli bounded sorgu/pagination veya açık durum vardır. |
| SK-AC-024 | P1 | Bağlam bütçesi görünürdür | Metadata, body ve referans maliyetleri ayrı raporlanır; body ihtiyaç öncesi yüklenmez. |
| SK-AC-025 | P0 | Yüklemek başarı sayılmaz | Discovered/loaded/invoked/completed/verified birbirinden ayrıdır. |
| SK-AC-026 | P0 | Genel skill sahte journal üretmez | Araştırma çıktısı journal append sonucu ile başarılandırılmaz. |
| SK-AC-027 | P0 | Araç etkisi mevcut admission yolundadır | Grant, claim, receipt ve terminal doğrulama atlanamaz. |
| SK-AC-028 | P0 | Alt ajan aynı sürümü kullanır | Bounded context skill/package/harness digest ve child scope taşır. |
| SK-AC-029 | P0 | Eksik yetenekte dürüst durur | Model/tool yoksa blocked/degraded; hayali çıktı veya test sonucu yoktur. |
| SK-AC-030 | P0 | Retried çalışma idempotenttir | Aynı iş yeni dış etki veya mükerrer usage üretmez. |
| SK-AC-031 | P0 | OpenCode tek yönetilen kopyayı görür | Birden fazla keşif yolunda aynı skill çoğaltılmaz; bilinmeyen alan güvenlik sağlamaz. |
| SK-AC-032 | P0 | Codex export doğru kapsamdadır | İzin verilmiş .agents/skills hedefi kullanılır; kişisel/global sızıntı yoktur. |
| SK-AC-033 | P0 | Claude export izin genişletmez | Dinamik shell ve allowed-tools varsayılan exporta taşınmaz; desteklenmeyen politika açıkça bildirilir. |
| SK-AC-034 | P0 | Kullanıcı dosyası ezilmez | Managed hedefin yerel değişikliği drift sayılır; karşılaştırmasız overwrite yoktur. |
| SK-AC-035 | P0 | Windows ve macOS native kanıtı ayrıdır | Bir işletim sistemi testi diğerinin canlı kabulü sayılamaz. |
| SK-AC-036 | P1 | Wheel kurulumunda erişilir | Üretim composition yalnız checkout/tarihsel dışlanmış modüllere dayanmaz. |
| SK-AC-037 | P0 | Soğuk oturum deneyi ayrıdır | Yeni model contextinde eski örnek veya elle skill adı ipucu olmadan implicit seçim sınanır. |
| SK-AC-038 | P0 | Bağımsız holdout kullanılır | Yazarın gördüğü paket örnekleri gizli holdout olarak sunulmaz. |
| SK-AC-039 | P0 | Başarısız ölçüm de saklanır | Failed/blocked/equal/regressed trial kanıtı silinmez veya geçer duruma çevrilmez. |
| SK-AC-040 | P1 | Tavan başarıda verimlilik değerlendirilebilir | %100 baseline için kalite korunup önceden seçilmiş maliyet metriği iyileşebilir; sessiz eşik gevşetme yoktur. |
| SK-AC-041 | P0 | Belirsizlik ve örnek sayısı raporlanır | Beş deneme evrensel güvenilirlik veya anlamlı iyileşme kanıtı sayılmaz. |
| SK-AC-042 | P0 | Gerçek verifier ayrıdır | Yalnız farklı rol etiketi taşıyan aynı doğrulamasız yazar sonucu kabul edilmez. |
| SK-AC-043 | P0 | Shadow aktif pointer değiştirmez | Çıktı/eval üretilse de etkin tüketim değişmez. |
| SK-AC-044 | P0 | Yerel canary canlı başarı sayılmaz | local-deterministic-fixture ve production_traffic=false korunur. |
| SK-AC-045 | P0 | CAS ve crash recovery çalışır | Etki/receipt arasında kesintide tekrar yan etki yapılmaz; readback ile uzlaştırılır. |
| SK-AC-046 | P0 | Revocation sonraki kullanımı durdurur | Yüklü eski context de yeni effect admission aşamasında engellenir. |
| SK-AC-047 | P0 | Geri dönüş kullanıcı verisini korur | Yalnız yönetilen sürüm/selector geri alınır; kullanıcı dosyası veya sonradan üretilen veri silinmez. |
| SK-AC-048 | P0 | Sonuç matrisi dürüsttür | Kod/test/kurulum/native/provider kabulü ayrı; çalışmayan aşama passed yazılmaz. |

## 7. Rollback, durdurma ve ilerleme koşulları

Her WP sonunda diff, gerçek test sonucu, risk ve sonraki güvenli adım checkpoint’i üret. Kapsam içi, geri alınabilir ve yetkili işlemlerde tekrar tekrar kullanıcı onayı isteme. Gerçek yetki/bütçe eksikliği veya kullanıcı değişikliği çakışması varsa yalnız ilgili etkiyi durdur; sonuç uydurma.

Tetiklenen bir güvenlik ihlali, scope sızıntısı, sahte kabul, onay atlama veya schema bozulması bütün otomatik aktivasyonları durdurur. Erişim veya model yokluğu tüm ürünü durdurmak zorunda değildir; yalnız o yeteneğin durumunu düşürür.

Rollback planı migration öncesi hazırlanır. Migration sonrasında yeni kayıtlar yazıldıysa eski yedeğe dönüp bunları kaybetmek rollback kabul edilmez; mevcut kurtarma protokolüne göre forward repair veya veri-koruyan uzlaştırma yapılır. Selector drift varken CAS zorlanmaz. Kullanıcının düzenlediği generated dosya ayrıca korunur.

## 8. Bağımsız ajan ve doğrulama düzeni

Gerçek uygulamada en az bir gerçek subagent kullan; koordinatörü bu sayıya dahil etme. Builder ve verifier farklı assignment/context ve gerekli yerde farklı process boundary taşır. Tek yazılabilir logical resource’ta aynı anda bir builder olsun.

Verifier yalnız builder’ın özetini değil; gerçek diff, kaynak kanıtı, test sonucu ve çıktı artifact’lerini inceler. Bir işin yazarı kendi skill’ini kendi beyanıyla aktive edemez. Model judge bağımsız kanıtın yerine geçmez; kritik güvenlik kuralları deterministik kontrol ister.

Bu araştırma paketinin hazırlanmasında gerçek ürün builder/verifier süreci çalıştırılmadı. Paket biçim kontrolü bağımsız ürün kabulü olarak kullanılmayacak.

## 9. Tamamlanma tanımı ve teslim

Aşağıdaki sonuçlar kanıtlı olmadan görev kapatılamaz:

- Başarılı iş ve gerçek kullanıcı düzeltmesinden, sahte hata kartı olmadan kaynaklı aday/revision oluşur.
- Mevcut skill yeniden kullanılır; gereksiz katalog çoğalması ve scope dışı metadata görünürlüğü engellenir.
- Standart paket ile kanonik kayıt/etkin revision/istemci artifact’i arasında digest zinciri vardır.
- Fresh-session kullanımında doğru seçim ve kabul edilen çıktı ölçülür; load ile quality/effect başarıları ayrıdır.
- Üç istemci için destek durumu gerçek kanıtla raporlanır; desteklenmeyen policy enforce edilmiş sayılmaz.
- Eski journal profili, eski learning verisi ve evolution/recovery güvenlik kapıları korunur.
- Standart regression/quality, yeni 48 kabul senaryosu ve gerekçeli platform/provider matrisi raporlanır.
- Kullanıcı verisi korunmuş, görev geçişi/projection/manifestler tutarlı ve açık işler doğru devredilmiştir.

Nihai uygulama tesliminde kaynak ağacı dışında tek ana Markdown kabul raporu ve makinece okunabilir kanıt indeksi üret. Raporda değişen gerçek dosyalar, migration ve rollback kanıtı, test komutları/exit kodları, doğrulayıcı kaydı, çalıştırılmayan alanlar, cihaz/istemci sürümleri ve kalan blocker’lar yer alsın. Ham secret, provider cevabı veya kişisel kayıtları rapora dökme.

**Kurulum yapıldı**, **provider-free test geçti**, **gerçek model kabulü geçti** ve **canlı otomasyon yetkilendirildi** ifadeleri birbirinden ayrı olsun. Görev planı teslimi bu dört durumun yerine geçmez.

## 10. Uygulayıcıya başlangıç metni

Aşağıdaki metin bu görev paketinin devri içindir; yeni kapsam eklemez:

> AGENTS.md dosyasını oku ve başlangıç protokolünü uygula. Bu teslimdeki AKTIF_GOREV.md dosyasını, yaşayan eski görevi ve açık işleri koruyarak WP-00 güvenli görev geçişiyle benimse. Önce gerçek kaynak kökü, Git durumu, task/projection/evolution bağları ve baseline testlerini doğrula. Daha yeni HEAD varsa eskiye dönme. Sonra yalnız ZEKAM-PERSONAL-SKILL-LIFECYCLE-001 kapsamındaki iş paketlerini mevcut altyapıyı yeniden kullanarak uygula. Kullanıcı verisini koru; yeni store/scheduler/approval sistemi kurma. Commit/push, canlı model çağrısı ve yeni işletim yetkisi üretme. Yetkili kapsam içinde tekrar onay istemeden ilerle; gerçek blocker’ları, yapılan testleri ve çalıştırılmayan alanları dürüst kaydet. Gerçek bağımsız verifier ve kabul kanıtı olmadan tamamlandı deme.

## 11. Kaynaklar

Bu görev, yanında gelen `ZEKAM_SKILL_ARASTIRMA_RAPORU.md` ve `KAYNAKLAR.json` ile izlenebilir. Esas depo revizyonu frontmatter’da sabittir. Temel kaynaklar aşağıdadır; dosyaların bazıları yalnız ilgili aralıklarıyla incelenmiştir.
- **[R01] AGENTS.md** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/AGENTS.md
  İnceleme: tam dosya.
- **[R02] 00_BASLA.md** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/00_BASLA.md
  İnceleme: tam dosya.
- **[R03] AKTIF_GOREV.md** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/AKTIF_GOREV.md
  İnceleme: başlangıç ve kapsam bölümleri; tüm maddelerin tamamlanma durumu doğrulanmadı.
- **[R04] src/zekam/application/active_task_contract.py** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/src/zekam/application/active_task_contract.py
  İnceleme: 1–180.
- **[R05] src/zekam/application/evolution_runtime.py** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/src/zekam/application/evolution_runtime.py
  İnceleme: 1–180.
- **[R06] src/zekam/infrastructure/sqlite/local_learning.py** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/src/zekam/infrastructure/sqlite/local_learning.py
  İnceleme: 1–400, 850–1340, 1460–dosya sonu; seçili bölümler.
- **[R07] src/zekam/application/skill_runtime.py** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/src/zekam/application/skill_runtime.py
  İnceleme: 1–240.
- **[R08] src/zekam/domain/learning.py** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/src/zekam/domain/learning.py
  İnceleme: Skill/SkillEvaluation ve ilgili domain bölümleri.
- **[R09] src/zekam/infrastructure/local_core_services.py** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/src/zekam/infrastructure/local_core_services.py
  İnceleme: 1–230.
- **[R10] src/zekam/application/rollout_runtime.py** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/src/zekam/application/rollout_runtime.py
  İnceleme: 1–220.
- **[R11] src/zekam/domain/evolution_rollout.py** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/src/zekam/domain/evolution_rollout.py
  İnceleme: 1–200.
- **[R12] docs/OTONOM_EVOLUTION_RUNBOOK.md** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/docs/OTONOM_EVOLUTION_RUNBOOK.md
  İnceleme: tam dosya.
- **[R13] docs/ZEKAM_YETKINLIK_ENVANTERI.md** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/docs/ZEKAM_YETKINLIK_ENVANTERI.md
  İnceleme: tam dosya.
- **[R14] README.md** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/README.md
  İnceleme: tam dosya.
- **[R15] pyproject.toml** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/pyproject.toml
  İnceleme: tam dosya.
- **[R16] tests/integration/test_local_learning_sqlite.py** — https://github.com/mehmet-karacan/zekam/blob/d273e543600176a1b7cfc39b6696994cf8fec5cf/tests/integration/test_local_learning_sqlite.py
  İnceleme: 1–160; testler çalıştırılmadı.
- **[S01] Agent Skills specification** — https://agentskills.io/specification
  İnceleme: SKILL.md ortak biçimi ve kademeli yükleme.
- **[S02] OpenCode Agent Skills** — https://opencode.ai/docs/skills/
  İnceleme: Keşif yolları ve skill yükleme izinleri.
- **[S03] OpenAI Build skills** — https://learn.chatgpt.com/docs/build-skills
  İnceleme: Codex yerel keşif ve instruction-only yaklaşımı; developers.openai.com/codex/skills yönlendirmesi.
- **[S04] Claude Code skills** — https://code.claude.com/docs/en/skills
  İnceleme: İstemciye özel alanlar, tool izinlerinin ve bağlam maliyetinin sınırları.
- **[S05] Agent Skills: Evaluating skill output quality** — https://agentskills.io/skill-creation/evaluating-skills
  İnceleme: Çıktı kalitesinin ölçülmesi.
- **[S06] OpenAI: Testing Agent Skills Systematically with Evals** — https://developers.openai.com/blog/eval-skills
  İnceleme: Çıktı, süreç, stil ve verimlilik değerlendirmesi.
- **[S07] Claude Platform: Agent Skills** — https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
  İnceleme: Skill mimarisi ve güvenlik değerlendirmesi.
- **[U01] Kullanıcının sağladığı video transkripti** — https://www.youtube.com/watch?v=PUtaB4uYvvA
  İnceleme: Bu konuşmada sağlanan transkript okundu; video bağımsız izlenmedi.
