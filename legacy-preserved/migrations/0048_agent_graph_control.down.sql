drop trigger if exists agent_message_no_mutation on agents.message;
drop trigger if exists message_membership on agents.message;
drop trigger if exists child_status_event_no_mutation on agents.child_status_event;
drop trigger if exists child_status_event_guard on agents.child_status_event;
drop trigger if exists spawn_edge_no_delete on agents.spawn_edge;
drop trigger if exists spawn_edge_update on agents.spawn_edge;
drop trigger if exists spawn_edge_binding on agents.spawn_edge;
drop trigger if exists graph_root_no_delete on agents.graph_root;
drop trigger if exists graph_root_update on agents.graph_root;
drop trigger if exists graph_root_binding on agents.graph_root;
drop function if exists agents.enforce_message_membership();
drop function if exists agents.transition_graph_child(uuid,uuid,text,timestamptz,integer,integer,
  bigint);
drop function if exists agents.reserve_spawn_edge(uuid,uuid,uuid,uuid,uuid,integer,integer,
  bigint,text,jsonb,timestamptz);
drop function if exists agents.enforce_child_status_event();
drop function if exists agents.enforce_spawn_edge_update();
drop function if exists agents.enforce_spawn_edge_binding();
drop function if exists agents.enforce_graph_root_update();
drop function if exists agents.enforce_graph_root_binding();
drop table if exists agents.message;
drop table if exists agents.child_status_event;
drop table if exists agents.spawn_edge;
drop table if exists agents.graph_root;
