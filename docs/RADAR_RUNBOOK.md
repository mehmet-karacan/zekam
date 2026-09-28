# Zekam Research Radar Kullanim Rehberi

Bu dokuman, `zekam research radar` komut ailesinin salt okunur plan,
disiplinli calistirma, hata kurtarma, onbellek/koken ve yetki
sinirlarini aciklar. Rehber AKTIF_GOREV.md WP-06 kapsaminda
izlenmelidir.

## 1. Radar nedir?

Radar, uc organizasyonun (`anthropics`, `openai`, `google-gemini`)
acik kaynakli public repository'lerini Zekam'in kendi kaynak ve
degerlendirme standardiyla karsilastiran kanitli muhendislik
arastirma hattidir. Hedef "en yeni araci kopyalamak" degil,
Zekam'a uygun, kaynakta dogrulanmis ve bagimsiz verifier'dan
gecmis gelistirme adaylari uretmektir.

Radar iki asamali calisir:

| Asama | Amac | Ag cagrisi | Model cagrisi |
|---|---|---|---|
| `discover` | Public envanter ve secili repo/commit/path manifesti | evet | hayir |
| `analyse` | Cozumlenmis kaynaklar uzerinden kanitli degerlendirme | pin uzerinden | evet, sinirli |

`plan` komutlari her iki asama icin de salt okunurdur; asla ag veya
model cagrisi yapmaz.

## 2. Oncelikli okumalar

Calistirmadan once su dokumanlari okuyun:

- `00_BASLA.md` -- oturum baslatma ve gercek durum kurali.
- `DEVAM_PROTOKOLU.md` -- checkpoint, stale ve recovery.
- `AKTIF_GOREV.md` 13 -- radar komutlari ve kabul sinirlari.
- `docs/OTONOM_EVOLUTION_RUNBOOK.md` -- otomatik evrim,
  onay ve pause/resume davranisi.
- `GLOBAL_DEFINITION_OF_DONE.md` -- genel kabul sartlari.

## 3. Komut ornekleri

Asagidaki orneklerde `<project>`, `<digest>`, `<campaign-id>`
calistirma anindaki gercek degerlerdir; sahte fixture degildir.

### 3.1 Plan: discover

```bash
zekam research radar plan \
  --project <project> \
  --stage discover \
  --json
```

Cikti, yerel scope/policy ve sinirli public discovery planini icerir.
Plan canonical plan govdesi ve digest'ini stdout'a verir. Bu komut:

- ag cagrisi yapmaz,
- model cagrisi yapmaz,
- kalici mutasyon yapmaz,
- calisma rootu disindaki plan dosyalarini reddeder.

Ornek cikti ozeti:

```json
{
  "schema": "zekam-radar-plan/v1",
  "stage": "discover",
  "owners": ["anthropics", "openai", "google-gemini"],
  "max_request": 100,
  "max_response_bytes": 33554432,
  "deadline_seconds": 600,
  "plan_digest": "sha256:..."
}
```

### 3.2 Plan: analyse

```bash
zekam research radar plan \
  --project <project> \
  --stage analyse \
  --inventory-digest <digest> \
  --json
```

Analyse plani, daha once tamamlanmis ve dogrulanmis inventory
uzerinden secili kaynaklari ve sinirli analysis planini gosterir.
Eger inventory eksik veya digest uyusmazsa `needs-discovery`
doner; gizli ag fetch'i yapilmaz.

### 3.3 Run

```bash
zekam research radar run \
  --plan-file <artifact-json> \
  --plan-digest <digest> \
  --uygula
```

Run komutu:

1. Plan bytes ve digest'i yeniden dogrular.
2. Mevcut kaynaklari, config/policy ve managed prompt'lari
   yeniden dogrular.
3. Gerekli yetki secenekleriyle mevcut admission uzerinden
   stage'i calistirir.

Etkin yetki bayraklari:

