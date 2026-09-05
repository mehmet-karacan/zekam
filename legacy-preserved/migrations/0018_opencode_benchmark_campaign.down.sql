drop trigger if exists opencode_qualification_binding
    on models.opencode_model_qualification_event;
drop trigger if exists opencode_campaign_outcome_binding
    on models.opencode_benchmark_campaign_outcome;
drop trigger if exists opencode_campaign_member_result_binding
    on models.opencode_benchmark_campaign_member_result;
drop trigger if exists opencode_campaign_member_plan_binding
    on models.opencode_benchmark_campaign_member_plan;
drop trigger if exists opencode_campaign_member_budget
    on models.opencode_benchmark_campaign_member;
drop trigger if exists opencode_campaign_binding
    on models.opencode_benchmark_campaign;

drop function if exists models.enforce_opencode_qualification();
drop function if exists models.enforce_opencode_campaign_outcome();
drop function if exists models.enforce_opencode_member_result_binding();
drop function if exists models.enforce_opencode_member_plan_binding();
drop function if exists models.enforce_opencode_campaign_member_budget();
drop function if exists models.enforce_opencode_campaign_binding();

drop table if exists models.opencode_model_qualification_event;
drop table if exists models.opencode_benchmark_campaign_outcome;
drop table if exists models.opencode_benchmark_campaign_member_result;
drop table if exists models.opencode_benchmark_campaign_member_plan;
drop table if exists models.opencode_benchmark_campaign_member;
drop table if exists models.opencode_benchmark_campaign;

drop function if exists models.valid_campaign_digest_array(text[]);

delete from core.schema_migrations where version = 18;
