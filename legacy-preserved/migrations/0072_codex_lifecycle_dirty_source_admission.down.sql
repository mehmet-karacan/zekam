do $migration$
declare
  definition_ text;
  revised_ text;
  baseline_ text := $baseline$source.revision=(case
       when envelope.source_revision ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
       then substring(envelope.source_revision from 5 for 40)
       else envelope.source_revision end)$baseline$;
  replacement_ text := 'envelope.source_revision=source.revision';
begin
  select pg_get_functiondef(
    'client.enforce_codex_lifecycle_admission()'::regprocedure)
    into strict definition_;
  if (length(definition_)-length(replace(definition_,baseline_,'')))
        /length(baseline_)<>1 then
    raise exception '0072 down refused: dirty source admission baseline drift'
      using errcode='55000';
  end if;
  revised_:=replace(definition_,baseline_,replacement_);
  if revised_=definition_ or position(baseline_ in revised_)>0 then
    raise exception '0072 down refused: dirty source admission rewrite incomplete'
      using errcode='55000';
  end if;
  execute revised_;
end $migration$;

comment on function client.enforce_codex_lifecycle_admission() is
  '0071 Codex lifecycle immutable plan body compatibility and admission guard';
