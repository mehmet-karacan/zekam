drop function if exists diagnostics.purge_trace(uuid,timestamptz,text,text);
drop function if exists diagnostics.close_trace(uuid);
drop trigger if exists diagnostic_trace_memory_candidate_guard on memory.candidate;
drop function if exists diagnostics.reject_direct_memory_promotion();
drop table if exists diagnostics.access_event;
drop function if exists diagnostics.enforce_access_event();
drop function if exists diagnostics.store_reduction(uuid,uuid,integer,text,text,jsonb,timestamptz);
drop table if exists diagnostics.reduction;
drop function if exists diagnostics.enforce_reduction();
drop function if exists diagnostics.expected_reduction_body(uuid);
drop function if exists diagnostics.append_trace_event(
  uuid,uuid,uuid,text,text,timestamptz,jsonb,text,text,text,bigint,bigint,text,text,jsonb,text
);
drop table if exists diagnostics.trace_event;
drop function if exists diagnostics.enforce_trace_event();
drop table if exists diagnostics.payload_ref;
drop table if exists diagnostics.trace_bundle;
drop function if exists diagnostics.enforce_trace_bundle();
drop schema if exists diagnostics;
