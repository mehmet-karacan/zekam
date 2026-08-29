grant execute on function runtime.admit_loop_attempt(uuid,uuid,uuid,text,text,text,text,text,text,
  text,text,text,bigint,bigint,bigint,uuid[],text),
  runtime.complete_loop_attempt(uuid,uuid,text,text,uuid,uuid,uuid,bigint,bigint,bigint)
  to zekam_app;
drop function if exists runtime.complete_loop_attempt_current(uuid,uuid,text,text,uuid,uuid,uuid,
  bigint,bigint,bigint,text,text,text,text[],text);
drop function if exists runtime.admit_loop_attempt_current(uuid,uuid,uuid,text,text,text,text,text,
  text,text,text,text,bigint,bigint,bigint,uuid[],text,integer,text,text,text,text,text);
drop function if exists runtime.bind_loop_attempt_job(uuid,integer,uuid,text,uuid,text);
drop function if exists runtime.store_scaffolding_ablation(uuid,uuid,uuid,uuid,jsonb,text,text);
drop function if exists runtime.store_loop_rollback_receipt(uuid,uuid,jsonb,text);
drop function if exists runtime.store_loop_change_set(uuid,uuid,uuid,jsonb,text,text);
drop function if exists runtime.store_tournament_plan(uuid,uuid,uuid,text,text,jsonb,text);
drop function if exists runtime.store_graph_execution_receipt(uuid,uuid,uuid,jsonb,text,boolean);
drop function if exists runtime.store_topology_decision(uuid,uuid,uuid,uuid,jsonb,text,text,jsonb,text);
drop function if exists runtime.store_loop_progress(uuid,uuid,uuid,uuid,uuid,uuid,jsonb,text,
  timestamptz,integer,jsonb,text,boolean,text,integer,text,jsonb,text,text);
drop function if exists runtime.store_measured_loop_contract(uuid,uuid,uuid,jsonb,text,text,
  uuid,uuid,jsonb,text,jsonb,text,integer,integer,integer,double precision);
drop function if exists runtime.assert_measured_payload_safe(jsonb);
drop table if exists runtime.scaffolding_ablation;
drop table if exists runtime.loop_rollback_receipt;
drop table if exists runtime.loop_change_set;
drop table if exists runtime.tournament_plan;
drop table if exists runtime.graph_execution_receipt;
drop table if exists runtime.execution_topology_decision;
drop table if exists runtime.loop_control_event;
drop table if exists runtime.loop_attempt_novelty;
drop table if exists runtime.loop_attempt_job;
drop table if exists runtime.loop_progress_packet;
drop table if exists runtime.measurement_evidence;
drop table if exists runtime.loop_policy_v2;
drop table if exists runtime.validator_asset_manifest;
drop table if exists runtime.optimization_objective;
drop function if exists runtime.record_loop_control_event(uuid,uuid,text,uuid,text,text);
