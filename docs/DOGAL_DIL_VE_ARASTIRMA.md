# Dogal dil intake ve kanitli arastirma

## Intake

`zekam route preview "<dogal dil>" --json` provider veya source cagrisi yapmadan intent,
project family, repository role ve Jira route'unu deterministik olarak cozer. Karar authority
vermez; ham soru yerine query/policy digest'i tasir.

Reviewed aileler `config/project_families.yaml` icindedir. Tekil repository slug/alias kaydi
operational registry'de kalir. Ornegin `sky` ailesi `sky-spring-ui` (`ui`) ve
`sky-microservis` (`backend`) repository'lerini birlikte kapsar; UI/backend ipucu yoksa bilgi
sorusu iki hedefe fan-out edilir. Belirsiz coklu-repository mutation ise durur.
`GPU` ve `Gelir Paylasimi Uygulamasi` ayni `gpu` ailesidir. `SKY`,
`Satis Kanallari Yonetim`, `TTBP` ve `Turk Telekom Bayi Portali` ayni `sky` ailesidir.

Route `single-project-rag` dondururse
`zekam ask "<dogal dil>" --project <exact-project-ref> --authorize-remote-query` aktif hybrid
indeksi sorgular. `parallel-project-rag` kararinda coordinator ayni exact soruyu her hedefe
ayri yollar ve citation sonuclarini fan-in eder. `general` sorular project RAG'a dusmez.

Kurallar:

- Proje sinyali olmayan bilgi sorusu `general-research` olur.
- Kod/proje sinyali olup aile bulunamayan soru `clarification-required` olur.
- Exact identifier (`ZEKAM-P09-T01`, `#123`, `123 numarali defect`) metinden
  cikarilir ve semantic benzerlik bunu **degistiremez**. Kanonik kayitta olmayan
  kimlik `identifier-unknown` olarak gorunur kalir.
- `bunu`, `sunu`, `this` gibi isaret zamirleri yalniz **taze ve bounded** bir
  konuyla cozulur. Konu yoksa veya bayatsa konu uydurulmaz; `anaphora-unresolved`.
- Proje adaylari kanonik registry'den kurulur; token-boundary uygulanir ve ayni sorudaki
  farkli proje/aile sinyalleri sessizce elenmez.
- GPU/Gelir Paylasimi Uygulamasi sayisal Jira isi `SKYRSM`;
  SKY/TTBP/Satis Kanallari Yonetim sayisal Jira isi `TLCSKY` prefix'ine cozulur.
- Route `provider_calls = 0` ve `grants_authority = false` tasir.

Belirsizlik varsa CLI `5` (ambiguous) cikis kodu dondurur ve netlestirme sorusu
yazar. Belirsiz istek asla sessizce ise donusmez.

## Arastirma sorusu, scope ve butce

`ResearchQuestion` project, work ve intent scope'una baglanir. Source revision veya
intent digest degisirse soru **stale** olur ve yeniden yurutulmez; yeni revision
gerekir.

`SourcePolicy` hangi kaynak turlerine dokunulabilecegini sinirlar. HTTPS kaynagi
exact host allowlist olmadan **hic** etkinlestirilemez. `ResearchBudget` token,
maliyet, sure ve tur butcesini zorunlu kilar: en fazla iki tur ve 600 saniye.

## Source snapshot ve provenance

Her kaynak immutable snapshot olarak kaydedilir:

| Tur | Zorunlu alanlar |
|---|---|
| `file` | relative locator, content digest |
| `repository` | relative locator, revision, content digest |
| `https` | gecerli https URL, host, query string yok |
| `import` | locator, kaynak surumu |

Absolute path, traversal ve secret benzeri locator fail-closed reddedilir; ayni
kurallar veritabani check constraint'i olarak da uygulanir.

## Research DAG

Kanonik rol DAG'i:

```text
coordinator
  -> researcher | domain-reviewer | critic      (paralel)
       -> synthesizer
            -> citation-verifier
```

- **Koordinator subagent sayilmaz.** DAG'da gercek builder rolu yoksa
  `PolicyViolation`. Dispatcher koordinator node'unu hic cagirmaz.
- Bagimsiz ilk roller ayni paralel grupta yer alir (`parallel_width = 3`).
- Her child strict `RoleResult` envelope dondurur; free-text authoritative degildir.
- Dongulu DAG ve rol uyusmazligi reddedilir.

