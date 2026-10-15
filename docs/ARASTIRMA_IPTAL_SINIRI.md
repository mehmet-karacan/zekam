# Arastirma Iptal Siniri

`run_research(..., cancel=threading.Event())` programatik iptal alir. Onceden
iptal edilen cagri enqueue/claim baslatmaz. Default OpenCode research adapter'i
ayni event'i process wait'e iletir. Timeout bounded integer 1..600 saniyedir;
monotonic deadline prompt hazirligindan once baslar.

Prompt mevcut `opencode run` argv'sinde tasinir; ayni payload stdin'e tekrar
yazilmaz. Bu sayede stdin okumayan child, cancellation kontrolunden once pipe
write icinde takilmaz. Stdin EOF ile kapanir. Reader thread baslatma veya process
wait sirasindaki Ctrl+C dahil hatalarda child tree sonlandirilir, baslamis reader
thread'leri beklenir ve pipe handle'lari kapanir.

Adapter iptal, timeout veya overflow sonucunu model/verification basarisi diye
sunmaz. Event stream dogrulamasi sonrasi iptal tekrar denetlenir. Runtime gec
adapter sonucunu ve materialization oncesinde iptal edilen raporu yayinlamaz.
Claim'den sonraki interruption mevcut terminal failure/recovery kaydina gider;
yeni invocation otomatik tekrar edilmez. Materialization basladiktan sonraki
atomik yayin existing protokolunu korur.

Custom adapter'in kendi child temizligi o adapter'in sorumlulugundadir; runtime
event'i kontrol ederek gec sonucu reddeder. Remote query transportunun aktif
cagri ici iptali, host-neutral native child baglantisi ve gercek host kabul
matrisi bu dilimle dogrulanmis sayilmaz.

`tests/unit/test_research_process_cancellation.py` icinde provider-free gercek
Python child senaryolari hazirlandi: pre-cancel, aktif child cancel, Ctrl+C,
deadline ve timeout tip sinirlari. Testler CALISTIRILMADI; final toplu test
asamasini bekler. Canli model/provider veya kullanici projesi komutu calistirilmadi.
