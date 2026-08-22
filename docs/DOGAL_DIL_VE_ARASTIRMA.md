# Dogal dil intake ve kanitli arastirma

## Intake

`zekam ask "<dogal dil>"` salt okunurdur. Istegi `research`, `project-change`,
`status`, `idea` veya `ambiguous` olarak siniflandirir; hicbir kaydi degistirmez.

Kurallar:

- Niyet ipucu yoksa sinif tahmin edilmez; `no-intent-cue` belirsizligi uretilir.
- Iki farkli niyet ipucu varsa `multiple-intents` ile secim istenir.
- Exact identifier (`ZEKAM-P09-T01`, `#123`, `123 numarali defect`) metinden
  cikarilir ve semantic benzerlik bunu **degistiremez**. Kanonik kayitta olmayan
  kimlik `identifier-unknown` olarak gorunur kalir.
- `bunu`, `sunu`, `this` gibi isaret zamirleri yalniz **taze ve bounded** bir
  konuyla cozulur. Konu yoksa veya bayatsa konu uydurulmaz; `anaphora-unresolved`.
- Proje adaylari kanonik registry'den kurulur (exact id > exact alias > normalize
  alias). Iki aday varsa mutation baslamaz; `project-ambiguous` secim ister.
- Intake `grants_authority = false` tasir ve bu veritabani constraint'iyle zorlanir.

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

`zekam research dag --json` bu sozlesmeyi salt okunur gosterir.

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

Answered raporun temizligi veritabani check constraint'iyle de zorlanir; bir rapor
unresolved celiskiyi gizleyerek `answered` olamaz.

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
