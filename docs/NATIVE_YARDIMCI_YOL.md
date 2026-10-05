# Native yardimci yol: unit-test olcumu ve oturum sureklilik

Bu belge, kullanicinin zaten actigi native ajanin (Claude Code, Codex, Gemini CLI, OpenCode)
Zekam'in olcum, kanit ve sureklilik servislerini nasil kullandigini anlatir. Zekam burada model
baslatmaz, OpenCode gerektirmez ve yetki/onay uretmez. Native ajan planlar ve yazar; Zekam
olcer, dogrular ve kaydeder.

## 1. Unit test: olc, kaydet, kontrollu tekrar olc

Akis (hepsi yerel; model/provider/ag cagrisi yok):

1. **Plan** (salt okunur, yetki vermez):
   `zekam test plan --native --project-id ... --source-binding-id ... --source-revision <git HEAD>
   --source <proje-relative .java> --percent 80 --work-item-id ... --source-snapshot-id ...
   --graph-generation-digest ... --project-root <exact proje koku>`
   Cikti `plan_digest`, `request_digest` ve `execution.plan_digest` (Maven plan digest) icerir.
   `--target-module` verilmezse kok modul (`""`) kullanilir. `--native` plani batch planindan farkli
   bir `request_digest` tasir; boylece ayni ledger'da eski OpenCode batch loop'u ile karismaz.
