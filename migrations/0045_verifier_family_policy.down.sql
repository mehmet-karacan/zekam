alter table models.model_route_decision
  drop constraint if exists model_route_decision_family_policy,
  drop column if exists excluded_model_families,
  drop column if exists family_policy_digest,
  drop column if exists risk;
