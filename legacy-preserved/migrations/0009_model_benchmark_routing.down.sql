drop table if exists models.deliberation_result;
drop table if exists models.runtime_observation;
drop table if exists models.model_decision;
drop table if exists models.model_quota_pool_binding;
drop table if exists models.quota_observation;
drop table if exists models.benchmark_aggregate;
drop table if exists models.benchmark_trial;
drop table if exists models.benchmark_verifier_result;
drop function if exists models.enforce_benchmark_claim_realm();
drop table if exists models.benchmark_plan;
drop table if exists models.benchmark_suite;
drop table if exists models.benchmark_fixture_registry;

delete from core.schema_migrations where version = 9;
