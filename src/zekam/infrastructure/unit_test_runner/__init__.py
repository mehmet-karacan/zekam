"""Java/Maven unit-test calistirici ve rapor okuyuculari (W04).

Alt moduller: ``safe_xml`` (sinirli, aga/DTD/entity cozumlemeyen XML), ``jacoco_report``,
``surefire_report``, ``pom_inspect`` (statik kesif), ``maven_plan`` (arac dogrulama ve
plan), ``maven_runner`` (explicit argv, kilit, surec agaci) ve ``measurement`` (taze
rapor toplama). Gercek Maven kosusu bu paketin testlerinde yoktur; testler fake-tool ve
replay fixture'lari kullanir.
"""
