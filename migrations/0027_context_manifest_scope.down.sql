-- Geri donus, realm icinde ayni digest'i tasiyan farkli work kayitlari varsa guvenli degildir.

do $$
begin
    if exists (
        select 1
        from work.context_manifest
        group by realm_id, manifest_digest
        having count(*) > 1
    ) then
        raise exception '0027 rollback blocked: cross-work duplicate manifest digest exists';
    end if;
end
$$;

alter table work.context_manifest
    drop constraint context_manifest_unique;

alter table work.context_manifest
    add constraint context_manifest_unique unique (realm_id, manifest_digest);