- `--authorize-public-source-read` -- public kaynak okuma yetkisi,
- `--authorize-agent-run` -- ajan calistirma yetkisi.

Bu bayraklar dogrudan grant uretmez; trusted plan/ledger
kontrollerine baglanir. Plan icerisindeki self-declared approval
alanlari reddedilir.

### 3.4 Status

```bash
zekam research radar status \
  --campaign-id <id> \
  --json
```

Status, campaign'in stage, coverage, usage, blocked/recovery/terminal
ayrimini gosterir. Yan etki yapmaz.

Ornek cikti ozeti:

```json
{
  "schema": "zekam-radar-status/v1",
  "campaign_id": "<id>",
  "stage": "analyse",
  "state": "active",
  "coverage": {"answered": 7, "open": 3},
  "budget": {
    "reserved_requests": 80,
    "max_requests": 500,
    "reserved_seconds": 1200,
    "max_seconds": 7200
  },
  "blocked": [],
  "recovery": []
}
```

### 3.5 Report

```bash
zekam research radar report \
  --campaign-id <id> \
  --json
```

Report, kanonik kayittan okunabilir ve makine-okur bulgular ile
evidence manifesti uretir. Rapor source root'a yazilmaz; proje
kapsamindaki mevcut kullanici artifact alanina kaydedilir.

### 3.6 Candidates

```bash
zekam research radar candidates \
  --campaign-id <id> \
  --json
```

Candidates komutu secilen, reddedilen ve kismi adaylari listeler.
Bu komut aktif gorev veya onay uretmez; yalnizca oneri kaydini
sunar.

## 4. Hata ve kurtarma

### 4.1 Yaygin hatalar

| Hata | Muhtemel neden | Cozum |
|---|---|---|
| `needs-discovery` | Analyse plani icin gecerli inventory yok. | Once discover stage'i calistirin. |
| `stale-plan` | HEAD, config, policy veya managed prompt degismis. | Plani yeniden uretin; eski authorization yeni plana tasinmaz. |
| `plan-digest-mismatch` | Plan dosyasi degistirilmis. | Orijinal plan dosyasini veya yeni plan uretin. |
| `inventory-partial` | Rate-limit, 403, timeout veya erken durus. | Onceki tam gorunum korunur; tam tarama icin resume uygulanir. |
| `recovery-required` | Claim alindi, terminal receipt yok. | Uzlastirma yapilmadan sessiz retry veya terminal basari uretilmez. |

### 4.2 Recovery prensibi

Claim var ve terminal receipt yoksa durum `recovery-required`'dir.
Ayni provider cagrisi basari biliniyormus gibi tekrar edilmez.
Recovery:

1. Effect'in dis dunyada gerceklesip gerceklesmedigini adapter
   kanitiyla uzlastirir.
2. Kanit yoksa ayni plan tekrar yurutulmez.
3. Yeni, acikca gozden gecirilmis recovery plani uygulanir.

### 4.3 Timeout ve limit durumlari

- Sure/request/byte/call/token rezervasyon siniri asilirsa yeni
  is admission'i durur.
- `answered` uydurulmaz; kalan coverage checkpoint'e yazilir.
- Ayni digest/gap sonucunu tekrarlayan uc ardisik alt kosu
  `no-progress` olarak durur.

## 5. Onbellek ve koken (provenance)

### 5.1 Immutable blob onbellegi

Immutable blob'lar ve arastirma calisma ciktilari mevcut kullanici
artifact alaninda content-addressed saklanir. Zekam kaynak agacina
veya baska proje klasorune birakilmaz.

### 5.2 ETag ve 304 davranisi

- ETag/conditional request ile degismemis kaynaklarda pahali
  tekrar okuma engellenir.
- `304` yalnizca kayitli ve dogrulanmis ilgili cache girdisi varsa
  kullanilir.
- Bos veya bozuk cache'te basari sayilmaz.
- Fetch edilmeyen body'ye yeni icerik digest'i uydurulmaz.

