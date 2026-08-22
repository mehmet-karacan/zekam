# Worker süreci

## Sohbetten bağımsız

Worker ayrı bir işlemdir. Zamanlama tanımlarını `ops.job_definition`'dan okur,
tetiklemeleri `ops.job_run`'a yazar ve `runtime.job` kuyruğundan iş alır. Süreç
yeniden başladığında durum veritabanından gelir; hiçbir şey bellekte tutulmaz.

```bash
zekam worker settings --json              # sinirlar, salt okunur
zekam worker tick --json                  # ne olacagini gosterir, hicbir sey yazmaz
zekam worker tick --uygula --json         # tek dongu calistirir
zekam worker run --uygula                 # uzun omurlu; Ctrl+C ile zarif kapanir
```

`tick` ve `run` mutasyon yaptığı için **açık `--uygula` bayrağı ister** — bu kural
`CANONICAL_COMMANDS` sözleşmesinde de kayıtlı ve `zekam surface check` ile
doğrulanıyor. Bayrak olmadan `tick` salt okunur bir plan üretir.

## Döngü sırası

```text
kapasite kontrolu -> zamanlama tetiklemeleri -> kuyruktan is alma -> isleme
```

1. **Kapasite**: kuyruk derinliği veya aktif worker sayısı sınırdaysa iş alınmaz
   (`BackpressureDecision`). Gerekçe döndürülür, sessizce beklenmez.
2. **Zamanlama**: zamanı gelen her tanım için idempotency anahtarıyla bir
   `job_run` açılır. Aynı pencere ikinci kez iş üretmez. `skip-visible`
   politikasında kaçırılan çalışma **olay olarak kaydedilir** ve
   `next_safe_action` taşır.
3. **Kuyruk**: `ExecutionHost.acquire_work` lease ve fencing ile iş alır; mantıksal
   kilitler edinilir, çakışma varsa iş bırakılır.
4. **İşleme**: iş türüne bağlı handler çalışır.

## Terminal durum kuralları

- **İşleyicisi olmayan iş `failed` olur** — sessiz başarı üretilmez.
- Handler hata fırlatırsa iş `failed` olur ve hata kategorisi `adapter` olarak
  sanitize edilir; ham hata metni saklanmaz.
- **Terminal receipt'i olmayan claim varsa `completed` reddedilir**
  (`ExecutionHost.finish`).
- **İptal edilen iş terminal sonuç yayımlayamaz**: `CancellationRequest`
  onaylanmışsa iş `abandoned` olur ve sonuç yayımlanması `PolicyViolation` üretir.

## Zarif kapanma

`ShutdownSignal` SIGINT ve SIGTERM'i bağlar. Worker mevcut işi yarıda bırakmaz;
o iş biter ve yeni döngü başlamaz. Sinyal ana iş parçacığı dışında bağlanamazsa
bu ölümcül değildir — worker `max_iterations` veya dışarıdan `request()` ile de
durdurulabilir.

## Varsayılan handler

`noop_handler` yan etkisi olmayan bir işleyicidir: işi güvenle tamamlar ve
sonucunu digest'ler. Gerçek işleyiciler bağlanana kadar bu kullanılır — sahte
başarı veya sessiz başarısızlık üretmez, yalnız "bu iş noop ile ele alındı"
bilgisini kanıtlanabilir biçimde kaydeder.

## Servis olarak çalıştırma

Worker uzun ömürlü bir süreçtir; systemd, Windows service veya container
supervisor altında çalıştırılabilir. Gereken tek dış girdi
`ZEKAM_DATABASE_PASSWORD` ortam değişkenidir.

```ini
# ornek systemd birimi
[Service]
Environment=ZEKAM_DATABASE_PASSWORD=...
ExecStart=/opt/zekam/.venv/bin/zekam worker run --uygula --etiket worker-1
Restart=always
KillSignal=SIGTERM
```
