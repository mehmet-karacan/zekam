# Native / ince akış paired değerlendirme planı (çalıştırılmadı)

Durum: **plan**. Hiçbir canlı CLI/model koşusu yapılmadı; bu belge sonuçlardan önce sabitlenmiş
eşikleri ve kapsamı kayda geçirir. Kalite artışı iddiası ölçüm yapılana kadar kurulamaz.

## Karşılaştırma kolları

| Kol | Tanım |
|---|---|
| A (ağır akış) | 4 Ekim 2026 kaynağı (`f37ab30`): coordinator default agent, geniş `allow`, eager `00_BASLA`/`DEVAM_PROTOKOLU`, zorunlu child |
| B (native/ince) | Bu görevin çıktısı: ince `AGENTS.md`, hook/coordinator yok |
| C (opsiyonel minimal referans) | Yalnız `AGENTS.md` içindeki yetki/kanıt sınırı |

Aynı görev, kaynak snapshot'ı, CLI+model sürümü, araç erişimi, güvenlik sınırı, bütçe ve kabul
ölçütü kullanılır. Gerçek kullanıcı home'u kullanılmaz; her kol izole sentetik ortamda (ayrı
`ZEKAM_HOME` ve sentetik native home) koşar. Eski global ayarlar gerçek home'a geri kurulmaz.

## Senaryolar (küçük ama ayırt edici)

1. Genel soru (proje içermeyen kavram sorusu).
2. Küçük source düzeltmesi (tek dosya, test ile doğrulanır).
3. Çok dosyalı inceleme (kanıtlı bulgu listesi).
4. Devam/recovery (`continuity native` checkpoint → başka istemcide resume).
5. Gerçek unit-test ölçümü (`test measure`, sentetik Maven fixture).

## Önceden sabitlenen ölçütler

- Birincil: sonuç kalitesi (görev kabul ölçütü) ve güvenlik ihlali sayısı (sıfır tolerans).
- İkincil: gereksiz tool/subagent çağrısı, effective context yükü, süre, maliyet.
- Eşikler mevcut `ScaffoldingAblationPolicy` varsayılanıdır (tüm toleranslar `0.0`):
  `scaffolding.quality-no-regression`, `reliability-no-regression`, `latency-budget`,
  `token-budget`, `cost-budget` kapıları B kolu için A'ya karşı geçmelidir; biri başarısızsa
  karar `keep-scaffolding` kalır. Eşikler sonuç görüldükten sonra değiştirilmez.
- Senaryo başına tekrar sayısı: **5** (önceden sabit, sonuca göre artırılmaz); tek başarılı
  örnekten üstünlük çıkarılmaz.
- Token azalırken doğru görev/skill'in bulunamaması veya kritik güvenlik bilgisinin kaybı
  başarı sayılmaz.
- Exact tokenizer yoksa byte/karakter ölçüsü token diye sunulmaz; ölçülemeyen latency/maliyet
  için sıfır yazılmaz (`unobservable`).

## Effective context ayrıştırması

`measure_instruction_load` (salt okunur) her istemci için global talimat, kök/ancestor dosyası,
eager import, `opencode.json` `instructions`, skill metadata (kaynak sınıfı + digest + yüklenme
nedeni) katkılarını ayırır; gizli system prompt ve tool serialization `unobservable` kalır, yani
sonuç tam etkin yük değil **keşfedilen kaynakların modelidir**. `duplicate_skill_discovery`
aynı skill adının birden çok keşif dizininde bulunmasını raporlar.

## Yetki

Canlı CLI/model çağrısı ve kurulum, mevcut açık yetki kapsamı dışında çalıştırılmaz. Yetki
yoksa provider-free testler tamamlanır ve ilgili satır `not-run` /
`authorization-required` olarak kalır.
