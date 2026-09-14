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

## 13 Eylul 2026 kapsam gecisi

| Alan | Onceki sozlesme | Yasayan sozlesme |
|---|---|---|
| Gorev | `ZEKAM-AUTONOMOUS-EVOLUTION-001` | `ZEKAM-PERSONAL-SKILL-LIFECYCLE-001` |
| Icerik SHA-256 | `9408eff417b42801580c94988c3ee3b6eef5bc02a2a54e4d979bfe8310854aba` | `d490accbc01e49da44050987e39a514102a6f7eea8c2ee4b3791fc02ebbb7cd6` |
| Onceki Git blob | `d976cc570cb993787842a2b1a857206db7173c4f` | Uygulanmaz |
| Baseline HEAD | `b59221a0891dc94d3702d042132254066dc089ed` | `d273e543600176a1b7cfc39b6696994cf8fec5cf` |

Gecis oncesi evolution admission kontrollu olarak duraklatildi. Operational okumada acik
Work Item, calisan lease, recovery case veya bekleyen outbox yoktu; current standing grant
sayisi sifirdi. Onceki sozlesmenin exact MD/YAML byte'lari bu dizinde tarihsel referans
olarak korundu. Yeni projection yalniz yasayan Markdown'in exact byte digest'inden uretildi.

### Carry-forward

- Onceki evolution receipt, claim, store, scheduler ve verileri geriye donuk degistirilmez.
- Eski kapsamdan canli provider, native supervisor, standing grant, commit veya push yetkisi
  devralinmaz; yeni task digest'i butun eski grant'leri drifted yapar.
- Legacy PostgreSQL veri erisimi ve core icin PostgreSQL/Docker bagimliligi yasaktir.
- Operational authority, approval, claim ve receipt Markdown, skill paketi, retrieval veya
  model ciktisindan turetilmez.
- Global DoD ve onceki acik kabul maddeleri yeni kanit olmadan tamamlanmis sayilmaz.
