drop view if exists models.route_capability_evidence;
alter table models.model_route_decision
  drop constraint if exists model_route_decision_capability_requirements,
  drop column if exists capability_evaluator_provenance_digest,
  drop column if exists capability_execution_profile_digest,
  drop column if exists capability_registry_digest,
  drop column if exists capability_suite_digest,
  drop column if exists capability_source_revision,
  drop column if exists capability_evidence_role,
  drop column if exists minimum_long_session_score,
  drop column if exists minimum_long_session_seconds,
  drop column if exists minimum_structured_output_score,
  drop column if exists minimum_tool_score,
  drop column if exists minimum_context_tokens;
alter table models.capability_benchmark_suite
  drop constraint if exists capability_suite_route_dimensions,
  drop column if exists task_route_dimensions;
drop function if exists models.valid_task_route_dimensions(jsonb,text[]);
