-- Authorization birden fazla reviewed effect claim'i kapsayabilir.
-- One-job-per-attempt invarianti measured loop orchestration binding'inde uygulanir;
-- global authorization tekilligi mevcut campaign/benchmark receipt zincirini bozar.

drop trigger if exists effect_claim_authorization_once on runtime.effect_claim;
drop function if exists runtime.enforce_effect_claim_authorization_once();
