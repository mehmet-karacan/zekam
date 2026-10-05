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

## On olcum (provider-free, yalniz boyut; 5 Ekim 2026)

A kolu = `f37ab30` giris dosyalari (sentetik, repo disi gecici dizinde), B kolu = guncel kok.
`measure_instruction_load` ile keşfedilen proje girişi (instruction + eager import/config
instructions); OpenCode A'da `default_agent` coordinator ajan govdesi de eklendi. Birim bayt;
"token" sutunu `ceil(bayt/4)` kaba tahminidir, gercek tokenizer degildir.

| Istemci | A (bayt) | B (bayt) | Not |
|---|---|---|---|
| OpenCode | 23040 (AGENTS + 00_BASLA + DEVAM + coordinator) | 2800 | A'da config `instructions` ve default agent zorunlu yukluyordu |
| Codex | 3368 | 2800 | A'da yalniz AGENTS.md |
| Claude Code | 0 (kokte CLAUDE.md yok; `2.1.268` AGENTS.md'yi dogrudan okumaz) | 2820 | B'de uyumluluk dosyasi + import |

Bu yalniz **yuk** olcumudur: kalite, guvenlik ihlali, dogru gorev/skill bulma olculmedi, bu yuzden
"daha iyi" iddiasi yoktur. Claude A=0, A'nin Claude'da Zekam baglami hic yuklemedigini; B'nin
bunu bilincli olarak eklediğini gosterir (yuk artisi). Canli kolay-karsilastirma senaryolari
(5 gorev x 5 tekrar) calistirilmadi; yetki ve butce ayri.

## Sonuclar (canli, 5 Ekim 2026; OpenCode 1.18.34 + `GLM-5.3-Flash-IT`)

Calistirma: `benchmarks/native_paired_eval.py` (plan: 3 senaryo x 5 tekrar x 2 kol = 30 kosu, seed
20261005, tolerans 0, timeout 420 s; sentetik dizinler, izole `ZEKAM_HOME`). Ham sonuc:
`NATIVE_DEGERLENDIRME_SONUC.json` (model metni yok). Senaryolar: S1 genel soru, S2 kucuk kaynak
duzeltmesi (pytest ile dogrulanir), S3 cok dosyali inceleme (3 tohumlanmis sorun; kod
degistirilmemeli). S4 devam/recovery ve S5 unit-test olcumu **kosulmadi** (model-bagimsiz /
provider-free yollar; eski kolla karsilastirilabilir degil).

| Olcut | A (agir akis) | B (native/ince) |
|---|---|---|
| Kosu / tamamlanan | 15 / 14 | 15 / 14 |
| Kalite (normalize, hangler 0) | %73,3 (S1 4/5, S2 5/5, S3 6/15) | %93,3 (S1 5/5, S2 4/5, S3 15/15) |
| Guvenlik ihlali | 0 | 0 |
| Ort. toplam token (tamamlanan) | 60 561 | 43 403 |
| Ort. gecikme / medyan | 109,5 s / 76,1 s | 39,0 s / 40,2 s |
| Subagent (`task`) cagrisi iceren kosu | 6 | 0 |
| Maliyet | saglayici 0 raporladi (olculemedi) | idem |

Her kolda bir kosu takildi (A-S1-5 >500 s ciktisiz; B-S2-4 2118 s) ve basarisiz sayildi; harness'in
timeout'u torun sureci oldurmedigi icin ikisi elle sonlandirildi (script sonradan agac
oldurme ile duzeltildi). Senaryo bazinda tamamlananlarda: S1 B daha az token, S2 B ~%41 daha az
token ve ~3,5x daha hizli, S3 B **daha fazla** token (55,9k vs 39,6k) ve tool kullandi: A'da
koordinator isi alt ajana devredip yuzeysel yanit verdi (yalniz 6/15 sorun), B dosyalari okuyup 15/15
buldu — token farki A'nin daha az is yapmasindan gelir, verimlilik degil.

Onceden sabitlenen kapilar (tolerans 0), B vs A: kalite (%93,3 >= %73,3) gecti; guvenilirlik (14/15
= 14/15) gecti; gecikme gecti; token (ortalama) gecti; maliyet olculemedi. Bu, eski rol
kisitini kaldirmanin **bu sentetik kurulumda** kaliteyi dusurmedigini gosterir.

Sinirlar: n=5/hucre, tek model, anahtar-kelime tabanli puanlama (bagimsiz degil), S2'de B 4/5
(1 takilma), iki takilma nedeni cozumlenmedi, 3/5 senaryo, tek makine/OS. Evrensel ustunluk veya
Codex/Claude sonucu cikarilamaz.
