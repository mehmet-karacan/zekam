# Zekam Agent Talimatı

1. İlk olarak `00_BASLA.md` dosyasını uygula.
2. Kanonik nihai görev `NIHAI_UYGULAMA_PROMPTU.md` dosyasıdır.
3. Devam ve recovery kuralları `DEVAM_PROTOKOLU.md` içindedir.
4. Agentic işte en az bir gerçek subagent kullan; koordinatör sayılmaz.
5. Kod mutation'ini bagli exact gercek project source rootunda yap; kopya, mirror, audit-work
   klasoru, detached worktree veya gecici proje klonu olusturma.
6. Work/authority durumunu vector, memory veya Markdown'dan üretme.
7. Secret değerini prompt/log/artifact/vector içine alma.
8. Claim olmadan effect, terminal receipt olmadan başarı üretme.
9. Test ve risk bazlı bağımsız verifier olmadan Work Item kapatma.
10. Commit mesajını Türkçe anlamlı ve ASCII-only yaz.
11. Global DoD bitmeden görevi “sonraki faz” diyerek bırakma.
12. Model kabulü varsayılan olarak yalnız bu cihazda kurulu olduğu, model artefaktı bulunduğu ve yerel execution boundary'si kanıtlandığı hedefler için çalışır. Kullanıcının açıkça istediği reviewed OpenCode/AIHub benchmark kampanyası bu varsayılanın tek exact uzak-provider istisnasıdır; plan, çağrı bütçesi ve tek kullanımlık yetkiler ayrıca doğrulanır.
13. Kurulu bir istemci tek başına yerel model sayılmaz; kullanıcı yeni exact kapsam açmadıkça canlı provider çağrısı yapma.
14. Doğal dilde benchmark başlatma isteği önce yalnız `zekam model campaign plan --json` dry-run'ına yönelir. Model sayısı, ses exclusion'ı ve exact çağrı bütçesi gösterilmeden authorization üretme veya kampanyayı çalıştırma. Çalıştırma için plan gösterildikten sonra ayrı ve açık kullanıcı onayı iste.
15. `ZEKAM-DOD-025` yalnız güncel kaynak/config/inventory/policy/fixture/verifier bağlarına sahip tam reviewed kampanya ve kanonik kanıt kapısı geçerse kapanır. Tek-model veya kısmi tanılama 83/83 kanıtı sayılamaz.
