# Yeni makinede başlama

Gereksinimler Python 3.12+ ve Git'tir. Yeni kurulum yerel SQLite operasyon deposunu,
içerik-adresli artifact deposunu ve gerekli dizinleri atomik olarak hazırlar.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[api]"
.venv/bin/zekam init --home /tmp/zekam-home
.venv/bin/zekam db status --home /tmp/zekam-home
.venv/bin/zekam doctor --home /tmp/zekam-home
```

Gerçek kullanıcı kökü yerine önce geçici bir `--home` ile doğrulama yapılması önerilir.
Kurulum uzak veritabanına bağlanmaz, dış ağ çağrısı yapmaz ve başka projelerin verisini
okumaz. Eski yapılandırma alanları görülürse yükleme etkiden önce başarısız olur.

Yedek manifesti için:

```bash
.venv/bin/zekam backup create --home /tmp/zekam-home
```
