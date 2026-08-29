create function runtime.enforce_effect_claim_authorization_once() returns trigger
language plpgsql security definer set search_path=pg_catalog,runtime as $$
begin
  if new.authorization_id is null then return new; end if;
  perform pg_advisory_xact_lock(hashtextextended(new.authorization_id::text,0));
  if exists(select 1 from runtime.effect_claim claim
      where claim.authorization_id=new.authorization_id) then
    raise exception 'effect claim authorization exact one-shot olmali'
      using errcode='23505';
  end if;
  return new;
end $$;
revoke all on function runtime.enforce_effect_claim_authorization_once() from public;
create trigger effect_claim_authorization_once
before insert on runtime.effect_claim
for each row execute function runtime.enforce_effect_claim_authorization_once();
