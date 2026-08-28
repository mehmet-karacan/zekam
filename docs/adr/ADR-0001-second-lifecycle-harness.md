# ADR-0001: Ikinci gercek lifecycle harness olarak Codex command hooks

- Durum: Kabul edildi; test ve verifier kaniti bekliyor
- Tarih: 2026-08-28
- Kapsam: Codex CLI 0.150.1, Windows x86_64

## Baglam

OpenCode plugin E2E mevcut ikinci bir taklit istemci degildir. Genel subprocess
adapterinin uydurdugu `--assignment-id`, `--role`, `--instruction-digest` ve
`--context-digest` bayraklari Codex CLI yuzeyi degildir ve lifecycle kaniti
sayilamaz. Ikinci harness kurulu istemcinin gercek, belgelenmis lifecycle
yuzeyini kullanmalidir.

Codex 0.150.1 command hooks su olaylari gercekten sunar: `SessionStart`, `Stop`,
`PreCompact`, `PostCompact` ve `SessionEnd`. Her command hook stdin uzerinden
JSON alir. `Stop` bir continuation karari verebilir; `PreCompact` ise
`continue: false` ile compaction'i durdurabilir. Windows komut farki icin
belgelenmis alan `commandWindows`'tur.

## Karar

Ikinci harness Codex command hooks olacaktir. Exact contract
`config/client-lifecycle/codex-0.150.1.json` ile surumlenir. Kurulu surum exact
`0.150.1` degilse adapter `lifecycle-events-v2` yetenegi ilan etmez ve hook
girdisi reddedilir. Baska bir surume benzerlik yoluyla uyarlama yapilmaz.

Kayipsiz allowlist mapping:

| Codex raw event | Canonical continuity event | Semantik |
| --- | --- | --- |
| `SessionStart` | `session_start` | oturum baslangici/resume |
| `PreCompact` | `pre_compaction` | compaction oncesi gate |
| `PostCompact` | `post_compaction` | compaction sonrasi gozlem |
| `Stop` | `pre_close` | kapanis/freshness gate noktasi |
| `SessionEnd` | `post_close` | en fazla 3 saniyelik advisory gozlem |

Raw event adi ve canonical event tipi ayni immutable spool entry'sinde,
observation digest ve entry digest altinda birlikte tutulur. Bilinmeyen bir
PascalCase olay normalize edilmez; fail-closed reddedilir. Generic bridge'in
external-event regex'i bu kararla genisletilmez.

PostgreSQL ingress mevcut ortak lifecycle ledger vocabulary'sini korur:
`session_start -> session.created`, `pre_compaction -> session.compacting`,
`post_compaction -> session.compacted`, `pre_close -> session.status` ve
`post_close -> session.deleted`. Bu mapping 0046 compiler outbox trigger
vocabulary'siyle uyumludur; ancak Codex'in ayni durable compiler yoluna gercekten
girdigi iddiasi production adapter ve PostgreSQL E2E gecmeden acceptance kaniti
sayilmaz. Raw Codex adi spool provenance'inda kaybolmaz.

## Icerik ve authority siniri

Hook adapteri yalniz su yapisal alanlari kabul eder: session/turn kimlikleri,
exact event adi, belgelenmis enum degerleri, `stop_hook_active` ve permission
mode. Session, turn ve yerel occurrence kimlikleri lowercase UUID olmak
zorundadir. `SessionEnd.reason` yalniz resmi `clear`, `logout`,
`prompt_input_exit`, `other` allowlist'inden gelir. `transcript_path`, `cwd`,
`model`, prompt, response,
`last_assistant_message`, tool input/output ve benzeri alanlar:

1. kalici kayda alinmaz,
2. hash'e katilmaz,
3. stdout'a veya hata mesaji icine yazilmaz.

Bu nedenle dusuk entropili gizli icerik bir digest oracle yoluyla da sizmaz.
Hook sonucu authority, authorization, lease, claim veya canonical completion
uretmez. PostgreSQL tek otoritedir; yerel spool yalniz durable pending evidence
tasir.

## Dayaniklilik ve replay

Hook, PostgreSQL ya da provider beklemeden
`ZEKAM_HOME/global/runtime/client-lifecycle/codex` altina yazar:

- event dosyalari append-only ve per-session hash-chain'dir,
- hook append'i tum event gecmisini taramaz; exact delivery ref ve en fazla iki
  entry'lik per-session tail checkpoint'ini dogrular,
- pending/committed checkpoint yazimi yarida kalirsa sonraki exact hook ayni
  content-free entry/ref'i tamamlar; baska bir payload'a sessiz gecmez,
