revoke execute on function ops.causal_chain(uuid,integer) from zekam_app;
revoke select on ops.causal_node,ops.causal_edge,ops.causal_orphan from zekam_app;
drop function ops.causal_chain(uuid,integer);
drop view ops.causal_orphan;
drop view ops.causal_edge;
drop view ops.causal_node;
