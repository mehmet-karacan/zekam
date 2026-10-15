# Canonical Work bagimliliklari (operational v8)

`work relate <source> <target> --project <project> --revision N` source Work'un
target Work tamamlanmadan yurutulmemesi icin bagimlilik plani verir. Referans UUID
veya kayitli exact harici numaradir. Uygulama ayni komuta `--uygula --plan-digest
<dry-run digest>` eklenerek yapilir. `--remove` ayni exact plan/revision sinirinda
bagimliligi kaldirir; tarihsel olay silinmez.

`work dependencies <reference> --project <project> --json` en fazla 100 mevcut
bagimliligi ve truncation bilgisini verir. `satisfied` yalniz hedef Work completed
ve evidence digest'i kayitliysa true olur. Cancelled/archived hedef tamam sayilmaz.
Bu gorunum lease, grant veya Work kapanisi uretmez.

## Kayit ve kabul siniri

- Ekleme/kaldirma mevcut Work revision'ini optimistic concurrency ile ilerletir;
  Work event ve work_dependency_event ayni transaction/savepoint'te yazilir.
- Self-edge, farkli proje, duplicate edge ve cycle DB trigger'lariyla reddedilir.
  Cycle reddi cagiran taraf hatayi yakalasa bile yarim revision birakmaz.
- Bagimlilik yalniz proposed/ready/blocked Work'ta degistirilir. Active Work once
  kontrollu olarak blocked durumuna gecmelidir. Tamamlanmis Work'a bag eklenmez.
- Unfinished dependency ready/active/verification/completed gecisini engeller.
  Canonical lease/effect admission ayni grafigi yeniden okur; genel kuyruk filtresi
  LIMIT'ten once uygulanir. Bekleyen is uygun baska isi ac birakmaz.
- Yeni bagimlilik source Work revision'ini degistirir; eski local job binding'i
  sessizce yenilenmez. Yeni plan/binding gerekir. Bagimlilik hedefinin kanitli
  tamamlanmasi mevcut source revision'ini degistirmez.

## Explicit v7 -> v8 migration

Dogal dil Work raporu, `work resume` ve workspace resume ayni SQLite snapshot'inda
bounded Work penceresi icin tek aggregate sorguyla bagimlilik ozetini okur.
Her Work icin toplam/tamamlanmamis oncul sayisi ve blocked bilgisi bulunur.
Resume blocked listesi durumdan veya unfinished dependency'den gelir; canonical
blokaj varsa self-reported checkpoint yonlendirmesinden once gorunur. Bu gorunum
grant/lease vermez. V8 oncesinde destek ve sayilar unknown/null olarak aciklanir;
salt-okunur rapor otomatik migration yapmaz. Raporun dependency-blocked sayisi
yalniz dondurulen acik Work penceresini kapsar; tum proje state sayimi ayridir.

Yeni tablo/view/trigger additive v8 migration'dir. V1-v7 SQL/name/digest pinleri
korunur; varsayilan fresh bootstrap v3 kalir. Eski kullanici DB'si otomatik
yukseltilmez. V7 Work dependency komutu `migration-required` verir.

`db upgrade` v7 icin digest-bound salt-okunur plan uretir. `--uygula --plan-digest`
yolu mevcut paused/disabled admission, writer/spool quiescence, fenced migration
lock, verified online backup, original-row parity ve postcommit readback sinirlarini
kullanir. Geri donus mevcut backup'tan yeni hedefe restore'dur; yerinde downgrade yoktur.

Bu gelistirmede kullanici DB'sine migration uygulanmadi. Yalniz yeni schema SQL
digest/fingerprint sabitleri generator yoluyla uretildi. Cycle/scope/stale revision,
immutable history, removal, dependency completion, queue starvation, CLI exact plan
ve v7-v8 old-row parity regresyon kodu hazirlandi; kullanici karariyla testler tum
gelistirmeler sonrasi tek final asamayi bekler. Bu belge kabul basarisi degildir.
