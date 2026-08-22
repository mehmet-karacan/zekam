# Model benchmark ve routing

Zekam model benchmark katmani ham prompt/yanit yerine surumlu fixture metadata'si,
metric ve SHA-256 provenance saklar. `config/model_benchmark_fixtures.yaml` genel
registry'dir; her case secret-free logical relative `fixture_source` ile local-only
veya remote-allowed olarak acikca isaretlidir. Absolute path, traversal, endpoint ve
secret benzeri metadata fail-closed reddedilir.
Project micro suite, project ID ve kanonik capability profile digest'ine baglanir.

Benchmark planlari inventory, suite ve policy digest'lerine baglidir ve en az bes
repetition ister. Ayni plan digest'i ikinci kez hazirlandiginda mevcut plan dondurulur.
Her provider trial'i mevcut bir Effect Claim ve terminal receipt'e baglanmadan
kaydedilemez; bu nedenle replay yeni provider maliyeti yaratmaz. Aggregate kalite,
guvenilirlik, latency, token ve verifier-approved cost icin mean, median, p95 ve
variance uretir. Tek unsafe trial sonucu fail eder. Tested model ile independent
verifier identity ayni olamaz.

`zekam model benchmark --json` registry contract'ini salt okunur gosterir. Model,
inventory ve policy digest'leri verildiginde digest-bound plan hazirlar. CLI
`--uygula`, exact authorization ve runtime claim gateway baglanmadan fail-closed
reddedilir. Provider execution, claim-before-adapter-call ve exact terminal receipt
yolundan gelir; ayni fixture/repetition replay'i adapter'i tekrar cagiramaz.

`zekam model decide --girdi karar.json --json` yalniz karar gereksinimlerini kabul eder;
CLI'dan aday, hard-gate boolean'i veya kota evidence'i kabul etmez. Adaylar kanonik
inventory, benchmark, quota-pool binding ve runtime observation ledger'larindan
kurulur. Butun red nedenleri ve evidence digest'leri karar digest'ine baglanir.
Qualified adaylar quality, reliability, project specialization, observed success,
latency, token efficiency, cost ve human correction ile puanlanir. Karar authority
veya mutation approval degildir.

## Gercek saglayici tasimasi

`src/zekam/application/provider_adapter.py`, gercek model endpoint'ine giden yolun
provider-neutral guvenlik siniridir. Endpoint URL envantere veya veritabanina
yazilmaz; logical `endpoint_ref + operation` anahtari process belleginde bir ortam
degiskeni locator'ina cozulur. Uzak hedef HTTPS olmak zorundadir; duz HTTP yalniz
loopback icin kabul edilir. Userinfo, query, fragment ve redirect reddedilir.

Her JSON veya multipart cagrisi su sirayi izler:

1. payload icerigi yerine SHA-256 digest'li outbound request hazirlanir,
2. provider, endpoint, operation ve veri sinifi exact authorization ile eslestirilir,
3. credential Secret Broker'dan yalniz cagri context'i icinde cozulur,
4. authorization transport'tan hemen once tek kullanimlik tuketilir,
5. response icerigi yerine response digest'i ve terminal outbound state kaydedilir.

Transport Python stdlib kullanir ve response boyutunu sinirlar. Whisper ses
girdisi deterministic multipart body ile gider; filename/header injection ve
desteklenmeyen media type reddedilir. Unit/security testleri bellek ici transport
ile tamamen cevrimdisidir. Chat/code, embedding, transcription, rerank, guardrail
ve VL response sekilleri fail-closed normalize edilir.

`config/model_provider_bindings.yaml` yedi modalite icin model secimini, logical
endpoint/credential ref'lerini, ortam locator adlarini, operation ve relative path
hint'lerini exact olarak sabitler. `zekam model provider-config --json` bu dosyayi
kanonik inventory ve SecretRef metadata'siyle karsilastirir; endpoint URL'lerini veya
credential degerlerini raporlamaz ve hicbir ag/provider cagrisi yapmaz.

`config/model_provider_contract_fixtures.yaml` yalniz public veri tasir. Chat JSON
shape, code marker, Turkce Whisper reference, uc embedding tekrari ve batch, uretilmis
red-square/blue-circle VL gorseli, rerank sirasi ve dengeli safe/unsafe guardrail
ornekleri fixture digest'ine baglidir. `zekam model provider-plan --json` bunlardan
7 exact hedef ve 10 ayri cagri plani uretir. Her cagri farkli plan digest'i, exact
effect/resource/provider/operation kapsami ve `max_uses: 1` tasir; bu belge authority
veya authorization uretmez.

Provider policy adayi yalniz manifestteki yedi hedefi acar; network default-deny ve
push default-deny korunur. Aday dry-run sirasinda kalici policy olmaz. Canli son kapida
policy drift tekrar kontrol edilir, aday kalici hale getirilir ve her cagri icin
authorization just-in-time uretilip transport'tan hemen once tuketilir.

2026-08-21 son dry-run sonucu inventory ve SecretRef metadata eslesmesi 7/7,
calistirilabilirlik 0/7'dir: yedi metadata kaydi vardir fakat ortamda endpoint/
credential degerleri ve Whisper public fixture locator'i yoktur. Bu nedenle gercek
provider yolu fail-closed kalir. Bu degerler hazirlanip son canli contract kapisi
calistirilmadan `ZEKAM-DOD-025` kapanmaz.

Kota provider markasi degil execution path havuzudur. Yalniz trusted observation
Codex `%40` ve Claude `%30` fallback esiklerini etkinlestirir. Observation yoksa veya
`unknown` ise oran tahmin edilmez. Bounded deliberation en fazla iki tur/on dakika;
acik token, cost ve evidence budget'i ister. Celiski verifier ya da insan review'a
aktarilir ve deliberation authority uretmez.
