# Zekam otonom evolution isletim runbook'u

Bu belge kodun varligi ile bu cihazdaki canli yetkiyi ayirir. Markdown work, grant veya
receipt authority'si degildir.

## Guvenli durum okuma

```powershell
zekam doctor --json
zekam local-core status
zekam evolve status --json
zekam evolve candidates --json
zekam evolve report --json
zekam evolve supervisor-status
```

`local-core all_ready=true`, tum urunun veya supervisor'un etkin oldugu anlamina gelmez.
`evolve status` icindeki grant, scheduler, recovery ve native kabul alanlari birlikte
degerlendirilir. Eski PostgreSQL donemi raporlari guncel kabul kaniti degildir.
Plan, status, report, doctor ve observatory ayni fail-closed effective-state onceligini
kullanir: recovery, pause/disable, blocker, drift, setup gap ve ancak sonra
observing/running. Rapor; before/after evaluation baglarini, gercek operational
usage/maliyet, reddedilen/geri alinan veya recovery edilen sayilari ve bounded capture-gap
sayisini kanonik kaynaktan gosterir.

## Windows supervisor

Once `zekam evolve bootstrap-plan` ile v3->v5 backup/migration, eksik project-realm binding,
sifir provider/token butceli yerel standing grant ve Windows supervisor tek exact plan olarak
gorulur. Kurulum ancak bu digest ayri olarak yetkilendirildikten sonra
`zekam evolve enable --uygula --plan-digest <digest>` ile yapilir. Apply, etkiden once exact
plani owner-private immutable intent olarak kaydeder ve append-only evolution control ledger'ina
ayni digest ile `bootstrap-claim` yazar. Migration sonrasi veya process kesintisi sonrasinda ayni
digest ile gercek DB/task readback'inden idempotent devam eder; tum etkiler dogrulaninca ayni
ledger `bootstrap-complete` terminalini kaydeder. V3 backup writer oncesi kalmissa yalniz ACL,
schema, logical ve original-row digest'leri exact kaynakla eslesiyorsa yeniden kullanilir. Dusuk seviye
yalniz-scheduler yuzeyleri `supervisor-plan` ve `supervisor-install` komutlaridir. Readback exact
task adi, action, principal/logon, repetition, multiple-instance ve missed-start ayarlarini
dogrular. Windows'un username->SID ve timezone gosterim normalizasyonu yalniz digest-bound SID
ve ayni UTC instant icin kabul edilir; pil/idle/unified davranislari acik plan alanlaridir.
Fazladan action, trigger, principal, ayar veya capability drift'tir ve overwrite edilmez.
Bu gorevin testleri adapter'i dogrular; Task Scheduler kaydi kurulmadikca native
installed denmez.

Native kabul, son `bootstrap-complete` olayindan sonraki iki ardisik bes dakikalik Windows
Task Scheduler EventID 201 basarisini operational DB'deki exact slot -> job -> tek effect claim
-> tek completed receipt zinciriyle esler. `native_acceptance.verified=true`, bos `setup_gaps`,
`supervisor.state=matching`, `active_grants=1` ve `drifted_grants=0` birlikte gorulmeden OE-08
kapanmis sayilmaz. Olay okuyucu provider veya ag cagrisi yapmaz.

`zekam evolve pause --uygula` yeni tick admission'ini keser;
`zekam evolve resume --uygula` yalniz paused durumu acar; `zekam evolve disable --uygula`
kalici olarak etkileri kapatir ve exact enable plani olmadan yeniden acilmaz.
Duraklatma yeni admission'i kesmeli, calisan isi safe checkpoint/recovery'ye goturmelidir.
Devam, grant expiry/revocation, recovery, supervisor, config, source ve implementation
manifest drift'ini yeniden dogrulamadan yapilmaz; admission evidence append-only control
olayina baglanir. Disable, managed supervisor tick etkilerini kalici olarak devreden
cikarir; schedule kaydi uyanmaya devam edebilse bile yeni claim/effect kabul edilmez.
Knowledge, SQLite ledger, receipt veya kullanici verisi silinmez.

## Rollout ve recovery

Sirasi shadow, local fixture canary, exact-scope activation ve gerekirse rollback'tir.
Production trafik kabul edilmediyse rapor `production_traffic=false` kalir. Selector effect'i
ile terminal settlement arasinda kesinti olursa ayni plan tekrar calistirilmaz. Durable
evidence ve evolution terminali okunur. Completed terminal varsa pointer'a dokunulmadan yalniz
prepared settlement finalize edilir. Terminal yoksa activation exact onceki digest'e CAS
recovery uygular; failed recovery idempotent settle edilir, unknown terminal insan
adjudication'i ister. Kullanici veya baska writer selector'u degistirmisse recovery durur.

Prepared receipt, evolution terminal ve improvement settlement farkli ledger'larda append-only
izlenir. Finalized veya recovered kayit live pending sayilmaz. Backup/restore icin mevcut
`zekam backup` yuzeyi kullanilir; canli SQLite dosyasi siradan dosya kopyasi ile tasinmaz.

## Model ve ag yoklugu

Capture, queue, deterministic compiler, maintenance ve local fixture testleri modelsiz
calisabilir. Ozetleme veya semantik evaluation icin yetkili model yoksa sahte sonuc uretilmez;
ilgili is `blocked` veya `degraded` kalir. Standing grant, benchmark kampanyasi veya uzak
provider izni vermez.

## Platform durumu

Bu teslim Windows'ta dogrulanir. macOS adapter kodu korunur fakat bu Windows oturumunda native
macOS kabul sonucu uretilmez. Iki cihaz ayni writable scope icin bagimsiz auto-writer olamaz.