- ayni delivery ayni observation ile idempotenttir,
- ayni delivery farkli observation ile replay drift olarak reddedilir,
- her append immutable global queue ref'i uretir; pending selection derived
  contiguous resolved cursor'dan en fazla 256 exact queue ref'i okur ve
  event/ACK gecmisini taramaz; mutation yolunda caller-controlled sequence
  atlamasi yoktur,
- cursor ilerlemesi immutable previous-cursor zinciri ile queue ref, source
  entry, delivery ref ve exact terminal attempt-state'i birlikte dogrular;
  `acknowledged` kaydi ayrica generic ACK ile terminal continuity binding
  parity'sini, `manual-review` kaydi ise ACK alanlarinin boslugunu kanitlar;
  derived pointer tek basina completion kaniti degildir,
- canonical drain source event'i silmez; ayri immutable ACK ekler,
- ayni poison failure digest'i idempotent attempt replay'idir; farkli failure
  evidence'i en fazla uc kez sayilir ve sonra `manual-review` olur; `completed`
  ve `manual-review` terminal attempt-state sonradan sessizce degistirilemez;
  manual-review session predecessor'ina bagli bir ardil ayrica exact predecessor
  state digest'iyle terminal recovery gereksinimine alinir ve farkli session
  kayitlarini bloke etmez,
- status event/ACK dizinlerini taramaz; immutable queue index uzerinden en fazla
  256 kayitlik sayfalar ve `next_after_sequence` ile history sunar.

Spool root, parent chain, target, lock, ACK ve cursor yollarinda symlink ve
Windows reparse point fail-closed reddedilir. Acilan nesne regular file, bounded
boyut ve pre/post-open identity ile dogrulanir; platform sunuyorsa `O_NOFOLLOW`
kullanilir. Immutable/atomic yazimda file handle `fsync` edilir. POSIX'te parent
directory de `fsync` edilir. CPython Windows no-follow directory handle'i
sunmadigi icin directory fsync kanitlanmis degildir; Windows garantisi flushed
file handle ve atomic link/replace ile sinirlidir.

Codex hook wire formu delivery id vermez. Adapter her hook invocation'i icin
icerik tasimayan random bir occurrence id uretir ve bunu reviewed wire digest
ile delivery digest'e baglar. Boylece ayni session'in ikinci resume/end olayi
sessizce birinciyle birlesmez. Occurrence-bound delivery digest spool'a
girdikten sonraki worker replay'lerinde aynen kullanilir; canonical idempotency
korunur.

Hook komutu DB, model veya provider cagrisi yapmaz. Daha sonraki explicit
`zekam client drain --uygula`, production continuity adapteri compose edilene
kadar spool nesnesi, client-instance dosyasi veya `RealmSession` olusturmadan
fail-closed olur. Drain API generic repository'yi dogrudan kabul etmez. Rev9
adapteri read-only preflight, generic ingest + continuity admission'i kapsayan
tek transaction apply ve ayri read-only terminal lookup saglamak zorundadir.
Ilk ingest ile lookup'in `event_id` ve canonical ACK digest'i ayni degilse yerel
pending dusmez. `PreCompact` icin iki okuma ayrica 0046 trigger'inin exact aktif
execution run/job/lease/plan bagindan urettigi compaction outbox id ve payload
digest'inde eslesmelidir; bu runtime binding yoksa yerel pending dusmez. Public,
keyfi digest kabul eden `client ack` komutu yoktur. Iki lookup eslesse bile
yerel ACK ancak exact realm/project/work/run, authorization/job/claim/effect
receipt, continuity event/outbox ve terminal receipt digest'lerini iceren
immutable continuity binding ile yazilir.
`PreCompact` binding'i ayrica `compiler_enqueue=true` olmak zorundadir.
Apply sonucu ile read-only lookup exact receipt olarak ayni degilse pending
dusmez; generic event admission'dan once ayri commit edilemez.
Basarisiz veya beklenmeyen exception yalniz sanitize kategori digest'li immutable
attempt uretir; exception metni, path veya payload kalicilasmaz ve otomatik retry
yoktur. Bu revision continuity adapterinin normal project/policy/authorization/
claim composition'ini uydurmaz: adapter henuz compose edilmedigi icin apply yuzu
fail-closed kalir. Exact HookSession/session binding, authorization ve claim
composition'i tamamlanmadan spool pending dusmez. Local ACK authority degildir.

