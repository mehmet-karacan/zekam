# Zekam — Her Model ve Her Oturum İçin Başlangıç Protokolü

Bu dosya Zekam Work, devam ve recovery işleri için başlangıç protokolüdür; her oturumun
zorunlu ilk okuması değildir. Ortak ince giriş `AGENTS.md`'dir. Codex, Claude Code, OpenCode,
Gemini CLI, kurum içi model veya başka bir istemci Zekam Work yürütürken aynı sırayı izler.

## 1. Oturum başlatma

Kapsam: Aşağıdaki başlangıç sırası repository üzerinde iş yapılacağı oturumlar içindir
(mutation, kaynak fallback'i, çok-kaynaklı research, devam/recovery). Selamlama, teşekkür,
proje içermeyen kavram sorusu ve yeterli pinned citation ile cevaplanan salt-okunur bilgi
sorusu için bu sıra, doctor, subagent veya bütün belgeleri okuma gerektirmez. Küçük yetkili
bir değişiklikte yalnız ilgili bağlam ve gerekli test yüklenir; tarihsel belgeler zorunlu okuma
değildir. Klasörü açmak bu sırayı başlatmaz; kullanıcının seçtiği görev başlatır.

Repository işinde aşağıdaki işlemleri konuşma geçmişinden bağımsız yap:

1. Repository kökünü ve `PROJE_MANIFESTI.yaml` dosyasını bul.
2. `git status --short`, branch, HEAD ve son beş commit'i oku.
3. Paket/şablon/release bütünlüğüne dokunan işte `python scripts/paket_dogrula.py` çalıştır.
4. Bağlayıcı `AKTIF_GOREV.md` dosyasını oku; `AKTIF_GOREV.yaml` projeksiyonunun exact
   digest eşleşmesini ve yerel operational store durumunu doğrula.
5. `DEVAM_PROTOKOLU.md` içindeki stale/recovery kurallarını uygula.
6. Aktif işin bağımlılıklarını, logical resource'larını, lease ve receipt durumunu doğrula.
7. `AKTIF_GOREV.md` ile `GLOBAL_DEFINITION_OF_DONE.md` kapsamını yükle.
   `NIHAI_UYGULAMA_PROMPTU.md` superseded tarihsel kaynaktır; otomatik yüklenmez ve
   authority değildir, yalnız tarihsel gerekçe aranırken açılır.
8. Yalnız aktif iş için gerekli bounded context'i derle; bütün repository'yi prompta yığma.
9. Alt ajan kullanımı host yeteneğine ve işin ihtiyacına bağlıdır; sayaç doldurmak için ajan
   açılmaz. Riskli veya yıkıcı değişiklikte ve Work Item kapanışında builder'dan bağımsız
   doğrulama atlanmaz; aynı modelin başka başlıkla yazdığı onay bağımsız verifier sayılmaz.
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
5. `AKTIF_GOREV.yaml` projection'ını yaşayan `AKTIF_GOREV.md` authority digest'iyle
   deterministik olarak uzlaştır; projeksiyona bağımsız state veya yetki yazma.
6. Commit gerekiyorsa `kalite/COMMIT_POLITIKASI.md` kurallarını uygula.
7. Bir sonraki exact safe action'ı yaz.

Bu adımlar yapılmadan oturumu "tamamlandi" diye kapatma.
