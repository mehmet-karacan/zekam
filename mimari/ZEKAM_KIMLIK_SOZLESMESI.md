# Zekam Tekil Kimlik Sözleşmesi

## Kanonik yüzey

Ürünün tek etkin kimliği aşağıdaki adlardan oluşur:

- repository, dağıtım, Python package ve CLI: `zekam`
- kullanıcı veri kökü environment anahtarı: `ZEKAM_HOME`
- varsayılan kullanıcı veri kökü: `~/.zekam`
- operational schema ailesi: `zekam-local-operational/v1`
- knowledge generation ailesi: `zekam-local-knowledge-index/*`
- analytics generation ailesi: `zekam-local-analytics/*`
- yapılandırma, receipt ve evidence şemaları: `zekam-*`
- source ignore dosyası: `.zekamignore`

Eski package, CLI, environment, home, schema, evidence, DB role veya setting alias'ı
çalıştırılmaz. Bilinmeyen eski biçimler fail-closed reddedilir.

## Store kimliği

Operational authority CPython SQLite schema v1 ile fresh bootstrap edilir. Knowledge
index SQLite FTS5 + sqlite-vec generation kimliği taşır ve source manifestten yeniden
üretilebilir. DuckDB yalnız raw event/benchmark artifact'larından yeniden kurulan derived
analytics generation'dır.

Legacy server veritabanı kimliği yeni sistem için migration head, bootstrap input,
compatibility alias veya fallback değildir. Bağlantı, dump, export/import, ETL ve veri
karşılaştırması yapılmaz.

## Değişmez kimlikler

Project, Work, Run, Claim, Receipt, CAS object, source revision, model revision,
benchmark result ve policy activation kimlikleri kendi kanonik gövdelerinin digestlerine
bağlıdır. Kimlikler yeniden adlandırma veya projection kolaylığı için tekrar hashlenmez.

Portable kayıtlarda mutlak cihaz yolu bulunmaz. Project ID, logical source binding,
repository-relative locator ve content digest kullanılır.

## Kabul

- wheel ve sdist yalnız `zekam` package/CLI kimliğini taşır;
- yalnız `ZEKAM_*` locator'ları çözülür;
- fresh bootstrap yalnız schema v1 yerel Zekam kimliklerini üretir;
- package, CLI, environment, home, schema ve store alias taraması eski aktif kimlik bırakmaz;
- index/projection rebuild aynı generation ve aggregate digestlerini yeniden üretebilir;
- backup/restore identity, manifest, mode ve semantic fingerprint doğrulaması yapar;
- push kullanıcı açıkça istemedikçe yapılmaz.
