-- Research raporunun domain kimligini ve digest'e giren exact govdesini sakla.
alter table research.report
  add column domain_report_id text,
  add column report_body jsonb;

alter table research.report
  add constraint report_roundtrip_pair check (
    (domain_report_id is null and report_body is null)
    or (domain_report_id is not null and report_body is not null)
  ),
  add constraint report_body_object check (
    report_body is null or jsonb_typeof(report_body) = 'object'
  ),
  add constraint report_domain_id_unique unique (realm_id, domain_report_id);

comment on column research.report.report_body is
  'Digest-bound canonical ResearchReport body; ZEKAM_HOME files are derived projections.';
