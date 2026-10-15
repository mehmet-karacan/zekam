# Native istemcide dogal dil Work islemleri

Bu akis kullanicinin acik Work isteginde native ajan tarafindan yurutulur.
Klasoru acmak veya bu belgeyi okumak islem yetkisi vermez. Zekam ikinci bir ajan
yoneticisi veya zorunlu subagent katmani kurmaz.

## Islem secimi

`route preview` acik Work/gorev/is kaydi islemini `work-management` niyetine ve
`operational-work-graph` capability'sine baglar. JSON'daki `work_operation` ve
`native_commands` yalniz allowlisted arac secimidir; `grants_authority=false` ve
`tool_invocation_performed=false` korunur. Agent gereken komutlari kendisi cagirir;
kullanicidan komut, UUID, revision, digest veya JSON hazirlamasini istemez.

| Kullanici istegi | Native ajanin yapacagi |
|---|---|
| GPU icin yeni gorev olustur | Baslik/tur/kabul kriterini kullanici isteginden al; eksik zorunlu basligi sor. Kayitli projeyi coz; create planini oku ve mevcut yetkiyle uygula. Retry icin ayni istek key'ini koru. |
| GPU gorevleri listele | Proje kapsamli `work list` oku; durum filtresini istege gore uygula. |
| GPU work 17 detayini/gecmisini/bagimliliklarini goster | `ask` veya ilgili salt-okunur Work komutuyla exact proje ve harici numarayi coz. |
| GPU work 17 durumunu degistir | Once `work show` ile revision/durumu oku; hedef durumu kullanicidan coz. `work transition` dry-run ve mevcut yetkiyle apply; stale revision'da guncel kaydi tekrar oku, eski plani uygulama. |
| GPU work 17 ve work 18 bagimlilik ekle | Bagimli Work ile oncul yonunu istekte dogrula; yon belirsizse sor. `work relate` dry-run digest'ini ayni source/target/revision icin kullan. Cycle/scope/migration hatasini atlama. |
| GPU work 17 checkpoint kaydet | Work'un canonical Run/checkpoint gorunumunu oku; birden cok olasi Run varsa netlestir. Kaynak snapshot ve sonraki sequence'i gercek kayittan coz. `work checkpoint` ile bounded, secret/transcript icermeyen step ozeti kaydet. |
| GPU work 17 tamamla | Guncel Work revision'i ve verification durumunu oku. Gercek test/verification/effect receipt kaniti ve gereken bagimsiz verifier olmadan `work close` uygulama; eksik kaniti acikla. |

Kisa dogal dil eslestiricisi tum dil bicimlerini kapsamaz. Native ajanin anladigi
diger acik talepler de ayni canonical komutlara ve ayni yetki/kanit kosullarina tabidir.
Okuma ile degisiklik karisimi, birden cok proje veya birden cok kayit belirsizse
route clarification verir; mutation/retrieval/provider cagrisi baslatmaz.

Work harici numarasinin icindeki proje benzeri metin (`work SKY-123`) yerel kayit
kimligidir; kendi basina SKY/Jira scope'u uretmez. Proje ayri kanonik registry'den
cozulur. Kayitsiz/belirsiz proje tahmin edilmez.

## Sinirlar ve kanit

Model yorumu ve route karari Work/lease/grant/receipt authority'si degildir.
Create/transition/relate/checkpoint/close mevcut CLI mutation admission ve SQLite
guard'larindan gecer. Checkpoint anlatimdir; tamamlanma veya yetki devri degildir.
`ask` yalniz okur; degisiklik uyguladigini iddia etmez. Native ajan sonucu gercek
komut readback'i ve varsa terminal receipt ile dogal dilde ozetler.

Bu dilimin unit/CLI regresyon kodu hazirlandi; kullanici talimatiyla testler tum
gelistirmeler sonrasi final toplu asamaya ertelendi. Bu belge native host kabul
kaniti veya WP-07 kapanisi degildir.