Bu tam DAG domain ve servis katmaninda uygulanmis ve test edilmistir. Windows'ta kullanilan
kanonik proje-research dikey dilimi daha dar bir gercek DAG calistirir: primary
`zekam-research-runner`, `zekam-researcher` ve ondan bagimsiz `zekam-verifier`. Verilen pinned
RAG evidence paketi disinda kaynak veya citation kullanamaz. Komut akisi:

```powershell
zekam research run "<soru>" --project <alias> --json
zekam research run "<soru>" --project <alias> --run-digest <digest> --uygula `
  --authorize-remote-query --authorize-agent-run --json
zekam research status <job-id> --json
zekam research report <job-id> --json
```

Ilk komut provider-free plandir. Apply tam plan digest'i ile bir remote RAG query islemi ve
en fazla uc model-agent cagrisi icin ayri iki authorization ister. Job, effect claim, receipt
ve terminal state mevcut operational SQLite otoritesinde tutulur. Claim olup receipt yoksa
silent retry yapilmaz; recovery gerekir. Basarili rapor proje altinda generated Markdown note
olarak materialize edilir ve note content digest'i terminal report digest'iyle capraz
dogrulanir. Ayni planin replay'i yeni provider veya agent cagrisi yapmaz.

## Celiski ve citation verifier

Celiskiler `compatible`, `scope-or-terminology`, `stale-source`, `evidence-gap`
veya `direct-contradiction` olarak siniflanir.

- **Direct contradiction yalniz verifier veya insan review ile cozulur.**
  Synthesizer'in "uzlasti" demesi cozum degildir.
- Citation verifier kimligi arastirmacilarla ayni olamaz — hem Python'da hem
  veritabani trigger'inda zorlanir.
- Dogrulanmamis veya reddedilmis bulgu rapora giremez.
- `partial`, `failed`, `blocked`, `abstained` ve `recovery-required` sonuclar
  fan-in tarafindan **yutulamaz**; raporda gorunur kalir.

## Rapor durumu

| Durum | Kosul |
|---|---|
| `answered` | dogrulanmis bulgu var, unresolved celiski ve non-success sonuc yok |
| `partial` | bulgu var ama unresolved celiski veya non-success sonuc var |
| `abstained` | dogrulanmis bulgu yok — kanit yetersiz, uydurma yok |

Answered raporun temizligi domain sozlesmesiyle zorlanir; bir rapor unresolved celiskiyi
gizleyerek `answered` olamaz. Markdown rapor Work veya authorization authority'si degildir.

## Markdown knowledge okuma yuzeyleri

Kanonik manifesti bulunan Markdown notlari su salt-okunur yuzeylerden okunur:

```powershell
zekam knowledge list [--project <alias>] [--work <uuid>] [--kind research] --json
zekam knowledge show <note-uuid-veya-portable-ref> --json
zekam knowledge search "<terimler>" [--project <alias>] [--work <uuid>] --json
zekam knowledge create <body.md> --title "<baslik>" [--project <alias>] [--work <uuid>] --json
zekam knowledge update <note-id> <body.md> --title "<baslik>" [--project <alias>] [--work <uuid>] --json
zekam knowledge archive <note-id> [--project <alias>] [--work <uuid>] --json
zekam knowledge restore <note-id> [--project <alias>] [--work <uuid>] --json
zekam knowledge mutation-status <job-id> --json
```

Varsayilan scope yalniz `global-user`dir; proje notlari global sorguya sizmaz. Her body read
symlink/reparse, ACL, boyut, strict UTF-8 ve manifest content digest kapilarindan gecer.
Arama en fazla 1000 aktif manifesti tarar ve en fazla 100 sonuc dondurur.
Create/update/archive/restore önce provider-free plan üretir. Uygulama exact `--plan-digest`
ve `--uygula` ister; update eski revizyonu silmez, doğrulanmış `supersedes` ilişkisi kurup
arsivler. Restore da arsiv kaydını değiştirmek yerine yeni bir active revizyon üretir.

## Plan candidate

Plan candidate yalniz `answered` rapordan turer (veritabani trigger'i). Uc bayrak
degismezdir:

```text
requires_authorization = true
approval_inherited     = false
grants_authority       = false
```

Arastirma bir plan **onerir**; uygulama yetkisi vermez. Mutation hala exact
authorization, plan drift kontrolu ve normal onay kapilarindan gecer.
