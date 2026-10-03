---
name: unit-test-engineering
description: Kanit tabanli Java unit-test ve coverage dongusunu exact plan, test-only patch, bagimsiz kabul ve durable ledger ile yonetir.
license: Proprietary
compatibility: Zekam local runtime, Maven/JaCoCo ve operational SQLite unit-test ledger
metadata:
  author: Zekam
  lifecycle: reviewed
---

# Unit Test Engineering

Bu skill, dogal dildeki unit-test yazma istegini once provider-free bir plana cevirir.
Plan; exact kaynak dosyalarini, coverage metrik/politikasini, tam esigi, attempt/sure
butcesini ve istenen model rotasini gorunur tasir. Plan tek basina yetki vermez.

`zekam test run` yalniz exact plan digest'i, ayri calistirma yetkisi ve yerel runtime
hazirligi birlikte kanitlandiginda effect baslatabilir. Maven/JaCoCo olcumu harness'in
sorumlulugundadir; model coverage sayaci veya basari iddia edemez. Production dosyalari,
POM/coverage ayarlari ve kullanici dirty dosyalari test-only policy ile korunur.

`status` ve `report` operational unit-test ledger'ini salt okunur okur. Receipt'siz
terminal basari sayilmaz. Kesinti, pause, resume ve cancel durumlari durable control
composition'i olmadan sahte terminal veya basari uretemez.