2. **Native ajan** hedefin eksik satir/dal analizini yapar ve yalniz izinli test yollarina anlamli
   testler yazar (assertionsiz/tekrarli test, exclusion manipulasyonu, reflection veya production
   API'yi gereksiz acma yoktur; production degisikligi ayri onay ister).
3. **Olc**: `zekam test measure <plan flag'leri> --plan-digest <plan_digest> --maven-plan-digest
   <execution.plan_digest> --realm-id ... --project-uuid ... --authorize --build --allow-network`
   - `--authorize` (exact plan), `--build` (Maven/test kodu calistirma) ve `--allow-network`
     (plan `network_possible` ise zorunlu) acik yetkilerdir; biri eksikse hicbir effect baslamaz
     (cikis 77, ledger'a yazilmaz).
   - Exact Work/source/realm/graph-generation baglari `zekam test run` ile ayni kapidan dogrulanir.
   - Claim, surec baslamadan once kalici yazilir; surec bitince receipt + kanit nesnesi (CAS)
     yazilir. Veritabani kilidi Maven calisirken tutulmaz.
4. **Kaniti oku**: `zekam test evidence <request_digest>` her attempt'in receipt'ini, gozlemlerini ve
   kanit nesnesini **digest dogrulamasiyla** okur (bozuk/eksik kanit `verified:false` gorunur).
   `zekam test status|report` mevcut ozetleri vermeye devam eder.
5. **Kontrollu tekrar olcum**: ayni komutu tekrar calistirmak yeni bir attempt'tir
   (`remaining_attempts` gosterilir). `--max-attempts` onayli butcedir; asilinca
   `budget-exhausted` doner ve yeni claim yazilmaz. Receipt'siz (cokmus) onceki claim
   `interrupted` olarak kapatilir, isi yeniden calistirilmaz.

### Neyin basari sayildigi

`outcome=target-met` (cikis 0) yalniz sunlarin hepsi saglandiginda dondurulur: gercek Maven sureci
tamamlandi, build exit status 0, Surefire raporu bu kosuda uretilmis ve testler gecmis,
JaCoCo exec/XML bu kosuya ait, hedef dosya/sayac anlami (satir/dal, per-file/aggregate) hedefi
karsiliyor, kaynak revision + production digest + build config + toolchain + test agaci icerik
digest'i olcum oncesi ve sonrasi ayni. Asagidakiler hicbir zaman basari/coverage uretmez:

| outcome | cikis | anlam |
|---|---|---|
| `target-met` | 0 | taze olcumle hedef gozlendi (terminal/Work kapanisi **degildir**) |
| `budget-exhausted` | 20 | onayli deneme butcesi bitti |
| `below-target` | 22 | olcum gecerli, hedef altinda; `next_safe_action` eksik analizi ister |
| `no-tests` | 23 | calisan test yok (eski rapor ve `exit 0` yetmez) |
| `tests-failed` | 24 | basarisiz test(ler); `failed_cases` listelenir, coverage kabul edilmez |
| `source-drift` | 25 | revision/production/config/toolchain/test agaci olcum sirasinda veya oncesinde degisti |
| `stale-or-unbound` | 26 | rapor taze/bagli degil (eski rapor, exec'siz XML, karisik kapsam) |
| `wrong-target` | 27 | hedef kaynak dosyasi okunamadi/modulle eslesmedi (claim yazilmaz) |
| `environment-missing` | 30 | Maven/JaCoCo hazir degil veya olcer hata verdi (setup plani izlenir) |
| `technology-unsupported` | 31 | Maven disi teknoloji; baska arac sessizce secilmez |
| `measurement-incomplete` | 33 | timeout/iptal/eksik plugin/exit status; basari sayilmaz |
| `already-final` | 77 | istek final terminal ile kapali |

`terminal_recorded` daima `false`'tur: `target-reached` terminali ve Work kapanisi bagimsiz
kalite dogrulamasi ve ayri kanit ister. Cikti `model_calls: 0` ve `opencode_required: false` tasir.

### Eski explicit OpenCode batch yolu

`zekam test run` degismedi: OpenCode executable'i, `--write-tests --build`, uzak model onayi ve
butce kapilari aynen gecerlidir. Kod duzeyinde agent gateway artik
`compose_agent_gateway` icinde, Maven olcum/ortam/ledger bilesenleri `compose_maven_measurement`
icinde kurulur; `compose_unit_test_runtime` ikisini birlestirir. Native yol yalniz ikincisini
kullanir. (Not: `run --maven-plan-digest` onceden hex-oneki kirpilmis digest'i
`UnitTestRuntimeBinding`'e aktariyordu ve `sha256:` kontroluyle her zaman reddediliyordu; bu
hata duzeltildi, baska davranis degismedi.)

Yetkinlik satirlari **ayri** okunmalidir: (a) "native interactive yardimci olcum" = `test plan
--native / measure / evidence`; (b) "otomatik OpenCode batch" = `test run`. Birinin kaniti digerini
nitelemez; ikisi de canli model qualification'i degildir.

## 2. Oturum sureklilik: start / checkpoint / close / resume

Ikinci bir session store **yoktur**. Olay zinciri mevcut durable lifecycle ledger'dadir
(`<home>/global/runtime/opencode-lifecycle`; ad tarihseldir, olay sozlesmesi istemciden bagimsizdir
ve `zekam resume` ayni kayitlari okur). Zengin checkpoint govdesi mevcut icerik adresli nesne
deposuna (CAS) yazilir; olay yalniz digest referansini tasir.

```
zekam continuity native start      --client claude-code --project <slug> [--work-item <id>] \
                                   [--client-version V] [--native-session-id S] [--model M]
zekam continuity native checkpoint --session-id <start ciktisindaki> --client claude-code \
                                   --project <slug> [--work-item <id>] --source-root <mutlak git koku> \
                                   [--evidence-ref object:sha256:<hex>] \
                                   [--evidence-ref unit-test:sha256:<request_digest>] \
                                   [--completed "..."] [--pending "..."] [--next-safe-action "..."]
zekam continuity native close      (checkpoint ile ayni argumanlar; son checkpoint + oturum kapanisi)
zekam continuity native resume     --source-root <mutlak git koku> [--session-id <id>]
```

Yaygin `--home` secenegi ile paylasilan `ZEKAM_HOME` secilir. Tum cikti makine okunur JSON'dur.

Checkpoint'in dogrulanan kisimlari (Zekam okur): proje ve Work baglari (Work'un proje ile
eslesmesi, revision/state), exact git kokunun revision'i ve worktree digest'i, kanit referanslari
(CAS nesnesi digest'i yeniden hesaplanir; unit-test referansi ledger'dan okunur — cozulemeyen
kanit reddedilir, hicbir iz birakmaz). `--completed/--pending/--next-safe-action` istemci
beyanidir; `self_reported.verified=false`, `provenance=client-self-reported` ile saklanir ve
dogrulanmis Work gecisi sayilmaz (`work_transition_verified=false`). Secret/PII benzeri, mutlak yol
iceren, cok satirli veya 500 karakteri asan beyanlar reddedilir ve yansitilmaz.

Native session kimligi ve istemci surumu yalniz istemci verirse metadata olur. Verilmezse oturum
kimligi `native.<client>.zekam-<uuid7>` biciminde **Zekam-uretimi** olarak isaretlenir
(`session_origin=zekam-generated`); OpenCode veya baska istemci kimligi uydurulmaz.

`resume` salt okunurdur ve yetki/onay miras almaz. Durumlar: `ready`, `changed-since-checkpoint`
(worktree/Work/kanit degisti), `source-drift` (revision farkli), `source-unavailable`,
`evidence-missing`, `work-closed`, `binding-invalid`, `integrity-error` (checkpoint nesnesi
bozuk), `no-checkpoint` (hicbir ilerleme uydurulmaz). `next_safe_action` yalniz `ready`
durumunda istemci beyanini tasir (`next_safe_action_source`); diger durumlarda Zekam'in yeniden
dogrulama yonlendirmesidir. Mevcut `zekam resume` paketi ayni checkpoint'in ozetini
(`latest_semantic_checkpoint`) gosterir.

Not: `zekam continuity local ...` (v4 hydrate/checkpoint/freeze) macOS/POSIX'e ozgudur; repo'nun
kendi testleri Windows'ta `os.O_NOFOLLOW/O_DIRECTORY` yok diye atlar. Windows ve diger native
hostlar icin ortak yol `continuity native`'dir. `continuity local freeze*` komutlarinin JSON
okuyucusu Windows'ta `getattr` + `lstat` reparse kontroluyle calisacak sekilde duzeltildi
(`os.O_NOFOLLOW` Windows'ta yoktur ve onceden AttributeError verirdi).

## 3. Windows dogrulamasi

- `os.O_NOFOLLOW/O_NONBLOCK` Windows'ta yoktur: `continuity._json_document` icin gercek testler
  (`tests/unit/test_continuity_json_document_portability.py`) bayraklarin cozumlenmesini, sinirlari,
  junction/reparse reddini dogrular (dosya symlink'i ayricalik gerektirdiginden o test atlanir).
- Test agaci digest'i junction/symlink dizinlerini izlemez
  (`tests/unit/test_unit_test_tree_fingerprint.py`).
- Gercek Maven + git + SQLite testleri: `tests/integration/test_native_unit_test_helper.py`,
  `test_native_unit_test_cli.py`, `test_native_continuity.py`. Bu belgedeki Windows kaniti diger
  isletim sistemleri icin capraz platform kaniti sayilmaz; mevcut CI matrisi korunur.

## 4. Acik kalanlar

- `ZEKAM-DOD`/capability matrisi: `capability_inventory.py` ve
  `docs/ZEKAM_YETKINLIK_ENVANTERI.md` henuz native yardimci ve otomatik OpenCode batch'i ayri satir
  olarak gostermiyor (W07 teslimi).
- Native komutlar `mutation_admission` kayit defterine (`LOCAL_EFFECT`) eklenmedi; kayit defteri
  bu isin dosya kapsami disindaydi.
- `target-reached` terminali native yoldan yazilmaz (bagimsiz kalite dogrulamasi ve Work gecisi
  ayri is).
- Canli provider/model qualification bu belgenin konusu degildir ve ayri exact yetki ister.