Hook deployment'i `python -m zekam.interfaces.cli.client hook ...` hafif
entrypoint'ini ve Windows'ta exact interpreter path'i `commandWindows` ile
kullanir. Bu yol genel `zekam` CLI composition'ini yuklemez; `SessionEnd` icin
belgelenmis uc saniyelik butceyi korur. Manuel status, pending ve dry-run drain
komutlari ana `zekam client` agacinda kayitlidir. Rev9 adapteri gelene kadar
`drain --uygula` da mutation yapmaz ve explicit fail-closed sonuc verir.

`PreCompact` spool basarisizliginda adapter `{"continue": false}` doner.
`Stop` ve diger sync hook basarisizliklarinda Codex'in belgelenmis hook rejection
cikis kodu `2` kullanilir; mesaj yalniz sanitize recovery kategorisidir.
Basarili hook stdout'u bos JSON object'tir. `SessionEnd` synchronous ve advisory
kalir; timeout 3 saniyeyi gecmez.

## Gercek binary E2E

E2E testi fake client kullanmaz. PATH'teki `codex` binarysini calistirir ve
once exact `codex-cli 0.150.1` cikisini dogrular. Testin izolasyonu:

1. gecici, bos `CODEX_HOME` ve hooks.json,
2. hooks feature acik ve bes allowlisted command hook,
3. resmi custom model-provider ayarlariyla yalniz `127.0.0.1` Responses SSE stub,
4. `requires_openai_auth = false`, bos auth/API-key ortami,
5. telemetry exporter kapali, MCP tanimi yok, web search yok,
6. HTTP/HTTPS/ALL proxy icin ayri loopback deny endpoint ve localhost NO_PROXY,
7. yalniz testte `--dangerously-bypass-hook-trust`,
8. read-only sandbox ve `approval_policy = "never"`.

Test loopback Responses endpoint'i disinda gozlenen bir istek veya deny proxyye
bir istek gelirse basarisiz olur. `codex exec --json` stream'inde terminal turn
sonucu ve spool'da en az `SessionStart`, `Stop`, `SessionEnd` raw/canonical
eslesmeleri dogrulanir. Ayni E2E dosyasi gercek hafif hook entrypoint'ine resmi
sekilli `PreCompact` ve `PostCompact` JSON'larini ayri subprocess invocation'i
ile verir ve iki yonlu mapping/chain parity'sini dogrular. Bu ikinci kisim Codex
binary'sinin test kosusunda kendiliginden compaction urettigi iddiasi degildir.
Spool belgelerinde test prompt'u, model cevabi, transcript veya absolute path
bulunmamasi ayrica kontrol edilir.

Bu E2E external provider kullanmaz; Responses hedefi local loopback stub'dir.
Ancak bu bir firewall, network namespace veya kernel egress-deny kaniti degildir.
Yalniz provider route'u ve proxy'ye uyan HTTP(S) cikislari gozlenir. Direct socket,
proxy'yi atlayan protokol veya kernel seviyesinde dis baglanti yoklugu bu testle
kanitlanmis sayilmaz. CI agent seviyesinde gercek egress deny varsa ayrica o
ortamin kaniti alinabilir.

## Sonuclar

- OpenCode ve Codex iki farkli gercek lifecycle runtime'i olur.
- Generic dispatch bayrak taklidi lifecycle kaniti olarak kullanilmaz.
- Surum veya event drift'i sessiz downgrade yerine gorunur failure uretir.
- Yerel kesinti canonical authority'yi degistirmez ve sessiz retry yaratmaz.
- Close/release freshness karari yerel hook output'una devredilmez; canonical
  worker/bridge checkpoint ve projection freshness kanitini uretmeden release
  tamamlayamaz.

## Cross-harness parity testinin siniri

`tests/e2e/test_cross_harness_memory_continuity.py`, iki gercek parser/spool
observation'inin ortak domain orchestrator, hydration sinifi ve deterministic
candidate compiler invariants'ini in-memory kanitlar. Bu test canonical
PostgreSQL admission, `runtime.execution_run`, terminal lifecycle outbox veya
claim kaniti degildir. Bu nedenle test adi E2E olsa da DB parity acceptance'i
ancak production continuity admission adapteri ve PostgreSQL integration testi
eklendiginde kapanir; mevcut test tek basina completion evidence sayilmaz.

## Kaynaklar

- <https://developers.openai.com/codex/hooks>
- <https://developers.openai.com/codex/config-reference>
- <https://developers.openai.com/codex/noninteractive>
- <https://developers.openai.com/codex/app-server>
