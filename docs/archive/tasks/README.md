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

## 15 Eylul 2026 CLI entegrasyon politikasi kapsam gecisi

| Alan | Onceki sozlesme | Yasayan sozlesme |
|---|---|---|
| Gorev | `ZEKAM-PERSONAL-SKILL-LIFECYCLE-001` | `ZEKAM-CLI-INTEGRATION-POLICY-001` |
| Icerik SHA-256 | `d490accbc01e49da44050987e39a514102a6f7eea8c2ee4b3791fc02ebbb7cd6` | `2c4029e9a9e1b123445502fa9cb3c973e55ccc3dc6c765896f49bc1b81089758` |
| Onceki Git blob | `6b33333fd1033b05e42e432c6fb3e920c44c89af` | Uygulanmaz |
| Baseline HEAD | `d273e543600176a1b7cfc39b6696994cf8fec5cf` | `638b6b703226b0888c031d3e38f919779d4b4865` |

Gecis oncesi operational okumada acik Work Item, calisan lease, recovery case veya bekleyen
outbox yoktu; evolution `owner-pause` durumundaydi ve aktif standing grant sayisi sifirdi.
Onceki MD/YAML exact byte'lari bu dizinde korundu. Yeni projection yalniz yasayan Markdown'in
exact byte digest'inden uretildi. Scope transition local runtime claim, effect, terminal receipt
ve journal readback zinciriyle kaydedildi; provider veya network cagrisi yapilmadi.

### Carry-forward

- Onceki skill lifecycle kapsamindaki aktivasyon, evaluation, production wiring ve native
  istemci kabul aciklari tamamlanmis sayilmaz; yeni gorev altinda duplicate enqueue edilmez.
- Canonical skill paketleri, activation/outcome gecmisi, eski receipt ve grant kayitlari
  korunur. CLI cleanup skill revoke veya gecmis veri temizligi degildir.
- Yeni task digest'i onceki task-scope grant'lerini genisletmez; canli provider, native
  supervisor, commit veya push yetkisi devralinmaz.
- Global DoD ve onceki kabul maddeleri yalniz yeni kanitla kapanabilir.

## 16 Eylul 2026 Jira iletisim skill'i kapsam gecisi

| Alan | Onceki sozlesme | Yasayan sozlesme |
|---|---|---|
| Gorev | `ZEKAM-CLI-INTEGRATION-POLICY-001` | `ZEKAM-JIRA-ENGINEERING-COMMUNICATION-001` |
| Icerik SHA-256 | `2c4029e9a9e1b123445502fa9cb3c973e55ccc3dc6c765896f49bc1b81089758` | `9de9fc54647d64e3be7f8b3463236c325a1be49fa3bc3064697a278b59516c67` |
| Onceki Git blob | `5f5db911e785005840547313bc4d91bb53339dd9` | Uygulanmaz |
| Baseline HEAD | `638b6b703226b0888c031d3e38f919779d4b4865` | `638b6b703226b0888c031d3e38f919779d4b4865` |

Gecis kullanicinin acik onayi ile yapildi. Operational okumada acik Work Item, calisan
lease, recovery case veya bekleyen outbox yoktu. Onceki MD/YAML exact byte'lari bu dizinde
korundu; yeni projection yalniz yeni yasayan Markdown'in exact byte digest'inden uretildi.

### Carry-forward

- Onceki CLI entegrasyon gorevinin dirty source degisiklikleri silinmedi, stash/reset
  edilmedi ve tamamlanmis sayilmadi. Bu degisiklikler yeni Jira skill'inin istemci
  projection altyapisi olarak korunur.
- Onceki targeted baseline'da 212 test gecti, 6 test platform nedeniyle atlandi. Tasima
  sirasinda acik kalan Codex `0.154.0` native lifecycle riski; exact Windows binary hash'i,
  resmi hook sozlesmesi ve gercek loopback lifecycle E2E kaniti ile kapatildi. Instruction
  projection hazirligi ile lifecycle kabulü yine ayri kanitlar olarak korunur.
- Skill activation, evaluation/review, native renderer kabulü ve gercek Jira yazma kaniti
  yeni kanit olmadan tamamlanmis sayilmaz.
- Canli Jira yazma, durum/atama/worklog degisikligi, provider cagrisi, commit ve push bu
  gecisten yetki kazanmaz.
- Legacy PostgreSQL veri erisimi ve core icin PostgreSQL/Docker bagimliligi yasaktir.