### 5.3 Snapshot kimligi

Her kaynak su kimliklerle baglanir:

| Kimlik | Anlam |
|---|---|
| repository_id + commit_sha | Incelemenin exact upstream revision'i |
| path + git_blob_id | Git object provenance |
| raw_content_digest | Alinmis gercek dosya byte'larinin SHA-256 degeri |
| normalized_content_digest | Normalizasyon varsa ayri kimlik |
| slice_digest | Kaynakta iddia edilen exact kesit |
| delivered_payload_digest | Modele gercekten verilen serialization/kesit |

Line araliklari 1-based inclusive'tir. CRLF, BOM, Unicode ve cok
baytli karakterler deterministic digest ve dogru byte hesabi ile
islenir.

### 5.4 Cross-project izolasyonu

Iki project/realm ayni upstream'i incelese bile public blob
dedup mumkun olsa da private gap/candidate/receipt bilgisi sizmaz.
Cache restore baska realm/project'in private karsilastirmasini
acamaz.

## 6. Yetki ve onay sinirlari

Radar varsayilan olarak **oneri uretir**. Kaynak kodunu veya aktif
politikayi degistiren gelecekteki aday mevcut bagimsiz degerlendirme
ve gerekli onaylardan gecer.

### 6.1 Otomatik/read-only

- `plan`, `status`, `report`, `candidates` komutlari.
- Yerel snapshot/config okuma.
- Derived projection/rebuild (kullanici verisini degistirmedikce).

### 6.2 Exact one-shot approval

- Ag/provider cagrisi (`run --authorize-public-source-read`).
- Ajan calistirma (`run --authorize-agent-run`).
- Kullanici verisi/memory/skill mutasyonu.
- Entegre proje mutasyonu.
- Git commit/push.
- Yok edici/geri dondurulemez effect.

Kullanici "bu plani uygula" diyerek exact effect'i acikca
yetkilendirdiyse child step'lerde ayni sey tekrar tekrar
sorulmaz. Scope/source/policy drift olursa yeni onay gerekir.

## 7. Scheduler: varsayilan devre disi

Radar'in surekli tarama baglantisi mevcut scheduler/evolution'a
read-only proposal handler olarak baglanir. Varsayilan olarak
devre disidir.

- Install/enable/model/network effect yoktur.
- Yalnizca mevcut exact yetkili manuel runtime calisabilir.
- Daha once verilmis gecerli grant varsa yalnizca exact sinirinda
  kullanilir; sonradan discovery ile kapsam genisletilmez.
- Yeni daemon yazmak, OS schedule kurmak veya kullanici adina
duzenli ag/model taramasini etkinlestirmek kapsam disidir.

## 8. Kabul ve kalite kapilari

Radar kampanyasi asagidaki test/evidence kimlikleriyle degerlendirilir:

| ID | Kisa aciklama |
|---|---|
| A01-A15 | Alt ajan ciktisi, bounded transport ve deterministik dogrulama |
| A16-A24 | Public repo envanteri, snapshot, cache ve plan/status/report/candidates |
| A25-A32 | Kampanya sinirlari, concurrency, idempotency, resume ve recovery |
| A33-A43 | Aday secimi, evolution baglantisi, migration, wheel-only kurulum |
| A44-A46 | E2E offline corpus, eski replay ve aktif gorev uyumu |

Ayrintili kabul sartlari icin `AKTIF_GOREV.md` 15'e bakin.

## 9. Iletisim ve sonraki adimlar

- Hata raporlari icin `zekam work list`, `zekam work show <id>` ve
  `zekam run status <id>` ciktilarini kullanin.
- Stale durumda once `zekam doctor --json` calistirin; pending
  migration varsa `zekam doctor --hazirla --json` ile bounded
  uygulayin.
- Bir sonraki exact safe action status veya plan komutu ciktisinda
  belirtilir.
