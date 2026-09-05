alter table research.report drop constraint if exists report_domain_id_unique;
alter table research.report drop constraint if exists report_body_object;
alter table research.report drop constraint if exists report_roundtrip_pair;
alter table research.report drop column if exists report_body;
alter table research.report drop column if exists domain_report_id;
