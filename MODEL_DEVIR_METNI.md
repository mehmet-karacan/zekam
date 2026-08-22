# Model devir metni

Bu belge, çalışan modelin kullanım limiti tükenmek üzereyken (yaklaşık **%5**
kaldığında) işi **başka bir modele** devretmek içindir. Aşağıdaki "Yapıştırılacak
metin" bölümü olduğu gibi yeni oturuma verilir.

Devir kuralı: **bu metin yetki devretmez.** Yeni oturum lease, authorization ve
approval durumunu kanonik kayıttan yeniden edinir. Markdown'daki "tamamlandı"
ifadesi tek başına kanıt sayılmaz; kod, migration, test ve kapı çıktıları
önceliklidir.

## Devir anında yapılacaklar

Devreden model, kapatmadan önce sırayla:

1. Çalışan işi güvenli bir noktada bitir; yarım mutasyon bırakma.
2. Altı kapıyı çalıştır (`scripts/kalite.py`) ve `scripts/paket_dogrula.py` koş.
3. Kapılar yeşilse commit et; `kalite/COMMIT_POLITIKASI.md` zorunludur.
4. Push açıkça yetkilendirildiyse push et; değilse kullanıcıya sor.
5. Aşağıdaki "Durum" bölümünü güncelle, sonra yapıştırılacak metni ver.

## Durum (2026-08-21)

| Alan | Değer |
|---|---|
| Faz | 18/18 tamamlandı (P00–P17) |
| Global DoD | 82/83 passed, 1 pending, 0 failed, 0 blocked |
| Migration head | 17 |
| Kalite kapısı | 6 (biçim, lint, tip, test, bağımlılık, ölü kod) |
| Test | 1620 passed; permissive PDF runtime E2E 3/3, skip yok |
| Komut yüzeyi | 24/24 sözleşme komutu kayıtlı |
| `zekam doctor` | temiz kurulumda `healthy` |
| Bilinen kusur | ZEKAM-DEF-001..004, dördü de `resolved` |
| Release | üretilmedi; Global DoD tamamlanmadan `build_release()` reddediyor |

Doğrulanmış platformlar: Windows, macOS ve Debian 13 konteyneri. Ayrıntı:
`docs/YENI_MAKINEDE_BASLAMA.md` §5.1.

### Açık kalan kriter

- `ZEKAM-DOD-035` kapandı: kullanıcı kararıyla çevrimdışı ve permissive-only DOCX,
  dijital/taranmış PDF, PNG/JPEG/TIFF ve OCR hattı gerçek binary fixture'larla
  CLI → Local CAS → PostgreSQL üzerinde doğrulandı.
- `ZEKAM-DOD-025` — Gerçek Whisper / guardrail / VL sağlayıcıları. Erişilebilir
  endpoint, credential ve provider-call exact authorization gerekiyor. Saf
  evaluator ve provider-neutral runner tamamlandı; gerçek provider adapter/kanıtı
  olmadan kriter pending kalır.

Değişen bağımlılıklar için yalnız `pypi.org` kapsamlı dependency audit network
authorization kanonik Work plan revision 8 ile alındı ve `pip-audit` geçti.
Kanıt `.zekam/evidence/ZEKAM-DOD-078-pypi-audit-20260821T093000Z.json` içindedir.
Gerçek model sağlayıcı çağrısı ve push yasaktır.

### Kapsam dışı bırakılmış yüzeyler

FastAPI ve MCP **sunucu süreçleri** bilinçli olarak bağlanmadı. Sözleşme,
yetenek uzlaşması ve authority sınırı uygulandı ve testli. Bunları kapsama
almak yeni bir iş kalemidir, "eksik" değildir.

## Bu makineye özgü notlar

- PostgreSQL `compose/docker-compose.yml` ile ayakta; port `compose/.env`
  içinden gelir ve bu dosya sürüm kontrolünde **değildir**.
