# Zekam Global DoD durum raporu

Bu rapor 5 Eylül 2026 tarihinde `AKTIF_GOREV.md` K-001, K-002 ve Bölüm 31.4
uyarınca yeni local-first mimari için yeniden başlatılmıştır. Önceki PostgreSQL
kanıtlarından gelen `passed` durumları taşınmamıştır.

## Özet

| Alan | Değer |
|---|---:|
| Kriter sayısı | 83 |
| Passed | 0 |
| Pending | 83 |
| Failed | 0 |
| Blocked | 0 |
| Global tamamlanma | Hayır |

Mac kabul kanıtları `AKTIF_GOREV.md` içindeki WP ve Bölüm 33 durumlarına ayrı ayrı
bağlanır. Windows x64, uzak OpenCode provider yolları, supported-Python matrisi ve
tam çapraz-platform evidence bundle K-013 kapsamında açık olduğu için bu rapor
`tamamlandı` sonucu üretemez.

Legacy PostgreSQL'e özgü kriterler uygulanacak iş olarak değil,
`removed-by-new-architecture` sınıfında tutulur; yerel halefleri yeni evidence ile
yeniden değerlendirilir. Ayrıntılı kriter listesi ve mevcut `pending` durumları
`GLOBAL_DEFINITION_OF_DONE.md` ile `kalite/GLOBAL_DOD.yaml` içindedir.
