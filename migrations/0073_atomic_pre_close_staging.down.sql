do $migration$
declare
  definition_ text;
  revised_ text;
  baseline_ text := $baseline$and outbox.plan_digest=admission.effect_plan_digest
     and ((continuity_event.event_type='pre_close'
       and outbox.state in ('pending','processing')
       and outbox.terminal_receipt_digest is null and outbox.completed_at is null)
       or (continuity_event.event_type<>'pre_close'
       and outbox.state='completed'
       and outbox.terminal_receipt_digest=admission.terminal_hook_receipt_digest
       and outbox.completed_at is not null))$baseline$;
  replacement_ text := $replacement$and outbox.state='completed'
     and outbox.plan_digest=admission.effect_plan_digest
     and outbox.terminal_receipt_digest=admission.terminal_hook_receipt_digest$replacement$;
begin
  if exists(
    select 1 from continuity.session_lifecycle_event event
    join continuity.lifecycle_delivery_outbox outbox
      on outbox.realm_id=event.realm_id and outbox.event_id=event.id
    join client.codex_lifecycle_admission admission
      on admission.realm_id=event.realm_id
      and admission.continuity_event_id=event.id
    where event.event_type='pre_close'
      and outbox.state in ('pending','processing')
      and outbox.terminal_receipt_digest is null
  ) then
    raise exception '0073 down refused: staged pre-close outbox exists'
      using errcode='55000';
  end if;
  select pg_get_functiondef(
    'client.enforce_codex_lifecycle_admission()'::regprocedure)
    into strict definition_;
  if (length(definition_)-length(replace(definition_,baseline_,'')))
        /length(baseline_)<>1 then
    raise exception '0073 down refused: pre-close admission baseline drift'
      using errcode='55000';
  end if;
  revised_:=replace(definition_,baseline_,replacement_);
  if revised_=definition_ or position(baseline_ in revised_)>0 then
    raise exception '0073 down refused: pre-close admission rewrite incomplete'
      using errcode='55000';
  end if;
  execute revised_;
end $migration$;

comment on function client.enforce_codex_lifecycle_admission() is
  '0072 Codex lifecycle dirty-aware source and immutable plan body admission';
