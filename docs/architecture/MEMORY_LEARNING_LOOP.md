# Memory Learning Loop

Bu belge mevcut Memory Continuity Plane'in authority ve veri akisi sinirlarini
tanımlar. Yeni bir ikinci bellek sistemi tanimlamaz.

## Kanonik dongu

1. Istemci hook'u raw prompt/response/transcript yerine bounded lifecycle envelope
   uretir.
2. Immutable lifecycle ledger envelope'i PostgreSQL'e append eder. PostgreSQL tek
   authority'dir.
3. Durable compiler worker claim-before-effect ile bounded source watermark'ini
   isler. Bir model kullanilirsa model ciktisi yalniz `compiler_candidate` olur.
4. Candidate, compiler kimliginden bagimsiz review olmadan promote edilemez.
5. Promotion exact authorization ve terminal receipt ister; sessiz direct promotion
   yoktur.
6. Deterministik projection kanonik snapshot'i insan tarafindan okunur bir gorunume
   cevirir. Projection authority vermez ve geri okunmaz.
7. Hydration yalniz kanonik kayitlari ve current projection digest'lerini bounded
   context olarak secer. Required omission veya stale projection close/release'i
   fail-closed kapatir.

```text
hook -> immutable ledger -> durable compiler -> candidate
                                             -> review -> promotion
                                             -> projection -> hydration
```

## Degismez kurallar

- PostgreSQL tek kanonik authority'dir; Markdown ve Obsidian dosyalari salt okunur
  projection'dir.
- Remote call varsayilan kapalidir. Retrieval veya model sonucu mutation yetkisi
  vermez.
- Secret, PII, raw transcript ve diagnostic payload projection/Git'e cikamaz.
- Candidate body dogrudan aktif memory, skill veya decision olamaz.
- Her write exact plan digest, effect digest, scope, authorization,
  claim-before-effect ve terminal receipt zincirini korur.
- Projection CURRENT pointer'i exact project UUID, source snapshot ve policy
  digest'i ile ayni degilse durum `current` sayilmaz. Bos snapshot dahil proje
  kimligi projection digest, manifest ve receipt zincirinden cikarilamaz.

## High-risk alan doldurma

`FieldEvidence` bir alan degerini kaynak ref'i, kaynak digest'i, revision,
classification, confidence, validation rules ve expiry ile baglar. `unknown`,
`conflicting`, `expired` veya `prohibited` durumda normalize deger `None` kalir.
CAPTCHA, MFA, imza, odeme ve hukuki beyan alanlari manual-only'dir.

Preview mutation yapmaz ve raw degeri raporlamaz. Fill ve submit iki ayri effect'tir:
ayri plan/effect digest ve ayri exact authorization gerekir. Submit plani, ayni
preview icin tamamlanmis fill receipt digest'ine baglanmadan uretilemez. Canli
browser/provider adapter'i bu primitive'in parcasi degildir.

## Recovery siniri

Projection veya autofill servisinin cagirani runtime claim'i effect'ten once
acmalidir. Effect sonucu terminal receipt'e baglanamazsa ayni effect sessizce tekrar
edilmez; reconciliation, eldeki claim/checkpoint/evidence ile yapilir.
