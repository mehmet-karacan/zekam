# Uygulama Teslim Sözleşmesi

## Beklenen model davranışı

Bu paket uygulayıcı modele verildiğinde model:

- yalnız plan yazıp durmaz,
- ilk 10 task'ı bitirip kapsamı kapatmaz,
- dış blocker yoksa checkpoint/fallback ile çalışmaya devam eder,
- her phase gate ve Global DoD'yi kod/test/evidence ile kapatır,
- gerektiğinde farklı model/client/subagent kullanır,
- başarısız child sonuçlarını ana state'e kaydeder,
- eski repository kodunu topluca taşımaz,
- dead code ve çelişkili docs bırakmaz.

## Her task teslimi

```text
Work/task revision
exact plan
değişen dosya/migration
subagent result envelope'ları
test/eval commands ve artifacts
verifier verdict
claim/receipt (effect varsa)
risk/rollback
continuity checkpoint
commit (policy gerektiriyorsa)
```

## Phase gate

Phase yalnız:
- bütün mandatory tasks terminal success,
- integration/negative tests,
- schema/migration compatibility,
- docs-code consistency,
- handoff

ile kapanır.

## Final release

`GLOBAL_DEFINITION_OF_DONE.md` ve `kalite/GLOBAL_DOD.yaml` bütün criteria pass olmadan final
release üretilemez. Waiver default yoktur. Gerçek haricî limitation varsa criterion completed
yerine documented blocker olur; ürün “tamamlandı” denmez.

## Uygulayıcı model değişimi

Yeni model yalnız:
- 00_BASLA,
- manifest,
- active Work,
- continuity,
- current plan/step,
- bounded required docs/evidence

ile devam eder. Önceki modelin sohbetini talep etmez.
