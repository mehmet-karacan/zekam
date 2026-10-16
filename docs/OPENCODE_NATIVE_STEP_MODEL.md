# Native invocation model kaynagi

WP-10.03 outcome verifier baglantisinin kaynak incelemesinde V2 tool callback'inin
model tasimadigi goruldu. Session default model veya kullanicinin son model secimi,
oncesinde uretilen native assistant message'in modeli yerine kullanilamaz.

Resmi kaynaklar:

- [Tool hook alanlari](https://github.com/anomalyco/opencode/blob/v2/packages/plugin/src/promise/tool.ts)
- [Exact step/message model olayi](https://github.com/anomalyco/opencode/blob/v2/packages/schema/src/session-event.ts)
- [Model.Ref provider, id ve variant](https://github.com/anomalyco/opencode/blob/v2/packages/schema/src/model.ts)

Generated plugin v6 yalniz session.step.started olayinin sessionID,
assistantMessageID, agent ve bounded model scalar alanlarini bellekte tutar.
En fazla 256 message kaydi bulunur; session deleted'da ilgili kayitlar cikarilir.
Content, prompt, reasoning, endpoint, headers veya provider credentials okunmaz
ve model map'ine konulmaz. Ayni message icin cakisan model/agent metadata'si
ambiguous kalir. Gec gelen veya cache'ten dusmus bilgi yerine tahmin yapilmaz.

Skill after hook'unda session, message ve agent exact eslesirse native model ref
provider/model#variant biciminde lifecycle --model alanina aktarilir. v2 skill
load etiketi u alaninda bu ref'in SHA256 hash'ini de tasir. Etiket 500 karakter
sinirini korur. Eslesme yoksa mevcut v1 load etiketi kalir, yeni model kimligi
uydurulmaz. Default session model degisikligi exact step kaydini degistirmez.

Invocation plan producer v1 ve v2 etiketlerini ayirir. v2'de model ref syntax,
bounded/secret-free model politikasi ve etiketteki hash eslesmesi gerekir.
Model native-step-message provenance'i ile planda pin edilir. v1'de model_ref
metadatasi bulunsa bile bu bag olmadigi icin unavailable kalir. Lifecycle model
alaninda #variant ve + literal'i desteklenir; session/agent/diger kimliklerin
alfabesi genisletilmez.

Exact v5 plugin hash'i eski yonetilen dosya sahipligi listesinde korunur.
Global plugin kurulumu veya kullanici ayari degismedi; model cagrisi yapilmadi.
Native model kimligi host-reported metadata'dir: upstream alias bagimsizligi,
host attestation veya verified skill outcome kaniti degildir. Invocation sonucu
unverified, host_attested=false ve skill_effect_verified=false kalir.

Bun fixture kaynaklarina exact, farkli message/session/agent, conflicting model,
invalid model ve sonradan session default switch senaryolari eklendi. SQLite
codec/recording kaynaklarina v2 hash drift, missing model, v1 unavailable ve
variant metadata roundtrip regresyonlari eklendi. Testler CALISTIRILMADI;
tum gelistirmeler bittikten sonraki toplu asamayi bekler.

Siradaki gelistirme gercek artifact/task evidence'i ve ayri verifier execution
receipt'ini invocation outcome'a baglamaktir. Sadece farkli model etiketi veya
tamamlanan load kaydi, outcome basarisi ya da WP-10.03 kapanisi uretmez.
