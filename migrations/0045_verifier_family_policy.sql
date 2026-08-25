-- Risk-bound verifier decisions persist reviewed model-family independence evidence.

alter table models.model_route_decision
  add column risk text not null default 'medium',
  add column family_policy_digest text,
  add column excluded_model_families text[] not null default '{}',
  add constraint model_route_decision_family_policy check (
    risk in ('low','medium','high','critical')
    and (family_policy_digest is null
      or family_policy_digest ~ '^sha256:[0-9a-f]{64}$')
    and models.valid_routing_text_array(excluded_model_families)
    and (cardinality(excluded_model_families)=0 or family_policy_digest is not null)
  );
