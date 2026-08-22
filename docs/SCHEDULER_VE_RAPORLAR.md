# Scheduler, gelen belgeler ve günlük rapor

## Sohbetten bağımsız

Zamanlama tanımı kalıcıdır (`ops.job_definition`) ve sohbet sürecine bağlı
değildir. Süreç yeniden başladığında durum veritabanından okunur: duraklatılmış
iş duraklatılmış, iptal edilmiş iş iptal edilmiş kalır.

## Tanımları oluştur

```bash
zekam scheduler init                 # dry-run: neyin tanimlanacagini yazar
zekam scheduler init --uygula        # 12 zorunlu bakim isini tanimlar
```

`init` idempotenttir ve **var olan tanımı ezmez**: aralığı elle değiştirdiyseniz
öyle kalır. Mutasyon yaptığı için açık `--uygula` bayrağı ister; bayraksız hâli
hiçbir şey yazmaz. Bakım işleri tanımlanmadan `zekam doctor` `runtime.scheduler`
için `degraded` döner ve worker o işleri çalıştıramaz.

## Tetikleme kararı

```bash
zekam scheduler list --json          # tanimlar + eksik zorunlu isler
zekam scheduler required --json      # kanonik bakim isleri
zekam scheduler plan <is> --json     # salt okunur tetikleme hesabi
```

- **Aralık** açık ve deterministiktir (`5m`, `6h`, `1d`) — cron ifadesi yerine
  test edilebilir bir aralık kullanılır.
- **Idempotency**: anahtar iş adı + planlanan an (UTC) + payload digest'inden
  türer. Aynı tetikleme iki kez iş üretmez; kural veritabanında da unique'tir.
  Aynı mutlak an farklı timezone gösterimiyle verilse bile anahtar aynıdır.
- **Misfire**: `run-once` kaçırılan çalışmalar için tek telafi çalıştırır;
  `skip-visible` atlar ama **kaç çalışma kaçırıldığını raporlar** — sessizce
  yutulmaz.
- **Overlap**: `skip` önceki çalışma sürerken atlar; `queue` sıraya alır. Bir
  tanımın aynı anda tek aktif çalışması olabilir (partial unique index).

Duraklat / devam ettir / iptal et akışı durum geçişlerini korur: iptal edilmiş iş
duraklatılamaz, aktif iş "devam ettirilemez".

## Gelen belgeler

| Karar | Koşul |
|---|---|
| `unstable` | dosya hâlâ yazılıyor (son değişiklikten beri < 5 sn) |
| `duplicate` | aynı içerik digest'i daha önce işlendi |
| `choice-required` | birden fazla hedef eşleşti — **tahmin edilmez, seçim istenir** |
| `accepted` | tek hedef eşleşti |
| `rejected` | uygun hedef yok |

Belge yolu portable olmak zorundadır: absolute path, ters bölü ve traversal hem
alanda hem check constraint'inde reddedilir. Boş dosya yönlendirilmez.

## Gece işleri

`NightBudget` token, maliyet ve süre sınırı ister. **Kota bilinmiyorsa iş
çalışmaz** — kalan oran tahmin edilmez. Kota tabanının altındaysa da çalışmaz.

## Günlük rapor

On bölüm zorunludur; eksikse rapor üretilmiş sayılmaz (alan + `report_required_sections`
constraint'i):

`tamamlanan-isler`, `aktif-lease-ve-recovery`, `subagent-model-dagilimi`,
`okunan-kaynaklar`, `token-cost-latency-quota`, `model-health-benchmark`,
`memory-skill-adaylari`, `retrieval-index-sorunlari`, `security-policy-olaylari`,
`onerilen-next-actions`.

Boş bölüm "kayıt yok" yazar — sessizce atlanmaz. Rapor `grants_authority = false`
taşır. Aynı gün ve aynı kapsam için ikinci rapor üretilmez.

```bash
zekam report sections --json
zekam report today --kapsam genel --json
```

`report today` rapor **üretmez**, kanonik kayıttan okur; rapor yoksa çıkış kodu 4.

## Olaylar ve runbook

`ops.scheduler_incident` her olay için `next_safe_action` ister — boş bırakılamaz.
Olay türleri: `misfire`, `overlap`, `failure`, `recovery-required`.
