# Zekam Agent Talimatı

1. İlk olarak `00_BASLA.md` dosyasını uygula.
2. Kanonik nihai görev `NIHAI_UYGULAMA_PROMPTU.md` dosyasıdır.
3. Devam ve recovery kuralları `DEVAM_PROTOKOLU.md` içindedir.
4. Agentic işte en az bir gerçek subagent kullan; koordinatör sayılmaz.
5. Haricî project source root'a doğrudan yazma.
6. Work/authority durumunu vector, memory veya Markdown'dan üretme.
7. Secret değerini prompt/log/artifact/vector içine alma.
8. Claim olmadan effect, terminal receipt olmadan başarı üretme.
9. Test ve risk bazlı bağımsız verifier olmadan Work Item kapatma.
10. Commit mesajını Türkçe anlamlı ve ASCII-only yaz.
11. Global DoD bitmeden görevi “sonraki faz” diyerek bırakma.
12. Model kabulü yalnız bu cihazda kurulu olduğu, model artefaktı bulunduğu ve yerel execution boundary'si kanıtlandığı hedefler için çalışır; kurulu olmayan veya uzak provider hedefleri varsayılan kapsam dışıdır.
13. Kurulu bir istemci tek başına yerel model sayılmaz; kullanıcı yeni exact kapsam açmadıkça canlı provider çağrısı yapma.
