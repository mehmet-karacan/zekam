# Work Checkpoint Payload Siniri

Operational SQLite `record_checkpoint` yolu, checkpoint'in digest eslesmesinden
once payload'i canonical JSON olarak dogrular.

## Sinirlar

- Canonical JSON en fazla 16 KiB'dir.
- Nesne/list derinligi en fazla 16, toplam alan/eleman sayisi en fazla 512'dir.
- Secret, API key, credential, password, private key, owner token, prompt, response,
  transcript ve raw content adlari reddedilir.
- Secret atamasi gibi `token: ...`, `password=...` ve PEM private key degerleri
  reddedilir.
- `grants_authority` yalniz `false` olabilir; checkpoint yetki vermez.

Bu kontrol payload'i temizleyip saklamaz; reddederek ham verinin kalici checkpoint
alanina girmesini engeller. Checkpoint yine Work/Run/source snapshot bagini veya
lease/grant yetkisini kendi basina uretmez. CLI preview digest hesaplayabilir; asil
authority uygulama aninda ayni operational dogrulamayi yapar.

Regresyon kaynagi `tests/unit/test_operational_store.py` icindedir. Testler tum
gelistirmeler bitene kadar kullanici talimatiyla calistirilmadi.
