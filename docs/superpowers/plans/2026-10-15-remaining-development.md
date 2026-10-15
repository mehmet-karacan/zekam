# Kalan Gelistirmeler Uygulama Plani

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. User instruction overrides intermediate test execution: run tests only in the final phase after all development.

**Goal:** AKTIF_GOREV.md WP-02..12 kapsamindaki kalan gelistirmeleri sirayla tamamlamak.

**Architecture:** Mevcut native CLI, canonical SQLite Work/lease/receipt ve CAS yollari genisletilir. Belge ve projection operational authority uretmez.

**Tech Stack:** Python, SQLite, CAS, native CLI adapterleri.

**Spec:** AKTIF_GOREV.md (tek kapsam authority'si).

## Global Constraints

- Gercek kaynak kokunde calis; worktree veya kopya olusturma.
- Kullanici karari: tum gelistirmeler bitmeden test calistirma; son asamada tek toplu test kosusu.
- Her asama Turkce ASCII commit ve origin/main push; test bekleme durumunu kaydet.
- Yeni canli provider/model cagrisi, kurulum ve global ayar kendi exact iznini gerektirir.
- Korunan untracked natural_request_rag dosyalari ve Java target dizinine dokunma.
- Work kapanisi ve release kabulu icin gereken bagimsiz kaniti uydurma.

## Review Focus

- Lease startedAt process incarnation degildir; canli spool sahibi korunur.
- Ayni revision/tree farkli kaynak kokunu esdeger yapmaz.
- Native checkpoint Work veya grant yetkisi tasimaz.
- Tekrar edilen delivery effect'i cogaltmaz; eksik receipt recovery gerektirir.
- Test ertelenmesi gelistirme veya kabul basarisi olarak raporlanmaz.

## Sirali gorevler

- [ ] 1. WP-07.05: spool candidate processStartedAt ayrimi ve native source root drift reddi. Dosyalar: opencode_spool.py, local_continuity_native.py; regresyon kodu ilgili mevcut test dosyalarinda hazirlanir.
- [ ] 2. WP-07.01..04: canonical Work dogal dil ve resume/lease/grant akislarindaki kalan uygulama bosluklarini kaynak incelemesiyle belirle ve tamamla.
- [ ] 3. WP-02/03: native mutation admission, istemci ve shell/publication transport baglantilarini tamamla.
- [ ] 4. WP-04/05/06: discovery/parser/secret ve incremental publication/reconciliation; issue/history/provenance baglantilarinin kalan uygulamasini tamamla.
- [ ] 5. WP-08/09/10: host capability, model/agent envelope, test muhendisligi ve memory/skill lifecycle baglantilarini tamamla.
- [ ] 6. WP-11/12: scheduler/backup/restore/report ve canonical mimari/release ciktilarini tamamla.
- [ ] 7. Butun gelistirme maddelerinin kaynak kapsamini yeniden denetle; gerekli gercek ortam/erisim eksiklerini exact kapilara bagla.
- [ ] 8. Yalniz tum gelistirmelerden sonra tek toplu test asamasi, gereken bagimsiz review ve kabul kanitlari. Hata varsa duzelt ve yalniz basarisiz kapsami yeniden dogrula.

## Yurutme kaydi

- Baslangic: 578238ef2aea1af614a766039cca3787a2e1e6f0. Kullanici bu sirali gelistirme ve son test duzenini onayladi.
- Eski test sonuclari yeni degisiklikler icin kanit sayilmaz. Hicbir WP bu planla kapatilmaz.
- Gorev 1 gelistirmesi 747c6ea: lease/process marker ayrimi ve native root-drift reddi. Regresyonlar hazir; CALISTIRILMADI.
- Gorev 3 native admission parcasi: start/checkpoint/close LOCAL_EFFECT ve work-item hint baglantisi eklendi. Regresyonlar hazir; CALISTIRILMADI. Shell/publication ve diger istemci baglari acik.
- Geri donus: her asamanin exact commit'ini yeni revert commit'iyle geri al; kullanici verisine veya operational DB'ye reset uygulama.
