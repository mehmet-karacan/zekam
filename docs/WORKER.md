# Yerel worker

## Work kaydini tekrar olusturmadan devam

Dogal dil Work durum raporu, toplam/state sayilarini SQL GROUP BY ile tum proje
icin hesaplar. Ayrinti penceresi proje basina en fazla 100 acik ve 20 kapali Work,
Work basina 20 event ve 5 run, run basina 20 step ve 5 checkpoint gosterir.
`open_count`/`counts` exact toplamdir; `*_truncated` alanlari ayrinti kesintisini
gosterir. Tam history yerine son event/run/checkpoint pencereleri okunur; mevcut
limitsiz public repository cagrilarinin davranisi korunur. Olculmeyen usage unknown
kalir, run butcesi harcama gibi raporlanmaz.

`work show|history|transition|close <reference> --project <project>` kayitli harici
is numarasini veya UUID'yi canonical Work'a cozer. Ayni numara birden fazla projede
varsa proje secilmeden devam edilmez. UUID bile verilse explicit proje siniri
korunur; numara prefix/substring eslestirmesi yapilmaz. Native ajan kullanicinin
proje ve is numarasini bu girise tasiyabilir; revision/evidence kosullari devam eder.

Native istemci ayni create istegini tekrar gonderecekse `work create <project> <title>
--key <request-key> --uygula --json` kullanir. Key proje kapsamindadir; ayni key ve
exact ilk create icerigi mevcut Work'un guncel revision/state'ini dondurur. Key ayni
kalirken title/type/payload/ilk state/evidence/harici numara degisirse istek reddedilir.
Yeni Work veya ikinci created event uretilmez. Key verilmezse mevcut create davranisi
korunur. Plaintext key operational kayda yazilmaz; mevcut Work PK kimligi ve ilk
immutable revision kullanilir. Ayri schema veya migration gerekmez.

`work resume --project <project> --json` proje resolver'i ve SQL LIMIT oncesi
OPEN_STATES filtresiyle devam penceresini okur. Bu komut lease veya execution izni
uretmez. Bu iki gelistirmenin regresyon kodu hazirdir; testler tum gelistirmeler
sonrasi tek final asamada calistirilacaktir.

Worker, fresh Mac yerel çekirdeğinin `state/operational.db` kuyruğu üzerinde
bounded run-once olarak çalışır. Ayrı bir daemon, PostgreSQL bağlantısı, DB
parolası, systemd birimi veya uzun ömürlü `run` komutu yoktur.

```bash
zekam worker status --home <home>
zekam worker run-once --home <home>
zekam worker run-once --uygula --home <home>
zekam worker reconcile --home <home>
zekam worker reconcile --uygula --home <home>
```

`status` yalnız ready/running/recovery-required/quarantined iş, outbox ve açık
recovery-case sayılarını okur. `run-once` bayraksızken bu durumu içeren salt-okunur
bir plan döndürür. `--uygula` verildiğinde startup recovery yapar ve en fazla bir
ready yerel işi mevcut `LocalRuntimeService` üzerinden claim eder. PID/process
incarnation, lease, fencing, claim-before-effect ve terminal receipt kuralları
aynı SQLite transaction sözleşmelerinden gelir; CLI bunları uydurmaz.

`reconcile` bayraksızken salt-okunur recovery planıdır. `--uygula`, yalnız mevcut
orphan/expired lease ve belirsiz outbox kayıtlarını kanonik recovery kurallarıyla
uzlaştırır. Ham payload, owner token veya secret yazdırılmaz. Süreç yeniden
başlatıldığında tüm durum aynı yerel veritabanından yeniden okunur.

Effect veya outbox publisher uzun surdugunde servis, SQLite write transaction'i
acmadan claim lease'ini ayri baglantidan yeniler. Yenileme ayni owner/fencing
tuple'ini zorunlu tutar; heartbeat kaybi sonucu basari/teslim iddiasi uretilmez,
etki bilinmeyen receipt ve recovery-required yoluna gider. Outbox heartbeat'i
expiry alanini dondurur, fencing sayacini degistirmez ve eski claim replay'ini
reddeder.
