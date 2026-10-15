# Kayitli Proje Envanteri Ve Kalici Devam

`zekam project inventory <proje> --home <ZEKAM_HOME>` kayitli proje ve bu
cihazin kaynak kok baglantisini cozer. Metadata-only discovery kullanir;
kaynak dosya icerigi, embedding, provider, indeks veya migration calistirmaz.
Ignore kural dosyalari mevcut bounded okuyucuyla okunabilir. Gizli ve
yoksayilan girdiler metadata olarak gorunebilir; degerleri ciktiya alinmaz.

Komut ASCII-safe JSON verir. `--entry-limit` (varsayilan 100, en fazla 1000)
yalniz cikti penceresidir. `entries_truncated` envanterin eksik tarandigi
anlamina gelmez. Gercek tarama `inventory.status`, `stop_reason` ve exact
`entry_count` ile raporlanir. `--max-entries` (en fazla 200000),
`--max-dir-entries` (en fazla 10000), `--max-depth` (0..100) taramayi sinirlar.
Metadata-only taramadan tam icerik source revision/digest'i uretilmez.

Partial rapordaki `inventory.resume_checkpoint` JSON olarak saklanabilir.
`--resume-file <dosya>` bu belgeyi veya onceki komut ciktisini okur; dosya
64 KiB'yi asamaz. Buyuk rapor yerine yalniz checkpoint belgesini saklayin.
Complete rapor `resume_checkpoint: null` verir; bu belge resume girdisi degildir.

Checkpoint v1; exact schema, kok kimligi digest'i, tek root-relative frontier,
frontier tur/boyut/mtime snapshot'i ve butunluk digest'i tasir. Koken kok ve
frontier metadata yeniden dogrulanir; foreign root, degisen frontier, path
traversal, bozuk tip veya digest reddedilir. Yalniz yol listesi yeterli degildir.
Digest imza veya yetki degildir; `grants_authority` daima false olur. Frontier
snapshot'i tum kaynak agacinin degismezligini kanitlamaz.

Resume eski kismi envantere ekleme veya sayfalama yapmaz. Dogrulamadan sonra
verilen yeni sinirlarla bastan bounded traversal yapar. Daha yuksek sinir
verilmediginde ayni partial sonuc alinabilir; complete sonucu zorlanmaz.

Regresyon kaynaklari: `tests/unit/test_discovery_checkpoint_document.py` ve
`tests/e2e/test_cli_project_inventory.py`. Bu gelistirme diliminde testler
CALISTIRILMADI; tum gelistirmelerden sonraki final test asamasini bekler.
