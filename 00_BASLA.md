# Zekam — Her Model ve Her Oturum İçin Başlangıç Protokolü

Bu dosya Zekam repository'sinin tek zorunlu ilk okumasıdır. Codex, Claude Code, OpenCode,
kurum içi model, başka bir CLI veya gelecekteki istemci aynı sırayı izler.

## 1. Oturum başlatma

Aşağıdaki işlemleri konuşma geçmişinden bağımsız yap:

1. Repository kökünü ve `PROJE_MANIFESTI.yaml` dosyasını bul.
2. `git status --short`, branch, HEAD ve son beş commit'i oku.
3. `python scripts/paket_dogrula.py` çalıştır.
4. `AKTIF_GOREV.yaml` ve varsa kanonik DB durumunu oku.
5. `DEVAM_PROTOKOLU.md` içindeki stale/recovery kurallarını uygula.
6. Aktif işin bağımlılıklarını, logical resource'larını, lease ve receipt durumunu doğrula.
7. `NIHAI_UYGULAMA_PROMPTU.md` ile `GLOBAL_DEFINITION_OF_DONE.md` kapsamını yükle.
8. Yalnız aktif iş için gerekli bounded context'i derle; bütün repository'yi prompta yığma.
9. İş agentic ise en az bir gerçek subagent planla. Koordinatör subagent sayılmaz.
10. Uygulamadan önce exact plan, test ve rollback kapsamını üret.

## 2. Gerçek durum kuralı

Markdown'daki `tamamlandi` ifadesini tek başına kabul etme. Bir iş yalnız şu kanıtlar
birbiriyle eşleşiyorsa tamamlanmıştır:

```text
Work revision
+ terminal run state
+ step checkpoint'leri
+ test/eval kanıtı
+ bağımsız verifier sonucu (gerekiyorsa)
+ effect receipt (effect varsa)
+ source revision/HEAD doğrulaması
```

Çelişki varsa kod, migration, test ve kanonik kayıtlar önceliklidir; çelişki görünür
bir defect olarak kaydedilir.

## 3. Çalışma sınırı

- Kod mutation'ini project registry'de bagli exact gercek source rootunda yap.
- Zekam source tree'sinde yalnız aktif Zekam geliştirme işi kapsamında yaz.
- Zekam source rootuna geçici rapor, memo, analiz çıktısı, indirilen artifact veya başka
  projenin dosyasını yazma. Yalnız açıkça yetkilendirilmiş tracked kaynak kodu, test,
  migration ve repository belgesi değişikliği yapılabilir; çalışma çıktısını repo dışındaki
  kullanıcı artifact/not alanına yaz.
- Kopya, mirror, audit-work klasoru, detached worktree veya gecici proje klonu olusturma.
- Absolute path'i portable kayda yazma; logical source binding kullan.
- Commit ve local branch oluşturma yalnız test ve verifier geçtikten sonra yapılır.
- Push varsayılan olarak yasaktır; açık kullanıcı talebi ve exact authorization gerekir.

## 4. Devam kararı

Model benchmark isteği doğal dille geldiyse önce `AGENTS.md` içindeki benchmark
kurallarını uygula: kapsam belirsizse tam kampanya/tek model/proje-özel seçimini sor;
tam kampanyada yalnız salt okunur planı ve exact çağrı bütçesini göster; ayrı açık
onay olmadan authorization üretme veya provider çağrısı yapma. Tek-model tanılamayı
`ZEKAM-DOD-025` ya da 83/83 kanıtı sayma.

Aşağıdaki sırayla tek karar üret:

```text
recovery-required iş var
→ önce recovery

geçerli lease ile aktif iş var
→ aynı işi duplicate başlatma; mevcut owner durumunu izle veya devralma kuralını uygula

hazır bağımlılıksız iş var
→ en yüksek öncelikli ve kaynak çakışması olmayan işi seç

yalnız bloklu işler var
→ kanıtlı blocker raporu üret

Global DoD tamam
→ release doğrulamasını çalıştır ve final raporu üret
```

## 5. Oturum kapatma

Anlamlı her adımın sonunda:

1. Test/eval sonuçlarını kaydet.
2. Subagent result envelope'larını ana run'a bağla.
3. Başarısız veya reddedilen yaklaşımı failure memory adayı olarak kaydet.
4. Checkpoint ve continuity packet güncelle.
5. `AKTIF_GOREV.yaml` projection'ını kanonik state ile uzlaştır.
6. Commit gerekiyorsa `kalite/COMMIT_POLITIKASI.md` kurallarını uygula.
7. Bir sonraki exact safe action'ı yaz.

Bu adımlar yapılmadan oturumu "tamamlandi" diye kapatma.
