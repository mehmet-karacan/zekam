# Work Resume Canonical Readback

## Kapsam

`work resume --json` artik yalniz acik Work basliklarini listelemez. Secilen
bounded acik Work penceresi icin SQLite operational authority'den son Work
eventlerini, son Run ozetlerini ve bound job referanslarini okur. Dogal dildeki
`zekam ask ... --json` Work durum raporu da ayni canonical bound job okuyucusunu
proje basina donen acik Work penceresine uygular; iki yuzey farkli bir operational
authority veya job payload'i uretmez.

Tekil dogal dil Work okumalari (`ask ... is kaydini goster`) da exact Work
kapsaminda ayni okuyucuyu kullanir; bu yuzeyde scope `exact-work` olarak raporlanir.
Dogru Work kimligiyle cagrilan `work show --json` de ayni bounded alanlari salt
okunur olarak verir.
Checkpoint readback'i de canonical `source_snapshot_id` alanini korur; kaynak
snapshot baglantisi CLI veya dogal dil ozetinde sessizce kaybolmaz.

## Sinirlar

- En fazla 100 acik Work okunur.
- Work basina en fazla 3 event ve 3 Run ozetlenir.
- Bound job penceresi en fazla 50 kayittir; `bound_jobs_truncated` sonucu bildirir.
- Work status raporunda bound job scope'u `returned-open-work-window` olarak
  belirtilir; source snapshot varsa revision/tree/content/config digestleri korunur.
- `recent_activity` event zamanina gore en fazla 300 kayittir.
- Job payload'i, owner token'i, grant'i ve ham provider/model icerigi ciktiya alinmaz.
- Tum cikti salt okunur ve `grants_authority=false` olarak kalir.

## Devam baglantisi

`recent_events` ve `runs` alanlari Work'in canonical event/run zincirini; `bound_jobs`
alanlari ise project, Work revision, Run, step, source snapshot, config ve plan
digest bagini gosterir. Bu gorunum yeni lease veya grant uretmez. Efekt baslatmadan
once mevcut lease, grant, source ve binding kosullari yeniden dogrulanmalidir.

`bound_jobs[*].source_snapshot` mevcutsa snapshot'in source binding ID'si,
revision ref'i ve tree/content/config digestleri ayni canonical SQLite joininden
okunur. Snapshot ID varken bu alanlardan biri eksikse resume readback'i basari
sayilmaz.

Regresyon kaynagi `tests/e2e/test_cli_sqlite_operational.py` icindedir. Kullanici
talimati geregi testler tum gelistirmeler bitene kadar calistirilmadi.
