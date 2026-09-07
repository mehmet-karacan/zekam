# Gorev Sozlesmesi Arsivi

Bu dizindeki dosyalar tarihsel, salt-okunur kapsam kanitidir. Yasayan gorev authority'si
yalniz repository kokundeki `AKTIF_GOREV.md` dosyasidir. Arsiv dosyalari yeni is, yetki,
claim veya approval uretemez.

## 6 Eylul 2026 kapsam gecisi

| Alan | Onceki sozlesme | Yasayan sozlesme |
|---|---|---|
| Gorev | `ZEKAM-LOCAL-INTELLIGENCE-PLANE-001` | `ZEKAM-AUTONOMOUS-EVOLUTION-001` |
| Icerik SHA-256 | `ebd9ca00a5cc500a650984e3cdc7be22186b85b5a283d86a6f9d863237530629` | `4788116aa01885aa8b884579f4f8ee1ce87b76ee54f55c8fe884aec7e33580b2` |
| Onceki Git blob | `ce2980e819df68ccf2ac375c1f550b6d675ebeaa` | Uygulanmaz |
| Baseline HEAD | `d95cdac2713df797e42afda020ab6e8e55188031` | `b59221a0891dc94d3702d042132254066dc089ed` |

Gecis oncesi operational okumada acik Work Item, calisan lease ve recovery case yoktu.
Eski Markdown ve onun exact generated projection'i degistirilmeden bu dizine alindi.
Yeni projection yalniz yeni yasayan Markdown'in exact byte digest'inden uretildi.

## Carry-forward

- Legacy PostgreSQL veri erisimi ve Zekam core icin PostgreSQL/Docker bagimliligi yasaktir.
- Operational state, approval, claim ve receipt retrieval/Markdown/model ciktisindan
  uretilmez.
- GPU ve SKY proje binding'leri ile kaynakli hibrit RAG korunur. Son Windows ACL ve RAG
  tekrar dongusu duzeltmesi yeni regresyon matrisine tasinir.
- Onceki local-first veriler, store'lar, receipt'ler ve proje indexleri silinmez veya yeniden
  bootstrap edilmez.
- Onceki operational Work Graph'ta tasinacak acik Work Item yoktur. Global DoD'nin 83
  kriteri kanit gelmeden pending kalir.
- Bu cihazdaki uygulama ve native kabul Windows kapsamindadir. macOS sonucu basarili
  varsayilmaz; ayri cihaz kaniti gelene kadar `unverified` kalir. Bu ayrim Windows yerel
  gelistirmesini durdurmaz, fakat global cross-platform kabulunu kapatmaz.
- Canli provider, OS scheduler kurulumu, standing grant aktivasyonu, commit ve push bu
  gecisten yetki kazanmaz.
