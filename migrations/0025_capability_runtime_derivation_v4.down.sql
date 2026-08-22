create or replace function models.derive_capability_request_body(template_ jsonb,state_ jsonb)
returns jsonb
language sql immutable strict set search_path=pg_catalog,models as $$
    select jsonb_build_object(
        'model',template_->>'model',
        'messages',jsonb_build_array(
            jsonb_build_object('role','system','content',template_->>'system'),
            jsonb_build_object(
                'role','user','content',(template_->>'prompt_prefix') ||
                'prior_state_digest tam olarak ' || models.capability_runtime_jsonb_digest(state_) ||
                ' olmali. Onceki continuity_state:' || chr(10) ||
                models.capability_runtime_canonical_json(state_)
            )
        ),
        'temperature',0,
        'max_tokens',(template_->>'max_tokens')::integer
    )
$$;

create or replace function models.enforce_capability_runtime_continuity() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models as $$
declare slot_record record; expected_prior text; expected_checkpoint uuid; expected_attestation text;
        request_digest text;
begin
    select manifest_id,model_id,task_digest,turn_number,slot_digest,chain_seed_digest,
           request_template,request_template_digest
      into slot_record from models.capability_runtime_approval_slot
     where realm_id=new.realm_id and id=new.slot_id;
    if slot_record.turn_number=1 then
        expected_prior:=slot_record.chain_seed_digest;
        expected_checkpoint:=null;
    else
        select o.result_digest,o.checkpoint_id into expected_prior,expected_checkpoint
          from models.capability_runtime_approval_slot s
          join models.capability_runtime_call_outcome o
            on o.realm_id=s.realm_id and o.slot_id=s.id and o.status='completed'
         where s.realm_id=new.realm_id and s.manifest_id=new.manifest_id
           and s.model_id=slot_record.model_id and s.task_digest=slot_record.task_digest
           and s.turn_number=slot_record.turn_number-1;
    end if;
    request_digest:=models.capability_runtime_jsonb_digest(
        models.derive_capability_request_body(slot_record.request_template,new.continuity_state));
    expected_attestation:=models.capability_runtime_jsonb_digest(jsonb_build_object(
        'schema','zekam-capability-request-derivation/v1',
        'algorithm','zekam-capability-continuity-derive/v3',
        'template_digest',slot_record.request_template_digest,
        'continuity_state_digest',new.continuity_state_digest,
        'request_body_digest',request_digest
    ));
    if slot_record.manifest_id is distinct from new.manifest_id
       or new.continuity_state_digest is distinct from
          models.capability_runtime_jsonb_digest(new.continuity_state)
       or expected_prior is null or new.prior_result_digest is distinct from expected_prior
       or new.checkpoint_id is distinct from expected_checkpoint
       or new.derivation_attestation_digest is distinct from expected_attestation then
        raise exception 'capability runtime continuity digest/prior/checkpoint attestation mismatch'
            using errcode='42501';
    end if;
    return new;
end
$$;

grant execute on function models.derive_capability_request_body(jsonb,jsonb) to zekam_app;
grant execute on function models.enforce_capability_runtime_continuity() to zekam_app;
