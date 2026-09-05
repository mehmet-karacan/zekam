# Geliştirme kurulumu

Zekam çekirdeği Python 3.12+, Git ve yerel dosya sistemini kullanır. Ayrı bir veri
sunucusu veya container çalışma zamanı gerekmez.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[api,dev]"
.venv/bin/zekam init --home /tmp/zekam-home
.venv/bin/zekam doctor --home /tmp/zekam-home
```

Windows üzerinde `bin` yerine `Scripts` kullanılır. Testler geçici dizinlerde çalışır:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy src/zekam
```

Kalıcı kullanıcı kökü yalnız açıkça verilen `--home` veya `ZEKAM_HOME` ile seçilir.
Varsayılan ağ politikası kapalıdır; dış sağlayıcı denemeleri ayrı ve açık izin ister.
