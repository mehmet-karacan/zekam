# Zekam Tekil Kimlik Sözleşmesi

## Kanonik yüzey

Ürünün tek etkin kimliği aşağıdaki adlardan oluşur:

- repository, dağıtım, Python package ve CLI: `zekam`
- kullanıcı veri kökü: `ZEKAM_HOME` ve varsayılan `~/.zekam`
- yapılandırma ve kanıt şemaları: `zekam-*`
- PostgreSQL uygulama rolü ve realm setting'i: `zekam_app`, `zekam.realm_id`
- source ignore dosyası: `.zekamignore`

Eski package, CLI, environment, home, schema, evidence, DB role veya setting alias'ı
çalıştırılmaz. Bilinmeyen eski biçimler fail-closed reddedilir.

## Migration geçmişi

Migration kaynakları tekil Zekam kimliğiyle tanımlıdır. Kimlik daraltması geçmiş SQL
checksum'larını değiştirdiği için önceki ledger üstünde sessiz upgrade veya checksum waiver
yapılmaz. Böyle bir kurulum immutable backup'a alınır; fresh Zekam şeması kurulur, kanonik
satırlar doğrulanmış export/import ile taşınır ve UUID/digest alanları byte-for-byte korunur.

## Değişmez tarihsel kayıtlar

UUID, Work/Run/Claim/Receipt kimlikleri, content digestleri ve CAS object digestleri yeniden
üretilmez. Kimlik değişikliği geçmiş state'i yeniden hash'lemek için gerekçe değildir.

## Kabul

- wheel yalnız `zekam` package ve CLI taşır;
- yalnız `ZEKAM_*` locator'ları çözülür;
- fresh PostgreSQL/SQLite kurulumu yalnız Zekam kimliği üretir;
- tracked aktif yüzeylerde eski ürün kimliği bulunmaz;
- full regression, migration up/down/reapply ve DR restore geçmeden release üretilmez.
