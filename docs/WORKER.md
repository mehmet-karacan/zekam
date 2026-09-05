# Yerel worker

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
