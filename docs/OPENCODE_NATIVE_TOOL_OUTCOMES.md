# Native tool sonucunun dogru kaydi

WP-07/10.03 invocation baglantisinin kaynak incelemesinde generated OpenCode
plugin'inin her `tool.execute.after` olayini completed diye kaydettigi bulundu.
V2 native hook ayni yuzeyde completed ve error sonuclarini tasir:
[OpenCode V2 plugin tool hooks](https://opencode.ai/v2/docs/build/plugins/#tools).
Hata veya eksik native status basari olarak yorumlanamaz.

Generated plugin v4, V2 event.status alanini kullanir: completed -> completed,
error/failed -> failed, cancelled -> cancelled; eksik veya taninmayan deger ->
unknown. V1 compatibility callback'i yalniz native successful output'un bounded
shape'ini (title/output string ve metadata object) gordugunde completed kaydeder;
output yoksa unknown olur. Tool result icerigi bu tanima icin saklanmaz.

After forwarding'inde native error/result object'i yayilmaz. Error.name/code/
message, title, output ve metadata aktarilmaz; sabit native-tool-error,
native-tool-cancelled veya native-tool-status-unknown kategorisi kullanilir.
Bu gozlem prompt, response veya execution authority tasimaz.

Resume projection error/failed'i failed, cancelled/unknown/eksik status'u
interrupted olarak okur. Idle, compaction, deleted veya semantik checkpoint
bu gozlemi basarili checkpoint'e ceviremez. Sessiz retry yerine sonuc dogrulama
yonlendirmesi korunur. Sonraki gercek native tool before/after gozlemi kendi
running/completed sonucuna sahip olabilir; eski failure olayi immutable
ledger'dan silinmez. Bu, tum session tool'larinin kalitesine iliskin kabul kaniti
degildir; Work veya skill outcome kapanisi uretmez.

Mevcut exact v3 template digest'i legacy ownership listesine eklendi; kullanici
tarafindan degistirilmis plugin marker ile sahiplenilmez. Kaynak template v4'e
gecildi; bu gelistirmede global client dosyasi degismedi ve kurulum yapilmadi.
Eski v3'un completed diye yazdigi event'lerden gercek native hata geriye donuk
uretilemez; bunlar yeni verified invocation kaniti diye yukseltilmez.

V1/V2 Bun runtime fixture kaynaklari native status, missing/future status,
cancel, error/result content non-disclosure ve resume fail/unknown korunmasi
icin genisletildi. Native executable kabul fixture'i eski npm paket yolunu
varsaymak yerine paylasilan shell-free resolver'i kullanir; gercek kurulu V2
sirf paket adi degisti diye sessizce skip edilmez.

Testler CALISTIRILMADI; kullanicinin talimatiyla final toplu asamayi bekler.
Native per-call skill/revision/package invocation producer baglantisi ve
gercek host kabul kapilari halen aciktir.
