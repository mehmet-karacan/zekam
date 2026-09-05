drop view if exists projects.routing_context_current_status;

drop table if exists models.model_route_candidate;
drop table if exists models.model_route_decision;
drop table if exists models.model_routing_qualification;
drop table if exists models.routing_suite_binding;
drop table if exists models.execution_target_snapshot;
drop table if exists models.routing_role_policy;
drop table if exists projects.routing_context_snapshot;

drop function if exists models.enforce_route_candidate();
drop function if exists models.enforce_route_decision();
drop function if exists models.enforce_routing_qualification();
drop function if exists models.enforce_routing_suite_binding();
drop function if exists projects.enforce_routing_context_binding();
drop function if exists models.valid_routing_digest_array(text[]);
drop function if exists models.valid_routing_text_array(text[]);
