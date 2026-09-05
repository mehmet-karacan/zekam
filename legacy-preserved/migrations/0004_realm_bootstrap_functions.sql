-- Realm bootstrap fonksiyonlari.
--
-- Sorun: `core.realm` uzerinde FORCE ROW LEVEL SECURITY vardir ve politika
-- `zekam.realm_id` ayarina bakar. Bu durumda uygulama, hangi realm'e baglanacagini
-- bilmeden realm'i arayamaz veya olusturamaz.
--
-- Cozum: dar kapsamli iki SECURITY DEFINER fonksiyonu. Bunlar yalnizca slug ile
-- realm arar veya olusturur; baska hicbir tabloya erisim vermez ve satir icerigi
-- dondurmez (yalnizca kimlik).
--
-- Guvenlik onlemleri:
--   * `search_path` sabitlenir (fonksiyon ele gecirme riskine karsi),
--   * girdi slug bicimi dogrulanir,
--   * yalnizca `zekam_app` rolune execute verilir.
--
-- Geri alma: 0004_realm_bootstrap_functions.down.sql

create or replace function core.find_realm_id(p_slug text)
returns uuid
language plpgsql
stable
security definer
set search_path = core, pg_temp
as $$
declare
    found uuid;
begin
    if p_slug !~ '^[a-z0-9]+(-[a-z0-9]+)*$' then
        raise exception 'gecersiz realm slug bicimi' using errcode = '22023';
    end if;
    select id into found from core.realm where slug = p_slug;
    return found;
end;
$$;

comment on function core.find_realm_id(text) is
    'Slug ile realm kimligi arar. Yalnizca kimlik doner; satir icerigi acmaz.';

create or replace function core.ensure_realm(p_slug text, p_display_name text)
returns uuid
language plpgsql
volatile
security definer
set search_path = core, pg_temp
as $$
declare
    found uuid;
begin
    if p_slug !~ '^[a-z0-9]+(-[a-z0-9]+)*$' then
        raise exception 'gecersiz realm slug bicimi' using errcode = '22023';
    end if;
    if btrim(coalesce(p_display_name, '')) = '' then
        raise exception 'realm gorunen adi bos olamaz' using errcode = '22023';
    end if;

    select id into found from core.realm where slug = p_slug;
    if found is not null then
        return found;
    end if;

    found := gen_random_uuid();
    insert into core.realm (id, slug, display_name) values (found, p_slug, p_display_name);
    return found;
end;
$$;

comment on function core.ensure_realm(text, text) is
    'Realm yoksa olusturur, varsa mevcut kimligi doner. Idempotenttir.';

revoke all on function core.find_realm_id(text) from public;
revoke all on function core.ensure_realm(text, text) from public;

grant execute on function core.find_realm_id(text) to zekam_app;
grant execute on function core.ensure_realm(text, text) to zekam_app;
