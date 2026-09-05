# Yerel scheduler, worker ve raporlar

Mac yerel çekirdeği bir scheduler daemon'u kurmaz. Kalıcı iş, lease, outbox ve
recovery durumu `state/operational.db` içindedir. Dış zamanlayıcı yalnız aşağıdaki
bounded run-once komutlarını çağırabilir; bunlar PostgreSQL, Docker veya provider
bağlantısı kullanmaz.

```bash
zekam worker status --home <home>
zekam worker run-once --home <home>             # salt-okunur plan
zekam worker run-once --uygula --home <home>    # en fazla bir claim/effect
zekam worker reconcile --home <home>            # salt-okunur recovery plani
zekam worker reconcile --uygula --home <home>

zekam scheduler status --home <home>
zekam scheduler reconcile --home <home>
zekam scheduler reconcile --uygula --home <home>
zekam scheduler rebuild --home <home>            # salt-okunur plan
zekam scheduler rebuild --uygula --home <home>   # immutable segmentlerden rebuild
zekam scheduler report --home <home>             # CURRENT projection reconcile/read
```

`worker run-once` claim-before-effect ve terminal receipt sözleşmesini mevcut
yerel runtime servisi üzerinden korur. `reconcile` yalnız orphan/expired lease ve
belirsiz outbox durumlarını mevcut recovery kurallarıyla uzlaştırır. Analytics
`rebuild`, immutable raw segmentlerden yeni DuckDB generation, kaynak manifesti,
iki rapor ve rebuild receipt üretip `CURRENT` işaretçisini atomik yayımlar.
`report` bu zinciri tekrar doğrular; eksik, bozuk veya stale bir parçayı rapor diye
sunmaz. Bu projeksiyonların hiçbiri yürütme yetkisi vermez.
