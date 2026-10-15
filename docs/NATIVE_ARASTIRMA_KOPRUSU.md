# Host Bagimsiz Arastirma Koprusu

`run_research(..., native_host=host)` host'un gercek child tool koprusunu
`NativeResearchAdapter` ile ayni canonical research job/claim/receipt/report
akisina baglar. `native_host` ve `adapter` birlikte verilemez. Varsayilan
OpenCode event adapter'i mevcut uyumluluk yoludur; native hook yokken baska
bir istemci icin child capability varsayilmaz veya sahte delegation uretilmez.

## Guven siniri

`NativeResearchHost` trusted in-process host entegrasyonudur. Capability ve
execution kimliklerini model sonucundan ayristiramaz; gercek host tool
gozleminden vermelidir. `child-dispatch`, `structured-result`, `tool-use` ve
`cancellation` acikca bildirilmelidir. Yeni capability adi mevcut istemcileri
otomatik etkinlestirmez, arac kurmaz ve grant uretmez.

Kopru `execute_child(task, timeout_seconds, cancel)` icin host-native cagrisi
yapar. Task'in saglanan evidence-only, network kapali, write resource bos,
tek child/model call ve token/maliyet butcelerini host tarafinda uygular.
Bu Python kontrati tek basina host sandbox'i degildir. Gercek host enforcement
ve usage readback'i kabul kapisidir; fixture bu kanit yerine gecmez.

Her gozlem role, parent/child session, call, actual provider/model, model
catalog digest, capability digest ve input/output digest tasir. Native roller
`researcher` ve `verifier`'dir; OpenCode named agent zorunlulugu yoktur.
Session/call kimlikleri ayri, parent ayni, catalog/capability snapshot'i sabit
olmalidir. Ayni model adi iki ayri execution'i engellemez; yalniz rol etiketi
degistirilmis tek execution bagimsiz verifier sayilmaz.

## Akis ve sonuc

Ilk child'a soru, exact proje/run/source scope'u ve bounded evidence verilir.
Verifier'a ayni evidence ile researcher payload'i ve acceptance subject
digest'leri verilir. Butun finding'ler terminal verdict alir; yabanci citation,
yanlis subject, duplicate veya ayni child session reddedilir. Gec sonuc,
iptal, capability/input mutasyonu ve bozuk result envelope kabul edilmez.

Paylasilan `zekam-research-result/v1` sonucu mevcut citation/report dogrulayicisi
ile islenir. Native execution raporu tam child payload/transcript tasimaz;
kimlik ve digest'leri tasir. Host coordinator'u icin model cagrisi yapilmadigindan
`root_agent_calls=0`, gercek iki tool gozlemi icin `delegated_agent_calls=2`
raporlanir. Model usage `agent_calls` sayar; provider calls/tokens/sure/maliyet
olculmediyse null kalir. Agent sayisi provider istek sayisi diye sunulmaz.

Uretim CLI'ina gercek Codex/Claude native tool koprusunun takilmasi, katalog ve
permission gozleminin hosttan gelmesi ve gercek client kabul matrisi halen
aciktir. Bu dilim yalniz typed entegrasyon ve mevcut canonical runtime bagidir;
canli model/alt ajan cagrisi, kullanici ayari veya kurulum yapilmadi.

Regresyon kaynaklari `tests/unit/test_native_research.py` ve
`tests/unit/test_research_runtime.py`: iki host kind, eksik capability,
yanlis subject/input, ayni session, iptal, oversized evidence, typed result
tamper ve canonical report/replay. Fixture gozlemleri uretim qualification
degildir. Testler CALISTIRILMADI; final toplu asamayi bekler.
