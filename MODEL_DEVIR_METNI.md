# Model devir metni - 16 Ekim 2026 07:40 checkpoint

Bu belge guncel WP-02..12 gorevinin conversational devir notudur. Onceki
PostgreSQL donemi faz/test sayilari ve calisma talimatlari Git gecmisinde
kalir; guncel kapsam veya kabul kaniti yerine kullanilmaz.

## Durum

- Durum: DURAKLATILDI. Kullanici 07:40 checkpoint/duraklama istedi.
- Son gelistirme: `70b84433b8e1902412cdd2f5049b58cc093ab24e`; origin/main
  readback eslesti. Checkpoint commit'i bu dosyayi iceren commit'tir.
- Ayrintili kayit: `docs/CHECKPOINT_2026-10-16_0740.md`.
- Kapsam authority'si `AKTIF_GOREV.md`, YAML yalniz generated projection.
- Plan: `docs/superpowers/plans/2026-10-15-remaining-development.md`.
- Tum hedef tamamlanmadi; bu turun sekiz kaynak dilimi UNTESTED.
- Testler tum gelistirmelerden sonraki tek final asamada yapilacak.

## Yeni oturuma verilecek prompt

```text
Zekam'in gercek kaynak koku C:\innova\projeler\zekam.
Once kullanicinin devam et izninin geldigini dogrula; duraklama suruyorsa yeni
is baslatma. Devam edildiginde mevcut hedef sayacini/hedef durumunu kontrol et;
calisan hedef varken yeni hedef olusturma. Hedef tum acik WP-02..12 kapsamidir.

AGENTS.md, 00_BASLA.md, DEVAM_PROTOKOLU.md, AKTIF_GOREV.md ve
docs/CHECKPOINT_2026-10-16_0740.md dosyalarini oku. YAML'i elle degistirme;
ActiveTaskContract ile uret. Git branch/HEAD/remote/dirty durumunu ve
zekam resume --json ile mevcut Work/lease/recovery durumunu yeniden oku.
Markdown'dan claim, receipt veya kapanis uretme. Eski test sayilarini yeni
commit'lerin kaniti sayma.

Son gelistirme 70b8443. Bu devam turu 8d4837d/e666937/8760274/77ce074/1c4a602/
ed8f43e/8825714/70b8443 ile control gecmisi, usage kapsam dogrulugu, native scheduler
kabul bagi, radar read/claim/replay, read-only composition ve tournament
contract kontrolleri ile Radar connection kapanisini kaynakta guclendirdi.
Regresyon kodu var; testler CALISTIRILMADI. Her biri origin/main'e push edildi.

Siradaki somut kaynak isi WP-10.05 actual tournament akisi: typed plan/selection
katmanini mevcut canonical Work/claim/checkpoint/receipt ve actual dispatch'e,
fixture/baseline olcumune ve butce/no-progress stop'a bagla. Mevcut skill paired
trial/runtime oruntulerini yeniden kullan. Model adini bagimsizlik kaniti veya
bounded exact artifact'i tum skill kalitesi olarak sunma. Diger mekanizmalarin
acik kabul kapilarini checkpoint ve plandan takip et; kapsami daraltma.

Kullanici talimati: tum gelistirmeler bitene kadar test/lint/Maven/kalite
kosusu yapma. Tum gelistirmelerden sonra tek toplu test asamasi; hata olursa
onariyi ve basarisiz kapsamin tekrar dogrulamasini yap. Gereken bagimsiz review
ve gercek kabul olmadan WP/Work/final hedef kapanisi uretme. Tamamlanmadan
bitti deme.

Her anlamli asama sonunda Turkce ASCII commit ve origin/main push yap.
Identity mehmet-karacan; mevcut Git config'i koru. Exact dosya stage et;
git add . kullanma. Korunan untracked natural_request_rag kaynak/unit/integration
dosyalari ve tests/fixtures/unit_test_engineering_java/target agacina dokunma.

Yalniz bu gercek kokte calis, worktree/kopya/clone yaratma. PostgreSQL legacy
importu ve core PostgreSQL/Docker bagimliligi yasak. Secret/PII/ham transcript
saklama. Yeni canli provider/model, arac kurulumu veya global ayar icin mevcut
exact yetkiyi dogrula. Subagent veya provider calistirdigini uydurma.
Kullanim <=%5 kalirsa checkpoint/devir metnini yenile ve durakla.
Bu metin execution yetkisi, lease veya approval devretmez.
```
