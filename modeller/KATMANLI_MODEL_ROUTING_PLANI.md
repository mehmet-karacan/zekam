# Katmanli model ve subagent routing plani

Bu belge OpenCode/AIHub kampanyasindan sonra uygulanacak routing v2 sozlesmesidir.
Plan bilgi tasir; authority, provider cagrisi veya DB mutation izni vermez.

## Hedef

Model secimi uc kanit katmaninin kesisiminden yapilir:

1. `general`: modelin genel kalite, guvenilirlik ve maliyet kaniti,
2. `workload-technology`: React, TypeScript, JavaScript, Java, Oracle/PLSQL,
   test, research ve benzeri exact workload/teknoloji kaniti,
3. `project`: projenin current source ve kurallarina bagli micro-suite kaniti.

Proje bagli route; project/source revision/tree, capability profile, dependency ve lock
digest'i, exact framework surumleri, teknoloji profili, mimari/kural seti, suite digest,
verifier provenance, current inventory/policy ve fresh health/qualification girdilerinin
tamamina baglanir. Biri degisirse eski sonuc append-only kalir fakat `stale` sayilir.
Kanit yoksa uzmanlik tahmin edilmez; sonuc `pending` veya policy'nin acik fallback'idir.

## Roller

- `implementer`: proje/workload uygulama yetkinligi,
- `reviewer`: implementer'dan model ve execution identity olarak bagimsiz,
- `researcher`: retrieval, citation ve long-context kaniti,
- `verifier`: tested/implementer modelden ve execution identity'den bagimsiz.

Primary ve fallback yalniz fresh health + current qualification tasiyan modellerden
secilir. Fallback scope'u sessizce genisletemez.

## Maliyet siniri

Tum modeller her projede yeniden benchmark edilmez. Current health/qualification ve
general sonucu ile baslayan ucuz filtre, workload/technology Pareto/top-K adaylarini
secer; project micro-suite yalniz kalan ilgili adaylarda calisir.

## Kalici sema — uygulanan migration 0021

`0021_layered_model_routing.sql` asagidaki append-only yapilari ekler:

- `projects.routing_context_snapshot`: source/profile/dependency/framework/technology/
  architecture/rules context digest'i,
- `models.routing_role_policy`: rol, zorunlu katmanlar, bagimsizlik, top-K, maliyet ve
  fallback policy'si,
- `models.execution_target_snapshot`: client/slot, native parallel veya fallback modu,
  model secilebilirligi, structured-result/cancellation ve gercek concurrency/cost kaniti,
- v2 suite binding: layer, role, workload, technology ve project context baglari,
- `models.model_routing_qualification`: model+layer+role+suite+aggregate+metrics+
  independent-verifier provenance,
- `models.model_route_decision` ve normalized candidate ledger: primary, fallback,
  rejection nedenleri ve exact evidence seti,
- current/stale view: HEAD, dependency, framework, architecture, rules, suite, health,
  inventory, policy ve expiry drift nedenleri.

Legacy `models.model_decision` okunabilir kalir; routing v2 kaniti sayilmaz.

## Servis ve CLI

Domain tipleri: `RoutingLayer`, `AgentRole`, `ProjectRoutingContext`,
`RoleRoutingPolicy`, `RoutingQualification`, `LayerCandidateEvidence`,
`LayeredModelDecision`.

Servisler project context hazirlama, qualification adoption ve katmanli
preview/decide/resolve akisini uygular.

```text
zekam model route prepare|preview|decide|resolve|explain|status|handoff
```

`preview/status/resolve/explain/handoff` salt okunurdur. `prepare/decide --uygula`
yazarsa exact Work + TaskPlan + DB_WRITE authorization, claim-before-write, terminal
receipt ve checkpoint zorunludur. Eksik benchmark `pending` karar kaydeder; provider
cagrisi baslatmaz.

## Kabul testleri

- general → workload/technology → project intersection ve top-K,
- dort rolun bagimsizliklari, kanit yokken fail-closed ve explicit fallback,
- HEAD/dependency/framework/rules/suite/health/policy drift stale nedenleri,
- local SQLite bootstrap/reopen/rebuild, append-only ve digest-forgery,
- disposable-home CLI success/fallback/no-candidate/drift/replay,
- exact authorization/claim/receipt/checkpoint ve secret-free evidence,
- Ruff, strict mypy, paket validator ve full pytest.

## Rollback

Migration 0021 ancak bagimli v2 kaydi yoksa exact authorized down ile geri alinir. Kayit
varsa silme yerine forward corrective migration kullanilir. V2 tamamlanmadan legacy
sonuc fresh v2 route gibi sunulmaz.