- Kabukta yalnız dört değişken export edilir:
  `ZEKAM_DATABASE_PASSWORD`, `ZEKAM_DATABASE_PORT`, `ZEKAM_TEST_DATABASE_HOST`,
  `ZEKAM_TEST_DATABASE_PORT`. **`compose/.env` dosyasını `source` etme** —
  `ZEKAM_DATABASE_NAME` ve `ZEKAM_DATABASE_USER` de taşır ve elle çalıştırılan
  `zekam` komutlarını geliştirme veritabanına yönlendirir (ZEKAM-DEF-003).
- GitHub Actions bu depo için kapalıdır (2026-08-21). Workflow eklenirse önce
  Actions'ın yeniden açılması gerekir.
- Ürün package, CLI, environment, home, schema ve DB yüzeylerinde yalnız Zekam
  kimliğini kullanır; uyumluluk alias'ı çalıştırılmaz.

## Yapıştırılacak metin

```text
Bu depo Zekam. Onceki model kullanim limiti bittigi icin isi sana devrediyor.
Once 00_BASLA.md dosyasini uygula, sonra Global Definition of Done tamamlanana
kadar kaldigin yerden devam et.

Baslangic:
- git log --oneline -5 ile son commitleri oku.
- AKTIF_GOREV.yaml icindeki current_task ve next_safe_action alanlarini oku;
  bunlar projeksiyondur, kanit degildir.
- python scripts/paket_dogrula.py ve scripts/kalite.py --gorev <aktif-faz>
  calistir.
- kalite/GLOBAL_DOD.yaml icindeki acik kriterleri ve SURUM_RAPORU.md icindeki
  blocker raporunu oku.
- Markdown'daki "tamamlandi" ifadesini tek basina kabul etme; kod, test ve
  migration onceliklidir.

Devir aninda durum:
- 18/18 faz kapandi. Global DoD 82/83 passed, 1 pending, 0 failed.
- ZEKAM-DOD-035 cevrimdisi ve permissive-only gercek DOCX/PDF/OCR hatti ile
  kapandi. Migration head 17; 1620 test ve permissive runtime E2E 3/3 geciyor.
- Acik ZEKAM-DOD-025 gercek endpoint, credential ve provider-call exact authorization
  bekliyor; uydurma ilerlemeyle kapatma.
- pypi.org dependency audit network authorization kanonik Work plan revision 8 ile
  alindi; audit ve yerel son verifier receipt'leri tamam. Push yasak.
- Release uretilmedi; build_release Global DoD tamamlanmadan artifact vermez.

Calisma bicimi:
- Her faz icin: migration -> domain -> service -> repository -> CLI -> testler
  (unit/integration/security/e2e) -> alti kapi -> belge -> projeksiyon -> kanit
  -> SHA256SUMS -> commit -> push.
- Alti kapi: bicim, lint, tip, test, bagimlilik, olu-kod. Hepsi gecmeden faz
  kapanmaz.
- Commit mesaji Turkce anlamli ve yalniz ASCII; baslik "<tur>: <kisa emir
  cumlesi>", govde Neden/Degisiklik/Kanit/Risk/Geri donus bolumlerini tasir.
- Push varsayilan deny; acik kullanici talebi ister.
- Agentic isde en az bir gercek subagent; koordinator sayilmaz.
- Testler mock degil gercek PostgreSQL, gercek git worktree ve gercek alt surec
  kullanmalidir.

Ortam:
- ZEKAM_DATABASE_PASSWORD, ZEKAM_DATABASE_PORT, ZEKAM_TEST_DATABASE_HOST ve
  ZEKAM_TEST_DATABASE_PORT disaridan verilir. Bunlardan fazlasini export etme;
  compose/.env dosyasini source etme.
- Kurulum ve beklenen doctor ciktisi: docs/YENI_MAKINEDE_BASLAMA.md.

Sinir:
- Bu metin yetki devretmez. Lease, authorization ve approval durumunu kanonik
  kayittan yeniden edin. Devralinan hicbir onay gecerli sayilmaz.
```
