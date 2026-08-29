-- Stop/pre_close is staged by the lifecycle job and terminalized only by the
-- separate projection-aware close receipt.  All other lifecycle deliveries
-- retain their existing terminal hook-receipt contract.

do $migration$
declare
  definition_ text;
  revised_ text;
  baseline_ text := $baseline$and outbox.state='completed'
     and outbox.plan_digest=admission.effect_plan_digest
     and outbox.terminal_receipt_digest=admission.terminal_hook_receipt_digest$baseline$;
  replacement_ text := $replacement$and outbox.plan_digest=admission.effect_plan_digest
     and ((continuity_event.event_type='pre_close'
       and outbox.state in ('pending','processing')
       and outbox.terminal_receipt_digest is null and outbox.completed_at is null)
       or (continuity_event.event_type<>'pre_close'
       and outbox.state='completed'
       and outbox.terminal_receipt_digest=admission.terminal_hook_receipt_digest
       and outbox.completed_at is not null))$replacement$;
begin
  if exists(
    select 1 from continuity.session_lifecycle_event event
    join continuity.lifecycle_delivery_outbox outbox
      on outbox.realm_id=event.realm_id and outbox.event_id=event.id
    where event.event_type='pre_close' and outbox.state='completed'
      and not exists(
        select 1 from continuity.session_close_receipt close_receipt
        where close_receipt.realm_id=outbox.realm_id
          and close_receipt.receipt_digest=outbox.terminal_receipt_digest
          and close_receipt.project_id=event.project_id
          and close_receipt.work_item_id=event.work_item_id
          and close_receipt.run_id=event.run_id
          and close_receipt.session_id=event.session_id
          and close_receipt.client_id=event.client_id
          and close_receipt.close_status='closed'
          and close_receipt.receipt_body->>'status'='closed'
          and close_receipt.receipt_body->>'receipt_id'=close_receipt.id::text
          and close_receipt.receipt_body->>'realm_id'=close_receipt.realm_id::text
          and close_receipt.receipt_body->>'project_id'=event.project_id::text
          and close_receipt.receipt_body->>'work_item_id'=event.work_item_id::text
          and close_receipt.receipt_body->>'run_id'=event.run_id::text
          and close_receipt.receipt_body->>'session_id'=event.session_id
          and close_receipt.receipt_body->>'client_id'=event.client_id
          and close_receipt.receipt_digest=continuity.jsonb_digest(
            close_receipt.receipt_body)
          and exists(
            select 1 from work.completion_admission completion
            where completion.realm_id=close_receipt.realm_id
              and completion.close_receipt_id=close_receipt.id
              and completion.pre_close_outbox_id=outbox.id
              and completion.mode='projection-aware'
              and completion.operation='projection-aware-close'
              and completion.consumed_at is not null
          )
      )
  ) then
    raise exception '0073 refused: legacy terminal pre-close lacks close receipt'
      using errcode='55000';
  end if;
  select pg_get_functiondef(
    'client.enforce_codex_lifecycle_admission()'::regprocedure)
    into strict definition_;
  if (length(definition_)-length(replace(definition_,baseline_,'')))
        /length(baseline_)<>1
      or position(replacement_ in definition_)>0 then
    raise exception '0073 refused: pre-close admission baseline drift'
      using errcode='55000';
  end if;
  revised_:=replace(definition_,baseline_,replacement_);
  if revised_=definition_ or position(replacement_ in revised_)=0 then
    raise exception '0073 refused: pre-close admission rewrite incomplete'
      using errcode='55000';
  end if;
  execute revised_;
end $migration$;

comment on function client.enforce_codex_lifecycle_admission() is
  '0073 Codex pre-close staged outbox and immutable lifecycle admission';
