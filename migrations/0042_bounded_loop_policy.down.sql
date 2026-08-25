drop trigger if exists tool_dispatch_loop_gate on tools.dispatch_gate_evidence;
drop function if exists runtime.enforce_tool_loop_dispatch();
drop trigger if exists model_invocation_loop_gate on models.invocation_attempt;
drop function if exists runtime.enforce_model_loop_dispatch();
drop trigger if exists agent_invocation_loop_gate on agents.invocation;
drop function if exists runtime.enforce_agent_loop_dispatch();
drop function if exists runtime.bind_loop_dispatch(uuid,text,uuid);
drop function if exists runtime.interrupt_loop_attempt(uuid,text);
drop function if exists runtime.complete_loop_attempt(uuid,uuid,text,text,uuid,uuid,uuid,bigint,bigint,bigint);
drop function if exists runtime.admit_loop_attempt(uuid,uuid,uuid,text,text,text,text,text,text,text,
  text,text,bigint,bigint,bigint,uuid[],text);
drop function if exists runtime.register_loop_delta_evidence(uuid,uuid,text,uuid);
drop function if exists runtime.create_loop_policy(uuid,uuid,uuid,uuid,integer,bigint,bigint,
  timestamptz,text,text[],text[],text);
drop table if exists runtime.loop_terminal;
drop table if exists runtime.loop_checkpoint;
drop table if exists runtime.loop_attempt_outcome;
drop table if exists runtime.loop_dispatch_binding;
drop table if exists runtime.loop_attempt_delta;
drop table if exists runtime.loop_attempt;
drop table if exists runtime.loop_delta_evidence;
drop table if exists runtime.loop_policy;
drop function if exists runtime.loop_effect_class(text);
