"""Dormant operational-v4 native lifecycle schema.

This module deliberately contains DDL only.  It grants no writer, decoder,
hook, process, or runtime authority.
"""

# ruff: noqa: E501

SCHEMA_V4_SQL = r"""
create table continuity_hook_attachment (
    attachment_id text primary key check(
        length(attachment_id)=36 and substr(attachment_id,9,1)='-'
        and substr(attachment_id,14,1)='-' and substr(attachment_id,19,1)='-'
        and substr(attachment_id,24,1)='-'
        and replace(attachment_id,'-','') not glob '*[^0-9a-f]*'
    ),
    session_id text not null unique references continuity_session_binding(session_id),
    client_contract_digest text not null,
    native_artifact_digest text not null,
    hook_set_digest text not null,
    attachment_digest text not null unique,
    body_json text not null check(json_valid(body_json) and length(cast(body_json as blob))<=32768),
    created_at text not null,
    check(length(client_contract_digest)=71 and client_contract_digest glob 'sha256:[0-9a-f]*'
        and substr(client_contract_digest,8) not glob '*[^0-9a-f]*'),
    check(length(native_artifact_digest)=71 and native_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(native_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(hook_set_digest)=71 and hook_set_digest glob 'sha256:[0-9a-f]*'
        and substr(hook_set_digest,8) not glob '*[^0-9a-f]*'),
    check(length(attachment_digest)=71 and attachment_digest glob 'sha256:[0-9a-f]*'
        and substr(attachment_digest,8) not glob '*[^0-9a-f]*')
) strict;

create table continuity_managed_process_receipt (
    receipt_digest text primary key check(length(receipt_digest)=71
        and receipt_digest glob 'sha256:[0-9a-f]*'
        and substr(receipt_digest,8) not glob '*[^0-9a-f]*'),
    attachment_id text not null references continuity_hook_attachment(attachment_id),
    predecessor_process_generation_digest text references continuity_hook_process_generation(process_generation_digest),
    native_pid integer not null check(native_pid>0),
    native_uid integer not null check(native_uid>=0),
    native_start_token text not null check(length(native_start_token) between 1 and 512),
    native_artifact_digest text not null,
    hook_set_digest text not null,
    ancestry_policy_digest text not null,
    transition_kind text not null check(transition_kind in
        ('initial-attach','orderly-reattach','recovery-reattach')),
    body_json text not null check(json_valid(body_json) and length(cast(body_json as blob))<=32768),
    created_at text not null,
    check(length(native_artifact_digest)=71 and native_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(native_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(hook_set_digest)=71 and hook_set_digest glob 'sha256:[0-9a-f]*'
        and substr(hook_set_digest,8) not glob '*[^0-9a-f]*'),
    check(length(ancestry_policy_digest)=71 and ancestry_policy_digest glob 'sha256:[0-9a-f]*'
        and substr(ancestry_policy_digest,8) not glob '*[^0-9a-f]*')
) strict;

create table continuity_hook_process_generation (
    process_generation_digest text primary key check(
        length(process_generation_digest)=71 and process_generation_digest glob 'sha256:[0-9a-f]*'
        and substr(process_generation_digest,8) not glob '*[^0-9a-f]*'
    ),
    attachment_id text not null references continuity_hook_attachment(attachment_id),
    generation integer not null check(generation>0),
    native_pid integer not null check(native_pid>0),
    native_uid integer not null check(native_uid>=0),
    native_start_token text not null check(length(native_start_token) between 1 and 512),
    native_artifact_digest text not null,
    hook_set_digest text not null,
    ancestry_policy_digest text not null,
    previous_process_generation_digest text references continuity_hook_process_generation(process_generation_digest),
    managed_launch_receipt_digest text not null unique references continuity_managed_process_receipt(receipt_digest),
    body_json text not null check(json_valid(body_json) and length(cast(body_json as blob))<=32768),
    created_at text not null,
    unique(attachment_id,generation),
    check((generation=1)=(previous_process_generation_digest is null)),
    check(length(native_artifact_digest)=71 and native_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(native_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(hook_set_digest)=71 and hook_set_digest glob 'sha256:[0-9a-f]*'
        and substr(hook_set_digest,8) not glob '*[^0-9a-f]*'),
    check(length(ancestry_policy_digest)=71 and ancestry_policy_digest glob 'sha256:[0-9a-f]*'
        and substr(ancestry_policy_digest,8) not glob '*[^0-9a-f]*')
) strict;

create table continuity_hook_recovery_case (
    recovery_case_id text primary key check(
        length(recovery_case_id)=36 and substr(recovery_case_id,9,1)='-'
        and substr(recovery_case_id,14,1)='-' and substr(recovery_case_id,19,1)='-'
        and substr(recovery_case_id,24,1)='-'
        and replace(recovery_case_id,'-','') not glob '*[^0-9a-f]*'
    ),
    attachment_id text not null references continuity_hook_attachment(attachment_id),
    session_id text not null references continuity_session_binding(session_id),
    process_generation_digest text not null references continuity_hook_process_generation(process_generation_digest),
    case_kind text not null check(case_kind in
        ('process-drift','compaction-correlation','source-drift','transaction-unknown','managed-receipt-missing')),
    evidence_digest text not null check(length(evidence_digest)=71
        and evidence_digest glob 'sha256:[0-9a-f]*'
        and substr(evidence_digest,8) not glob '*[^0-9a-f]*'),
    body_json text not null check(json_valid(body_json) and length(cast(body_json as blob))<=32768),
    created_at text not null
) strict;

create table continuity_hook_recovery_resolution (
    resolution_id text primary key check(
        length(resolution_id)=36 and substr(resolution_id,9,1)='-'
        and substr(resolution_id,14,1)='-' and substr(resolution_id,19,1)='-'
        and substr(resolution_id,24,1)='-'
        and replace(resolution_id,'-','') not glob '*[^0-9a-f]*'
    ),
    recovery_case_id text not null unique references continuity_hook_recovery_case(recovery_case_id),
    outcome text not null check(outcome in ('restored','closed','abandoned')),
    evidence_digest text not null check(length(evidence_digest)=71
        and evidence_digest glob 'sha256:[0-9a-f]*'
        and substr(evidence_digest,8) not glob '*[^0-9a-f]*'),
    body_json text not null check(json_valid(body_json) and length(cast(body_json as blob))<=32768),
    created_at text not null
) strict;

create table continuity_reviewed_hook_command (
    command_digest text primary key check(length(command_digest)=71
        and command_digest glob 'sha256:[0-9a-f]*'
        and substr(command_digest,8) not glob '*[^0-9a-f]*'),
    attachment_id text not null references continuity_hook_attachment(attachment_id),
    external_event_type text not null check(
        external_event_type in ('SessionStart','PreCompact','PostCompact')),
    topology text not null check(
        topology='native-fork-shell-exec-launcher-exec-runtime/v1'),
    client_contract_digest text not null,
    hook_set_digest text not null,
    shell_artifact_digest text not null,
    python_launcher_artifact_digest text not null,
    python_runtime_artifact_digest text not null,
    argv_recipe_digest text not null,
    sandbox_profile_digest text not null,
    body_json text not null check(json_valid(body_json)
        and length(cast(body_json as blob)) between 1 and 32768),
    created_at text not null,
    grants_authority integer not null default 0 check(grants_authority=0),
    approval_inherited integer not null default 0 check(approval_inherited=0),
    unique(attachment_id,external_event_type),
    check(length(client_contract_digest)=71
        and client_contract_digest glob 'sha256:[0-9a-f]*'
        and substr(client_contract_digest,8) not glob '*[^0-9a-f]*'),
    check(length(hook_set_digest)=71 and hook_set_digest glob 'sha256:[0-9a-f]*'
        and substr(hook_set_digest,8) not glob '*[^0-9a-f]*'),
    check(length(shell_artifact_digest)=71
        and shell_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(shell_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(python_launcher_artifact_digest)=71
        and python_launcher_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(python_launcher_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(python_runtime_artifact_digest)=71
        and python_runtime_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(python_runtime_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(argv_recipe_digest)=71 and argv_recipe_digest glob 'sha256:[0-9a-f]*'
        and substr(argv_recipe_digest,8) not glob '*[^0-9a-f]*'),
    check(length(sandbox_profile_digest)=71
        and sandbox_profile_digest glob 'sha256:[0-9a-f]*'
        and substr(sandbox_profile_digest,8) not glob '*[^0-9a-f]*')
) strict;

create table continuity_hook_invocation_ancestry_receipt (
    receipt_digest text primary key check(length(receipt_digest)=71
        and receipt_digest glob 'sha256:[0-9a-f]*'
        and substr(receipt_digest,8) not glob '*[^0-9a-f]*'),
    process_generation_digest text not null
        references continuity_hook_process_generation(process_generation_digest),
    delivery_id text not null unique check(length(cast(delivery_id as blob)) between 1 and 512),
    topology text not null check(
        topology='native-fork-shell-exec-launcher-exec-runtime/v1'),
    launch_command_digest text not null
        references continuity_reviewed_hook_command(command_digest)
        check(length(launch_command_digest)=71
        and launch_command_digest glob 'sha256:[0-9a-f]*'
        and substr(launch_command_digest,8) not glob '*[^0-9a-f]*'),
    external_event_type text not null check(
        external_event_type in ('SessionStart','PreCompact','PostCompact')),
    ancestry_policy_digest text not null check(length(ancestry_policy_digest)=71
        and ancestry_policy_digest glob 'sha256:[0-9a-f]*'
        and substr(ancestry_policy_digest,8) not glob '*[^0-9a-f]*'),
    native_pid integer not null check(native_pid>0),
    native_start_token text not null check(length(cast(native_start_token as blob)) between 1 and 512),
    native_uid integer not null check(native_uid>=0),
    native_artifact_digest text not null,
    shell_pid integer not null check(shell_pid>0),
    shell_start_token text not null check(length(cast(shell_start_token as blob)) between 1 and 512),
    shell_uid integer not null check(shell_uid>=0),
    shell_parent_pid integer not null check(shell_parent_pid>0),
    shell_parent_start_token text not null
        check(length(cast(shell_parent_start_token as blob)) between 1 and 512),
    shell_parent_uid integer not null check(shell_parent_uid>=0),
    shell_artifact_digest text not null,
    hook_pid integer not null check(hook_pid>0),
    hook_start_token text not null check(length(cast(hook_start_token as blob)) between 1 and 512),
    hook_uid integer not null check(hook_uid>=0),
    hook_parent_pid integer not null check(hook_parent_pid>0),
    hook_parent_start_token text not null
        check(length(cast(hook_parent_start_token as blob)) between 1 and 512),
    hook_parent_uid integer not null check(hook_parent_uid>=0),
    python_launcher_artifact_digest text not null,
    python_runtime_artifact_digest text not null,
    observation_digest text not null,
    observed_at text not null,
    grants_authority integer not null default 0 check(grants_authority=0),
    approval_inherited integer not null default 0 check(approval_inherited=0),
    body_json text not null check(json_valid(body_json)
        and length(cast(body_json as blob))<=32768),
    check(length(native_artifact_digest)=71
        and native_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(native_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(shell_artifact_digest)=71
        and shell_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(shell_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(python_launcher_artifact_digest)=71
        and python_launcher_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(python_launcher_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(python_runtime_artifact_digest)=71
        and python_runtime_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(python_runtime_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(observation_digest)=71
        and observation_digest glob 'sha256:[0-9a-f]*'
        and substr(observation_digest,8) not glob '*[^0-9a-f]*'),
    check(native_pid<>shell_pid),
    check(shell_pid=hook_pid and shell_start_token=hook_start_token and shell_uid=hook_uid),
    check(shell_parent_pid=native_pid and shell_parent_start_token=native_start_token
        and shell_parent_uid=native_uid),
    check(hook_parent_pid=shell_parent_pid
        and hook_parent_start_token=shell_parent_start_token
        and hook_parent_uid=shell_parent_uid),
    check(shell_uid=native_uid and hook_uid=native_uid),
    check(native_artifact_digest<>shell_artifact_digest
        and native_artifact_digest<>python_launcher_artifact_digest
        and native_artifact_digest<>python_runtime_artifact_digest
        and shell_artifact_digest<>python_launcher_artifact_digest
        and shell_artifact_digest<>python_runtime_artifact_digest
        and python_launcher_artifact_digest<>python_runtime_artifact_digest)
) strict;

create table continuity_turn_commit_receipt (
    receipt_digest text primary key check(length(receipt_digest)=71
        and receipt_digest glob 'sha256:[0-9a-f]*'
        and substr(receipt_digest,8) not glob '*[^0-9a-f]*'),
    session_id text not null references continuity_session_binding(session_id),
    binding_digest text not null,
    role text not null check(role in ('user','assistant')),
    item_ref text not null check(length(item_ref) between 1 and 1024),
    content_digest text not null check(length(content_digest)=71
        and content_digest glob 'sha256:[0-9a-f]*'
        and substr(content_digest,8) not glob '*[^0-9a-f]*'),
    store_generation_digest text not null check(length(store_generation_digest)=71
        and store_generation_digest glob 'sha256:[0-9a-f]*'
        and substr(store_generation_digest,8) not glob '*[^0-9a-f]*'),
    previous_turn_commit_digest text references continuity_turn_commit_receipt(receipt_digest),
    body_json text not null check(json_valid(body_json) and length(cast(body_json as blob))<=32768),
    created_at text not null,
    unique(session_id,item_ref)
) strict;

create table continuity_native_event_receipt (
    receipt_digest text primary key check(length(receipt_digest)=71
        and receipt_digest glob 'sha256:[0-9a-f]*'
        and substr(receipt_digest,8) not glob '*[^0-9a-f]*'),
    event_digest text not null unique references session_event_detail(event_digest)
        deferrable initially deferred,
    attachment_revision_digest text not null references continuity_hook_attachment_revision(revision_digest),
    process_generation_digest text not null references continuity_hook_process_generation(process_generation_digest),
    ancestry_receipt_digest text not null unique
        references continuity_hook_invocation_ancestry_receipt(receipt_digest),
    spool_digest text not null,
    previous_spool_digest text,
    observation_digest text not null,
    delivery_id text not null,
    spool_sequence integer not null check(spool_sequence>0),
    external_event_type text not null check(external_event_type in ('SessionStart','PreCompact','PostCompact')),
    internal_event_type text not null check(internal_event_type in ('SESSION_START','PRE_COMPACTION','POST_COMPACTION')),
    external_turn_id text check(external_turn_id is null or length(external_turn_id) between 1 and 512),
    external_trigger_id text check(external_trigger_id is null or length(external_trigger_id) between 1 and 512),
    shell_pid integer not null check(shell_pid>0),
    shell_uid integer not null check(shell_uid>=0),
    shell_start_token text not null check(length(shell_start_token) between 1 and 512),
    hook_pid integer not null check(hook_pid>0),
    hook_uid integer not null check(hook_uid>=0),
    hook_start_token text not null check(length(hook_start_token) between 1 and 512),
    shell_artifact_digest text not null,
    python_launcher_artifact_digest text not null,
    python_runtime_artifact_digest text not null,
    hydration_receipt_digest text references hydration_receipt(receipt_digest),
    grants_authority integer not null default 0 check(grants_authority=0),
    approval_inherited integer not null default 0 check(approval_inherited=0),
    body_json text not null check(json_valid(body_json) and length(cast(body_json as blob))<=32768),
    created_at text not null,
    unique(process_generation_digest,spool_sequence),
    unique(delivery_id),
    check(length(spool_digest)=71 and spool_digest glob 'sha256:[0-9a-f]*'
        and substr(spool_digest,8) not glob '*[^0-9a-f]*'),
    check(previous_spool_digest is null or (length(previous_spool_digest)=71
        and previous_spool_digest glob 'sha256:[0-9a-f]*'
        and substr(previous_spool_digest,8) not glob '*[^0-9a-f]*')),
    check(length(observation_digest)=71 and observation_digest glob 'sha256:[0-9a-f]*'
        and substr(observation_digest,8) not glob '*[^0-9a-f]*'),
    check(length(shell_artifact_digest)=71
        and shell_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(shell_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(python_launcher_artifact_digest)=71
        and python_launcher_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(python_launcher_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check(length(python_runtime_artifact_digest)=71
        and python_runtime_artifact_digest glob 'sha256:[0-9a-f]*'
        and substr(python_runtime_artifact_digest,8) not glob '*[^0-9a-f]*'),
    check((external_event_type='SessionStart')=(internal_event_type='SESSION_START')),
    check((external_event_type='PreCompact')=(internal_event_type='PRE_COMPACTION')),
    check((external_event_type='PostCompact')=(internal_event_type='POST_COMPACTION')),
    check((internal_event_type='SESSION_START')=(hydration_receipt_digest is not null))
) strict;

create table continuity_internal_event_receipt (
    receipt_digest text primary key check(length(receipt_digest)=71
        and receipt_digest glob 'sha256:[0-9a-f]*'
        and substr(receipt_digest,8) not glob '*[^0-9a-f]*'),
    event_digest text not null unique references session_event_detail(event_digest)
        deferrable initially deferred,
    session_id text not null references continuity_session_binding(session_id),
    binding_digest text not null,
    event_kind text not null check(event_kind in
        ('USER_TURN_COMMITTED','ASSISTANT_TURN_COMMITTED','TOOL_EFFECT_CLAIMED',
         'TOOL_EFFECT_COMPLETED','CHECKPOINT_REQUESTED','PRE_CLOSE','SESSION_CLOSED',
         'CRASH_RECOVERED')),
    operation_key text not null check(length(operation_key) between 1 and 1024),
    expected_previous_event_digest text,
    turn_commit_digest text references continuity_turn_commit_receipt(receipt_digest),
    effect_claim_id text references local_effect_claim(id),
    effect_receipt_id text references local_effect_receipt(id),
    native_event_receipt_digest text references continuity_native_event_receipt(receipt_digest),
    close_request_digest text references continuity_close_request(request_digest)
        deferrable initially deferred,
    close_receipt_digest text references close_receipt(receipt_digest),
    hook_recovery_resolution_id text references continuity_hook_recovery_resolution(resolution_id),
    local_recovery_resolution_id text references local_recovery_resolution(id),
    attachment_revision_digest text references continuity_hook_attachment_revision(revision_digest),
    grants_authority integer not null default 0 check(grants_authority=0),
    approval_inherited integer not null default 0 check(approval_inherited=0),
    body_json text not null check(json_valid(body_json) and length(cast(body_json as blob))<=32768),
    created_at text not null,
    unique(session_id,event_kind,operation_key)
    ,check(attachment_revision_digest is not null)
    ,check(
      (turn_commit_digest is not null)
      +(effect_claim_id is not null)
      +(effect_receipt_id is not null)
      +(native_event_receipt_digest is not null)
      +(close_request_digest is not null)
      +(close_receipt_digest is not null)
      +(hook_recovery_resolution_id is not null)
      +(local_recovery_resolution_id is not null)=1
    )
) strict;
create unique index continuity_internal_session_closed_idx
    on continuity_internal_event_receipt(session_id) where event_kind='SESSION_CLOSED';

create table continuity_hook_attachment_revision (
    revision_digest text primary key check(length(revision_digest)=71
        and revision_digest glob 'sha256:[0-9a-f]*'
        and substr(revision_digest,8) not glob '*[^0-9a-f]*'),
    attachment_id text not null references continuity_hook_attachment(attachment_id),
    revision_number integer not null check(revision_number>0),
    previous_revision_digest text references continuity_hook_attachment_revision(revision_digest),
    operation_key text not null,
    state text not null check(state in
        ('attached','hydrated','pre-compact-committed','resume-pending','recovery-required','frozen','closed')),
    process_generation_digest text not null references continuity_hook_process_generation(process_generation_digest),
    active_manifest_digest text references context_manifest(manifest_digest),
    active_hydration_receipt_digest text references hydration_receipt(receipt_digest),
    checkpoint_digest text references continuity_checkpoint(checkpoint_digest),
    pre_compaction_event_digest text references session_event_detail(event_digest),
    post_compaction_event_digest text references session_event_detail(event_digest),
    close_request_digest text references continuity_close_request(request_digest),
    pre_close_event_digest text references session_event_detail(event_digest),
    close_receipt_digest text references close_receipt(receipt_digest),
    session_closed_event_digest text references session_event_detail(event_digest),
    hook_recovery_case_id text references continuity_hook_recovery_case(recovery_case_id),
    hook_recovery_resolution_id text references continuity_hook_recovery_resolution(resolution_id),
    local_recovery_case_id text references local_recovery_case(id),
    local_recovery_resolution_id text references local_recovery_resolution(id),
    crash_recovered_event_digest text references session_event_detail(event_digest),
    crash_recovered_receipt_digest text references continuity_internal_event_receipt(receipt_digest),
    body_json text not null check(json_valid(body_json) and length(cast(body_json as blob))<=65536),
    created_at text not null,
    unique(attachment_id,revision_number), unique(attachment_id,operation_key),
    check((revision_number=1)=(previous_revision_digest is null)),
    check((active_manifest_digest is null)=(active_hydration_receipt_digest is null)),
    check((checkpoint_digest is null)=(pre_compaction_event_digest is null) or close_request_digest is not null),
    check((close_request_digest is null)=(pre_close_event_digest is null)),
    check((close_receipt_digest is null)=(session_closed_event_digest is null)),
    check(not (hook_recovery_case_id is not null and local_recovery_case_id is not null))
) strict;

create index continuity_hook_revision_current_idx
    on continuity_hook_attachment_revision(attachment_id,revision_number desc);
create index continuity_native_event_session_idx
    on continuity_native_event_receipt(process_generation_digest,spool_sequence);

create trigger continuity_hook_attachment_scope before insert on continuity_hook_attachment
when new.body_json<>json_object(
      'attachment_id',new.attachment_id,
      'client_contract_digest',new.client_contract_digest,
      'created_at',new.created_at,
      'hook_set_digest',new.hook_set_digest,
      'native_artifact_digest',new.native_artifact_digest,
      'session_id',new.session_id)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.created_at) is not new.created_at
 or json_extract(new.body_json,'$.attachment_id') is not new.attachment_id
 or json_extract(new.body_json,'$.session_id') is not new.session_id
 or json_extract(new.body_json,'$.client_contract_digest') is not new.client_contract_digest
 or json_extract(new.body_json,'$.native_artifact_digest') is not new.native_artifact_digest
 or json_extract(new.body_json,'$.hook_set_digest') is not new.hook_set_digest
 or json_extract(new.body_json,'$.created_at') is not new.created_at
 or not exists(
    select 1 from continuity_session_binding b join session s on s.id=b.session_id
    where b.session_id=new.session_id and s.status='open'
)
begin select raise(abort,'continuity hook attachment open binding required'); end;

create trigger continuity_process_receipt_scope before insert on continuity_managed_process_receipt
when new.body_json<>json_object(
      'ancestry_policy_digest',new.ancestry_policy_digest,
      'attachment_id',new.attachment_id,
      'created_at',new.created_at,
      'hook_set_digest',new.hook_set_digest,
      'native_artifact_digest',new.native_artifact_digest,
      'native_pid',new.native_pid,
      'native_start_token',new.native_start_token,
      'native_uid',new.native_uid,
      'predecessor_process_generation_digest',new.predecessor_process_generation_digest,
      'transition_kind',new.transition_kind)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.created_at) is not new.created_at
 or json_extract(new.body_json,'$.attachment_id') is not new.attachment_id
 or json_extract(new.body_json,'$.predecessor_process_generation_digest')
      is not new.predecessor_process_generation_digest
 or json_extract(new.body_json,'$.native_pid') is not new.native_pid
 or json_extract(new.body_json,'$.native_uid') is not new.native_uid
 or json_extract(new.body_json,'$.native_start_token') is not new.native_start_token
 or json_extract(new.body_json,'$.native_artifact_digest') is not new.native_artifact_digest
 or json_extract(new.body_json,'$.hook_set_digest') is not new.hook_set_digest
 or json_extract(new.body_json,'$.ancestry_policy_digest') is not new.ancestry_policy_digest
 or json_extract(new.body_json,'$.transition_kind') is not new.transition_kind
 or json_extract(new.body_json,'$.created_at') is not new.created_at
 or not exists(
    select 1 from continuity_hook_attachment a where a.attachment_id=new.attachment_id
      and a.native_artifact_digest=new.native_artifact_digest
      and a.hook_set_digest=new.hook_set_digest
) or (new.transition_kind='initial-attach')<>(new.predecessor_process_generation_digest is null)
begin select raise(abort,'continuity managed process receipt scope mismatch'); end;

create trigger continuity_process_generation_chain before insert on continuity_hook_process_generation
when new.body_json<>json_object(
      'ancestry_policy_digest',new.ancestry_policy_digest,
      'attachment_id',new.attachment_id,
      'created_at',new.created_at,
      'generation',new.generation,
      'hook_set_digest',new.hook_set_digest,
      'managed_launch_receipt_digest',new.managed_launch_receipt_digest,
      'native_artifact_digest',new.native_artifact_digest,
      'native_pid',new.native_pid,
      'native_start_token',new.native_start_token,
      'native_uid',new.native_uid,
      'previous_process_generation_digest',new.previous_process_generation_digest)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.created_at) is not new.created_at
 or json_extract(new.body_json,'$.attachment_id') is not new.attachment_id
 or json_extract(new.body_json,'$.generation') is not new.generation
 or json_extract(new.body_json,'$.native_pid') is not new.native_pid
 or json_extract(new.body_json,'$.native_uid') is not new.native_uid
 or json_extract(new.body_json,'$.native_start_token') is not new.native_start_token
 or json_extract(new.body_json,'$.native_artifact_digest') is not new.native_artifact_digest
 or json_extract(new.body_json,'$.hook_set_digest') is not new.hook_set_digest
 or json_extract(new.body_json,'$.ancestry_policy_digest') is not new.ancestry_policy_digest
 or json_extract(new.body_json,'$.previous_process_generation_digest')
      is not new.previous_process_generation_digest
 or json_extract(new.body_json,'$.managed_launch_receipt_digest')
      is not new.managed_launch_receipt_digest
 or json_extract(new.body_json,'$.created_at') is not new.created_at
 or not exists(
    select 1 from continuity_hook_attachment a
    join continuity_managed_process_receipt r on r.receipt_digest=new.managed_launch_receipt_digest
    where a.attachment_id=new.attachment_id and r.attachment_id=a.attachment_id
      and r.native_pid=new.native_pid and r.native_uid=new.native_uid
      and r.native_start_token=new.native_start_token
      and r.native_artifact_digest=new.native_artifact_digest
      and r.hook_set_digest=new.hook_set_digest
      and r.ancestry_policy_digest=new.ancestry_policy_digest
      and a.native_artifact_digest=new.native_artifact_digest
      and a.hook_set_digest=new.hook_set_digest
      and ((new.generation=1 and r.transition_kind='initial-attach') or
           (new.generation>1 and r.transition_kind in ('orderly-reattach','recovery-reattach')))
      and ((new.generation=1 and r.predecessor_process_generation_digest is null) or
           (new.generation>1 and exists(
              select 1 from continuity_hook_process_generation p
              where p.process_generation_digest=new.previous_process_generation_digest
                and p.attachment_id=new.attachment_id and p.generation=new.generation-1
                and r.predecessor_process_generation_digest=p.process_generation_digest)))
)
begin select raise(abort,'continuity process generation chain mismatch'); end;

create trigger continuity_hook_revision_chain before insert on continuity_hook_attachment_revision
when not exists(
    select 1 from continuity_hook_attachment a
    join continuity_hook_process_generation g on g.process_generation_digest=new.process_generation_digest
    where a.attachment_id=new.attachment_id and g.attachment_id=a.attachment_id
      and (
        (new.revision_number=1 and new.state='attached' and g.generation=1
         and new.active_manifest_digest is null and new.checkpoint_digest is null
         and new.close_request_digest is null and new.hook_recovery_case_id is null
         and new.local_recovery_case_id is null)
        or
        (new.revision_number>1 and exists(
          select 1 from continuity_hook_attachment_revision p
          where p.revision_digest=new.previous_revision_digest
            and p.attachment_id=new.attachment_id and p.revision_number=new.revision_number-1
            and (
              (p.state='attached' and new.state in ('hydrated','recovery-required','attached')) or
              (p.state='hydrated' and new.state in ('pre-compact-committed','frozen','recovery-required','attached')) or
              (p.state='pre-compact-committed' and new.state in ('resume-pending','recovery-required')) or
              (p.state='resume-pending' and new.state in ('hydrated','recovery-required')) or
              (p.state='recovery-required' and new.state in ('attached','hydrated','frozen')) or
              (p.state='frozen' and new.state in ('closed','recovery-required'))
            )
        ))
      )
)
begin select raise(abort,'continuity attachment revision transition mismatch'); end;

create trigger continuity_hook_revision_exact before insert on continuity_hook_attachment_revision
when new.body_json<>json_object(
      'active_hydration_receipt_digest',new.active_hydration_receipt_digest,
      'active_manifest_digest',new.active_manifest_digest,
      'attachment_id',new.attachment_id,
      'checkpoint_digest',new.checkpoint_digest,
      'close_receipt_digest',new.close_receipt_digest,
      'close_request_digest',new.close_request_digest,
      'crash_recovered_event_digest',new.crash_recovered_event_digest,
      'crash_recovered_receipt_digest',new.crash_recovered_receipt_digest,
      'created_at',new.created_at,
      'hook_recovery_case_id',new.hook_recovery_case_id,
      'hook_recovery_resolution_id',new.hook_recovery_resolution_id,
      'local_recovery_case_id',new.local_recovery_case_id,
      'local_recovery_resolution_id',new.local_recovery_resolution_id,
      'operation_key',new.operation_key,
      'post_compaction_event_digest',new.post_compaction_event_digest,
      'pre_close_event_digest',new.pre_close_event_digest,
      'pre_compaction_event_digest',new.pre_compaction_event_digest,
      'previous_revision_digest',new.previous_revision_digest,
      'process_generation_digest',new.process_generation_digest,
      'revision_digest',new.revision_digest,
      'revision_number',new.revision_number,
      'session_closed_event_digest',new.session_closed_event_digest,
      'state',new.state)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.created_at) is not new.created_at
 or exists(
   select 1 from json_each(json_array(
     new.revision_digest,new.previous_revision_digest,new.process_generation_digest,
     new.active_manifest_digest,new.active_hydration_receipt_digest,new.checkpoint_digest,
     new.pre_compaction_event_digest,new.post_compaction_event_digest,
     new.close_request_digest,new.pre_close_event_digest,new.close_receipt_digest,
     new.session_closed_event_digest,new.crash_recovered_event_digest,
     new.crash_recovered_receipt_digest)) d
   where d.value is not null and (
     length(d.value)<>71 or substr(d.value,1,7)<>'sha256:'
     or substr(d.value,8) glob '*[^0-9a-f]*')
 )
 or json_extract(new.body_json,'$.revision_digest') is not new.revision_digest
 or json_extract(new.body_json,'$.attachment_id') is not new.attachment_id
 or json_extract(new.body_json,'$.revision_number') is not new.revision_number
 or json_extract(new.body_json,'$.previous_revision_digest') is not new.previous_revision_digest
 or json_extract(new.body_json,'$.operation_key') is not new.operation_key
 or json_extract(new.body_json,'$.state') is not new.state
 or json_extract(new.body_json,'$.process_generation_digest')
      is not new.process_generation_digest
 or json_extract(new.body_json,'$.active_manifest_digest') is not new.active_manifest_digest
 or json_extract(new.body_json,'$.active_hydration_receipt_digest')
      is not new.active_hydration_receipt_digest
 or json_extract(new.body_json,'$.checkpoint_digest') is not new.checkpoint_digest
 or json_extract(new.body_json,'$.pre_compaction_event_digest')
      is not new.pre_compaction_event_digest
 or json_extract(new.body_json,'$.post_compaction_event_digest')
      is not new.post_compaction_event_digest
 or json_extract(new.body_json,'$.close_request_digest') is not new.close_request_digest
 or json_extract(new.body_json,'$.pre_close_event_digest') is not new.pre_close_event_digest
 or json_extract(new.body_json,'$.close_receipt_digest') is not new.close_receipt_digest
 or json_extract(new.body_json,'$.session_closed_event_digest')
      is not new.session_closed_event_digest
 or json_extract(new.body_json,'$.hook_recovery_case_id') is not new.hook_recovery_case_id
 or json_extract(new.body_json,'$.hook_recovery_resolution_id')
      is not new.hook_recovery_resolution_id
 or json_extract(new.body_json,'$.local_recovery_case_id') is not new.local_recovery_case_id
 or json_extract(new.body_json,'$.local_recovery_resolution_id')
      is not new.local_recovery_resolution_id
 or json_extract(new.body_json,'$.crash_recovered_event_digest')
      is not new.crash_recovered_event_digest
 or json_extract(new.body_json,'$.crash_recovered_receipt_digest')
      is not new.crash_recovered_receipt_digest
 or json_extract(new.body_json,'$.created_at') is not new.created_at
 or not exists(
   select 1 from continuity_hook_attachment a
   join continuity_hook_process_generation g on g.attachment_id=a.attachment_id
   where a.attachment_id=new.attachment_id
     and g.process_generation_digest=new.process_generation_digest
 )
 or (new.active_manifest_digest is not null and not exists(
   select 1 from context_manifest m
   join hydration_receipt h on h.manifest_digest=m.manifest_digest
   join continuity_hook_attachment a on a.session_id=m.session_id
   where m.manifest_digest=new.active_manifest_digest
     and h.receipt_digest=new.active_hydration_receipt_digest
     and h.session_id=m.session_id and a.attachment_id=new.attachment_id
 ))
 or not (
   (new.revision_number=1 and new.state='attached'
    and new.active_manifest_digest is null and new.checkpoint_digest is null
    and new.pre_compaction_event_digest is null and new.post_compaction_event_digest is null
    and new.close_request_digest is null and new.pre_close_event_digest is null
    and new.close_receipt_digest is null and new.session_closed_event_digest is null
    and new.hook_recovery_case_id is null and new.hook_recovery_resolution_id is null
    and new.local_recovery_case_id is null and new.local_recovery_resolution_id is null
    and new.crash_recovered_event_digest is null and new.crash_recovered_receipt_digest is null)
   or
   (new.revision_number>1 and exists(
     select 1 from continuity_hook_attachment_revision p
     join continuity_hook_process_generation pg on pg.process_generation_digest=p.process_generation_digest
     join continuity_hook_process_generation ng on ng.process_generation_digest=new.process_generation_digest
     where p.revision_digest=new.previous_revision_digest
       and p.attachment_id=new.attachment_id
       and p.revision_number=new.revision_number-1
       and p.revision_number=(select max(x.revision_number)
                              from continuity_hook_attachment_revision x
                              where x.attachment_id=new.attachment_id)
       and (
         (p.state='attached' and new.state='hydrated'
          and new.process_generation_digest=p.process_generation_digest
          and new.active_manifest_digest is not null
          and new.checkpoint_digest is null and new.pre_compaction_event_digest is null
          and new.post_compaction_event_digest is null and new.close_request_digest is null
          and new.pre_close_event_digest is null and new.close_receipt_digest is null
          and new.session_closed_event_digest is null
          and new.hook_recovery_case_id is p.hook_recovery_case_id
          and new.hook_recovery_resolution_id is p.hook_recovery_resolution_id
          and new.local_recovery_case_id is p.local_recovery_case_id
          and new.local_recovery_resolution_id is p.local_recovery_resolution_id
          and new.crash_recovered_event_digest is p.crash_recovered_event_digest
          and new.crash_recovered_receipt_digest is p.crash_recovered_receipt_digest)
         or
         (p.state='hydrated' and new.state='pre-compact-committed'
          and new.process_generation_digest=p.process_generation_digest
          and new.active_manifest_digest is p.active_manifest_digest
          and new.active_hydration_receipt_digest is p.active_hydration_receipt_digest
          and new.checkpoint_digest is not null and new.pre_compaction_event_digest is not null
          and new.post_compaction_event_digest is null and new.close_request_digest is null
          and new.pre_close_event_digest is null and new.close_receipt_digest is null
          and new.session_closed_event_digest is null
          and new.hook_recovery_case_id is p.hook_recovery_case_id
          and new.hook_recovery_resolution_id is p.hook_recovery_resolution_id
          and new.local_recovery_case_id is p.local_recovery_case_id
          and new.local_recovery_resolution_id is p.local_recovery_resolution_id
          and new.crash_recovered_event_digest is p.crash_recovered_event_digest
          and new.crash_recovered_receipt_digest is p.crash_recovered_receipt_digest)
         or
         (p.state='pre-compact-committed' and new.state='resume-pending'
          and new.process_generation_digest=p.process_generation_digest
          and new.active_manifest_digest is p.active_manifest_digest
          and new.active_hydration_receipt_digest is p.active_hydration_receipt_digest
          and new.checkpoint_digest is p.checkpoint_digest
          and new.pre_compaction_event_digest is p.pre_compaction_event_digest
          and new.post_compaction_event_digest is not null and new.close_request_digest is null
          and new.pre_close_event_digest is null and new.close_receipt_digest is null
          and new.session_closed_event_digest is null
          and new.hook_recovery_case_id is p.hook_recovery_case_id
          and new.hook_recovery_resolution_id is p.hook_recovery_resolution_id
          and new.local_recovery_case_id is p.local_recovery_case_id
          and new.local_recovery_resolution_id is p.local_recovery_resolution_id
          and new.crash_recovered_event_digest is p.crash_recovered_event_digest
          and new.crash_recovered_receipt_digest is p.crash_recovered_receipt_digest)
         or
         (p.state='resume-pending' and new.state='hydrated'
          and new.process_generation_digest=p.process_generation_digest
          and new.active_manifest_digest is not null
          and new.checkpoint_digest is p.checkpoint_digest
          and new.pre_compaction_event_digest is p.pre_compaction_event_digest
          and new.post_compaction_event_digest is p.post_compaction_event_digest
          and new.close_request_digest is null and new.pre_close_event_digest is null
          and new.close_receipt_digest is null and new.session_closed_event_digest is null
          and new.hook_recovery_case_id is p.hook_recovery_case_id
          and new.hook_recovery_resolution_id is p.hook_recovery_resolution_id
          and new.local_recovery_case_id is p.local_recovery_case_id
          and new.local_recovery_resolution_id is p.local_recovery_resolution_id
          and new.crash_recovered_event_digest is p.crash_recovered_event_digest
          and new.crash_recovered_receipt_digest is p.crash_recovered_receipt_digest)
         or
         (p.state='hydrated' and new.state='frozen'
          and new.process_generation_digest=p.process_generation_digest
          and new.active_manifest_digest is p.active_manifest_digest
          and new.active_hydration_receipt_digest is p.active_hydration_receipt_digest
          and new.checkpoint_digest is not null and new.pre_compaction_event_digest is null
          and new.post_compaction_event_digest is null
          and new.close_request_digest is not null and new.pre_close_event_digest is not null
          and new.close_receipt_digest is null and new.session_closed_event_digest is null
          and new.hook_recovery_case_id is p.hook_recovery_case_id
          and new.hook_recovery_resolution_id is p.hook_recovery_resolution_id
          and new.local_recovery_case_id is p.local_recovery_case_id
          and new.local_recovery_resolution_id is p.local_recovery_resolution_id
          and new.crash_recovered_event_digest is p.crash_recovered_event_digest
          and new.crash_recovered_receipt_digest is p.crash_recovered_receipt_digest)
         or
         (p.state='frozen' and new.state='closed'
          and new.process_generation_digest=p.process_generation_digest
          and new.active_manifest_digest is p.active_manifest_digest
          and new.active_hydration_receipt_digest is p.active_hydration_receipt_digest
          and new.checkpoint_digest is p.checkpoint_digest
          and new.pre_compaction_event_digest is null
          and new.post_compaction_event_digest is null
          and new.close_request_digest is p.close_request_digest
          and new.pre_close_event_digest is p.pre_close_event_digest
          and new.close_receipt_digest is not null and new.session_closed_event_digest is not null
          and new.hook_recovery_case_id is p.hook_recovery_case_id
          and new.hook_recovery_resolution_id is p.hook_recovery_resolution_id
          and new.local_recovery_case_id is p.local_recovery_case_id
          and new.local_recovery_resolution_id is p.local_recovery_resolution_id
          and new.crash_recovered_event_digest is p.crash_recovered_event_digest
          and new.crash_recovered_receipt_digest is p.crash_recovered_receipt_digest)
         or
         (p.state in ('attached','hydrated','pre-compact-committed','resume-pending','frozen')
          and new.state='recovery-required'
          and new.process_generation_digest=p.process_generation_digest
          and new.active_manifest_digest is p.active_manifest_digest
          and new.active_hydration_receipt_digest is p.active_hydration_receipt_digest
          and new.checkpoint_digest is p.checkpoint_digest
          and new.pre_compaction_event_digest is p.pre_compaction_event_digest
          and new.post_compaction_event_digest is p.post_compaction_event_digest
          and new.close_request_digest is p.close_request_digest
          and new.pre_close_event_digest is p.pre_close_event_digest
          and new.close_receipt_digest is p.close_receipt_digest
          and new.session_closed_event_digest is p.session_closed_event_digest
          and p.hook_recovery_case_id is null and p.local_recovery_case_id is null
          and ((new.hook_recovery_case_id is not null and new.local_recovery_case_id is null)
               or (new.hook_recovery_case_id is null and new.local_recovery_case_id is not null))
          and new.hook_recovery_resolution_id is null
          and new.local_recovery_resolution_id is null
          and new.crash_recovered_event_digest is null
          and new.crash_recovered_receipt_digest is null)
         or
         (p.state='recovery-required' and new.state in ('hydrated','frozen')
          and new.process_generation_digest=p.process_generation_digest
          and new.active_manifest_digest is p.active_manifest_digest
          and new.active_hydration_receipt_digest is p.active_hydration_receipt_digest
          and new.checkpoint_digest is p.checkpoint_digest
          and new.pre_compaction_event_digest is p.pre_compaction_event_digest
         and new.post_compaction_event_digest is p.post_compaction_event_digest
         and new.close_request_digest is p.close_request_digest
         and new.pre_close_event_digest is p.pre_close_event_digest
          and new.close_receipt_digest is p.close_receipt_digest
          and new.session_closed_event_digest is p.session_closed_event_digest
         and new.hook_recovery_case_id is p.hook_recovery_case_id
          and new.local_recovery_case_id is p.local_recovery_case_id
          and new.crash_recovered_event_digest is not null
          and new.crash_recovered_receipt_digest is not null
          and ((new.hook_recovery_resolution_id is not null
                and new.local_recovery_resolution_id is null
                and exists(select 1 from continuity_hook_recovery_resolution hr
                  where hr.resolution_id=new.hook_recovery_resolution_id
                    and hr.recovery_case_id=p.hook_recovery_case_id and hr.outcome='restored'))
               or (new.hook_recovery_resolution_id is null
                   and new.local_recovery_resolution_id is not null
                   and exists(select 1 from local_recovery_resolution lr
                     where lr.id=new.local_recovery_resolution_id
                       and lr.recovery_case_id=p.local_recovery_case_id
                       and lr.outcome in ('completed','delivered'))))
          and ((new.state='hydrated' and p.active_manifest_digest is not null)
               or (new.state='frozen' and p.close_request_digest is not null)))
         or
         (p.state='recovery-required' and new.state='attached'
          and ng.generation=pg.generation+1
          and ng.previous_process_generation_digest=pg.process_generation_digest
          and exists(select 1 from continuity_managed_process_receipt mr
            where mr.receipt_digest=ng.managed_launch_receipt_digest
              and mr.transition_kind='recovery-reattach')
          and new.active_manifest_digest is null and new.checkpoint_digest is null
          and new.pre_compaction_event_digest is null
          and new.post_compaction_event_digest is null
          and new.close_request_digest is null and new.pre_close_event_digest is null
          and new.close_receipt_digest is null and new.session_closed_event_digest is null
          and new.hook_recovery_case_id is p.hook_recovery_case_id
          and new.local_recovery_case_id is p.local_recovery_case_id
          and new.crash_recovered_event_digest is not null
          and new.crash_recovered_receipt_digest is not null
          and ((new.hook_recovery_resolution_id is not null
                and new.local_recovery_resolution_id is null
                and exists(select 1 from continuity_hook_recovery_resolution hr
                  where hr.resolution_id=new.hook_recovery_resolution_id
                    and hr.recovery_case_id=p.hook_recovery_case_id and hr.outcome='restored'))
               or (new.hook_recovery_resolution_id is null
                   and new.local_recovery_resolution_id is not null
                   and exists(select 1 from local_recovery_resolution lr
                     where lr.id=new.local_recovery_resolution_id
                       and lr.recovery_case_id=p.local_recovery_case_id
                       and lr.outcome in ('completed','delivered')))))
         or
         (p.state in ('attached','hydrated') and new.state='attached'
          and ng.generation=pg.generation+1
          and ng.previous_process_generation_digest=pg.process_generation_digest
          and exists(select 1 from continuity_managed_process_receipt mr
            where mr.receipt_digest=ng.managed_launch_receipt_digest
              and mr.transition_kind='orderly-reattach')
          and new.active_manifest_digest is null and new.checkpoint_digest is null
          and new.pre_compaction_event_digest is null
          and new.post_compaction_event_digest is null
          and new.close_request_digest is null and new.pre_close_event_digest is null
          and new.close_receipt_digest is null and new.session_closed_event_digest is null
          and new.hook_recovery_case_id is p.hook_recovery_case_id
          and new.hook_recovery_resolution_id is p.hook_recovery_resolution_id
          and new.local_recovery_case_id is p.local_recovery_case_id
          and new.local_recovery_resolution_id is p.local_recovery_resolution_id
          and new.crash_recovered_event_digest is p.crash_recovered_event_digest
          and new.crash_recovered_receipt_digest is p.crash_recovered_receipt_digest)
       )
   ))
 )
begin select raise(abort,'continuity attachment revision exact invariant mismatch'); end;

create trigger continuity_hook_revision_evidence before insert on continuity_hook_attachment_revision
when (new.state='hydrated' and new.active_manifest_digest is null)
 or (new.state in ('pre-compact-committed','resume-pending','frozen','closed')
     and new.active_manifest_digest is null)
 or (new.state='attached' and (new.active_manifest_digest is not null
     or new.checkpoint_digest is not null or new.pre_compaction_event_digest is not null
     or new.post_compaction_event_digest is not null or new.close_request_digest is not null
     or new.close_receipt_digest is not null))
 or (new.state='pre-compact-committed' and not exists(
   select 1 from continuity_hook_attachment_revision p
   join continuity_hook_attachment a on a.attachment_id=p.attachment_id
   join continuity_checkpoint cp on cp.checkpoint_digest=new.checkpoint_digest
     and cp.session_id=a.session_id and cp.covered_event_digest=new.pre_compaction_event_digest
   join session_event e on e.event_digest=new.pre_compaction_event_digest
     and e.session_id=a.session_id and e.event_kind='PRE_COMPACTION'
   join continuity_native_event_receipt n on n.event_digest=e.event_digest
     and n.internal_event_type='PRE_COMPACTION'
     and n.attachment_revision_digest=p.revision_digest
   where p.revision_digest=new.previous_revision_digest
 ))
 or (new.state='resume-pending' and not exists(
   select 1 from continuity_hook_attachment_revision p
   join continuity_hook_attachment a on a.attachment_id=p.attachment_id
   join session_event e on e.event_digest=new.post_compaction_event_digest
     and e.session_id=a.session_id and e.event_kind='POST_COMPACTION'
   join continuity_native_event_receipt n on n.event_digest=e.event_digest
     and n.internal_event_type='POST_COMPACTION'
     and n.attachment_revision_digest=p.revision_digest
   where p.revision_digest=new.previous_revision_digest
 ))
 or (new.state='hydrated' and new.checkpoint_digest is not null and not exists(
   select 1 from context_manifest m
   where m.manifest_digest=new.active_manifest_digest
     and m.checkpoint_digest=new.checkpoint_digest
 ))
 or (new.state='frozen' and not exists(
   select 1 from continuity_hook_attachment_revision p
   join continuity_hook_attachment a on a.attachment_id=p.attachment_id
   join continuity_checkpoint cp on cp.checkpoint_digest=new.checkpoint_digest
     and cp.session_id=a.session_id
   join continuity_close_request c on c.request_digest=new.close_request_digest
     and c.session_id=a.session_id and c.checkpoint_digest=cp.checkpoint_digest
   join session_event e on e.event_digest=new.pre_close_event_digest
     and e.session_id=a.session_id and e.event_kind='PRE_CLOSE'
   join continuity_internal_event_receipt r on r.event_digest=e.event_digest
     and r.event_kind='PRE_CLOSE' and r.close_request_digest=c.request_digest
   where p.revision_digest=new.previous_revision_digest
     and cp.covered_sequence=c.covered_sequence
     and cp.covered_event_digest=e.event_digest
     and (
       (p.state='hydrated' and r.attachment_revision_digest=p.revision_digest)
       or
       (p.state='recovery-required' and exists(
         select 1
         from continuity_hook_attachment_revision f
         join continuity_hook_attachment_revision h
           on h.revision_digest=f.previous_revision_digest
          and h.attachment_id=f.attachment_id
          and h.state='hydrated'
         where f.revision_digest=p.previous_revision_digest
           and f.attachment_id=p.attachment_id
           and f.state='frozen'
           and r.attachment_revision_digest=h.revision_digest
           and f.process_generation_digest=p.process_generation_digest
           and f.active_manifest_digest is p.active_manifest_digest
           and f.active_hydration_receipt_digest is p.active_hydration_receipt_digest
           and f.checkpoint_digest is p.checkpoint_digest
           and f.pre_compaction_event_digest is p.pre_compaction_event_digest
           and f.post_compaction_event_digest is p.post_compaction_event_digest
           and f.close_request_digest is p.close_request_digest
           and f.pre_close_event_digest is p.pre_close_event_digest
           and f.close_receipt_digest is p.close_receipt_digest
           and f.session_closed_event_digest is p.session_closed_event_digest
       ))
     )
 ))
 or (new.state='closed' and not exists(
   select 1 from continuity_hook_attachment_revision p
   join continuity_hook_attachment a on a.attachment_id=p.attachment_id
   join close_receipt c on c.receipt_digest=new.close_receipt_digest
     and c.session_id=a.session_id and c.request_digest=new.close_request_digest
     and c.checkpoint_digest=new.checkpoint_digest
   join session_event e on e.event_digest=new.session_closed_event_digest
     and e.session_id=a.session_id and e.event_kind='SESSION_CLOSED'
   join continuity_internal_event_receipt r on r.event_digest=e.event_digest
     and r.event_kind='SESSION_CLOSED' and r.close_receipt_digest=c.receipt_digest
     and r.attachment_revision_digest=p.revision_digest
   where p.revision_digest=new.previous_revision_digest and p.state='frozen'
 ))
 or ((new.crash_recovered_event_digest is not null
      or new.crash_recovered_receipt_digest is not null) and not exists(
   select 1 from continuity_hook_attachment_revision p
   where p.revision_digest=new.previous_revision_digest
     and p.attachment_id=new.attachment_id
     and (
       (p.crash_recovered_event_digest is null
        and p.crash_recovered_receipt_digest is null
        and new.crash_recovered_event_digest is not null
        and new.crash_recovered_receipt_digest is not null
        and p.state='recovery-required'
        and exists(
          select 1 from continuity_hook_attachment a
          join session_event e on e.event_digest=new.crash_recovered_event_digest
            and e.session_id=a.session_id and e.event_kind='CRASH_RECOVERED'
          join continuity_internal_event_receipt r
            on r.receipt_digest=new.crash_recovered_receipt_digest
            and r.event_digest=e.event_digest and r.event_kind='CRASH_RECOVERED'
            and r.attachment_revision_digest=p.revision_digest
          where a.attachment_id=p.attachment_id
            and ((r.hook_recovery_resolution_id=new.hook_recovery_resolution_id
                  and r.local_recovery_resolution_id is null)
              or (r.local_recovery_resolution_id=new.local_recovery_resolution_id
                  and r.hook_recovery_resolution_id is null))
        ))
       or
       (p.crash_recovered_event_digest is not null
        and p.crash_recovered_receipt_digest is not null
        and new.hook_recovery_case_id is p.hook_recovery_case_id
        and new.hook_recovery_resolution_id is p.hook_recovery_resolution_id
        and new.local_recovery_case_id is p.local_recovery_case_id
        and new.local_recovery_resolution_id is p.local_recovery_resolution_id
        and new.crash_recovered_event_digest is p.crash_recovered_event_digest
        and new.crash_recovered_receipt_digest is p.crash_recovered_receipt_digest)
     )
 ))
 or (new.revision_number>1 and exists(
   select 1 from continuity_hook_attachment_revision p
   where p.revision_digest=new.previous_revision_digest
     and p.hook_recovery_resolution_id is not null
     and (new.hook_recovery_case_id is not p.hook_recovery_case_id
       or new.hook_recovery_resolution_id is not p.hook_recovery_resolution_id
       or new.crash_recovered_event_digest is not p.crash_recovered_event_digest
       or new.crash_recovered_receipt_digest is not p.crash_recovered_receipt_digest)
 ))
 or (new.revision_number>1 and exists(
   select 1 from continuity_hook_attachment_revision p
   where p.revision_digest=new.previous_revision_digest
     and p.local_recovery_resolution_id is not null
     and (new.local_recovery_case_id is not p.local_recovery_case_id
       or new.local_recovery_resolution_id is not p.local_recovery_resolution_id
       or new.crash_recovered_event_digest is not p.crash_recovered_event_digest
       or new.crash_recovered_receipt_digest is not p.crash_recovered_receipt_digest)
 ))
begin select raise(abort,'continuity attachment revision evidence mismatch'); end;

create trigger continuity_hook_recovery_case_scope before insert on continuity_hook_recovery_case
when new.body_json<>json_object(
      'attachment_id',new.attachment_id,
      'case_kind',new.case_kind,
      'created_at',new.created_at,
      'evidence_digest',new.evidence_digest,
      'process_generation_digest',new.process_generation_digest,
      'recovery_case_id',new.recovery_case_id,
      'session_id',new.session_id)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.created_at) is not new.created_at
 or json_extract(new.body_json,'$.recovery_case_id') is not new.recovery_case_id
 or json_extract(new.body_json,'$.attachment_id') is not new.attachment_id
 or json_extract(new.body_json,'$.session_id') is not new.session_id
 or json_extract(new.body_json,'$.process_generation_digest') is not new.process_generation_digest
 or json_extract(new.body_json,'$.case_kind') is not new.case_kind
 or json_extract(new.body_json,'$.evidence_digest') is not new.evidence_digest
 or json_extract(new.body_json,'$.created_at') is not new.created_at
 or not exists(
    select 1 from continuity_hook_attachment a
    join continuity_hook_process_generation g on g.attachment_id=a.attachment_id
    where a.attachment_id=new.attachment_id and a.session_id=new.session_id
      and g.process_generation_digest=new.process_generation_digest
)
begin select raise(abort,'continuity hook recovery scope mismatch'); end;

create trigger continuity_hook_recovery_resolution_scope before insert on continuity_hook_recovery_resolution
when new.body_json<>json_object(
      'created_at',new.created_at,
      'evidence_digest',new.evidence_digest,
      'outcome',new.outcome,
      'recovery_case_id',new.recovery_case_id,
      'resolution_id',new.resolution_id)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.created_at) is not new.created_at
 or json_extract(new.body_json,'$.resolution_id') is not new.resolution_id
 or json_extract(new.body_json,'$.recovery_case_id') is not new.recovery_case_id
 or json_extract(new.body_json,'$.outcome') is not new.outcome
 or json_extract(new.body_json,'$.evidence_digest') is not new.evidence_digest
 or json_extract(new.body_json,'$.created_at') is not new.created_at
 or not exists(select 1 from continuity_hook_recovery_case c where c.recovery_case_id=new.recovery_case_id)
begin select raise(abort,'continuity hook recovery resolution mismatch'); end;

create trigger continuity_reviewed_hook_command_scope
before insert on continuity_reviewed_hook_command
when new.body_json<>json_object(
      'approval_inherited',json('false'),
      'argv_recipe_digest',new.argv_recipe_digest,
      'attachment_id',new.attachment_id,
      'client_contract_digest',new.client_contract_digest,
      'created_at',new.created_at,
      'external_event_type',new.external_event_type,
      'grants_authority',json('false'),
      'hook_set_digest',new.hook_set_digest,
      'python_launcher_artifact_digest',new.python_launcher_artifact_digest,
      'python_runtime_artifact_digest',new.python_runtime_artifact_digest,
      'sandbox_profile_digest',new.sandbox_profile_digest,
      'schema','zekam-reviewed-hook-command/v1',
      'shell_artifact_digest',new.shell_artifact_digest,
      'topology',new.topology)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.created_at) is not new.created_at
 or not exists(
   select 1 from continuity_hook_attachment a
   where a.attachment_id=new.attachment_id
     and a.client_contract_digest=new.client_contract_digest
     and a.hook_set_digest=new.hook_set_digest
 )
begin select raise(abort,'continuity reviewed hook command mismatch'); end;

create trigger continuity_hook_ancestry_scope
before insert on continuity_hook_invocation_ancestry_receipt
when new.body_json<>json_object(
      'ancestry_policy_digest',new.ancestry_policy_digest,
      'approval_inherited',new.approval_inherited,
      'delivery_id',new.delivery_id,
      'external_event_type',new.external_event_type,
      'grants_authority',new.grants_authority,
      'hook_parent_pid',new.hook_parent_pid,
      'hook_parent_start_token',new.hook_parent_start_token,
      'hook_parent_uid',new.hook_parent_uid,
      'hook_pid',new.hook_pid,
      'hook_start_token',new.hook_start_token,
      'hook_uid',new.hook_uid,
      'launch_command_digest',new.launch_command_digest,
      'native_artifact_digest',new.native_artifact_digest,
      'native_pid',new.native_pid,
      'native_start_token',new.native_start_token,
      'native_uid',new.native_uid,
      'observation_digest',new.observation_digest,
      'observed_at',new.observed_at,
      'process_generation_digest',new.process_generation_digest,
      'python_launcher_artifact_digest',new.python_launcher_artifact_digest,
      'python_runtime_artifact_digest',new.python_runtime_artifact_digest,
      'schema','zekam-hook-invocation-ancestry-receipt/v1',
      'shell_artifact_digest',new.shell_artifact_digest,
      'shell_parent_pid',new.shell_parent_pid,
      'shell_parent_start_token',new.shell_parent_start_token,
      'shell_parent_uid',new.shell_parent_uid,
      'shell_pid',new.shell_pid,
      'shell_start_token',new.shell_start_token,
      'shell_uid',new.shell_uid,
      'topology',new.topology)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.observed_at) is not new.observed_at
 or not exists(
   select 1 from continuity_hook_process_generation g
   join continuity_hook_attachment a on a.attachment_id=g.attachment_id
   join continuity_reviewed_hook_command c
     on c.command_digest=new.launch_command_digest
   where g.process_generation_digest=new.process_generation_digest
     and g.ancestry_policy_digest=new.ancestry_policy_digest
     and g.native_pid=new.native_pid and g.native_uid=new.native_uid
     and g.native_start_token=new.native_start_token
     and g.native_artifact_digest=new.native_artifact_digest
     and a.native_artifact_digest=new.native_artifact_digest
     and c.attachment_id=a.attachment_id
     and c.external_event_type=new.external_event_type
     and c.topology=new.topology
     and c.client_contract_digest=a.client_contract_digest
     and c.hook_set_digest=a.hook_set_digest
     and c.shell_artifact_digest=new.shell_artifact_digest
     and c.python_launcher_artifact_digest=new.python_launcher_artifact_digest
     and c.python_runtime_artifact_digest=new.python_runtime_artifact_digest
 )
begin select raise(abort,'continuity hook invocation ancestry mismatch'); end;

create trigger continuity_native_event_kind_guard before insert on continuity_native_event_receipt
when new.body_json<>json_object(
      'ancestry_receipt_digest',new.ancestry_receipt_digest,
      'approval_inherited',new.approval_inherited,
      'attachment_revision_digest',new.attachment_revision_digest,
      'created_at',new.created_at,
      'delivery_id',new.delivery_id,
      'event_digest',new.event_digest,
      'external_event_type',new.external_event_type,
      'external_trigger_id',new.external_trigger_id,
      'external_turn_id',new.external_turn_id,
      'grants_authority',new.grants_authority,
      'hook_pid',new.hook_pid,
      'hook_start_token',new.hook_start_token,
      'hook_uid',new.hook_uid,
      'hydration_receipt_digest',new.hydration_receipt_digest,
      'internal_event_type',new.internal_event_type,
      'observation_digest',new.observation_digest,
      'previous_spool_digest',new.previous_spool_digest,
      'process_generation_digest',new.process_generation_digest,
      'python_launcher_artifact_digest',new.python_launcher_artifact_digest,
      'python_runtime_artifact_digest',new.python_runtime_artifact_digest,
      'shell_artifact_digest',new.shell_artifact_digest,
      'shell_pid',new.shell_pid,
      'shell_start_token',new.shell_start_token,
      'shell_uid',new.shell_uid,
      'spool_digest',new.spool_digest,
      'spool_sequence',new.spool_sequence)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.created_at) is not new.created_at
 or length(new.event_digest)<>71 or substr(new.event_digest,1,7)<>'sha256:'
 or substr(new.event_digest,8) glob '*[^0-9a-f]*'
 or (new.hydration_receipt_digest is not null and (
      length(new.hydration_receipt_digest)<>71
      or substr(new.hydration_receipt_digest,1,7)<>'sha256:'
      or substr(new.hydration_receipt_digest,8) glob '*[^0-9a-f]*'))
 or not exists(
  select 1 from continuity_hook_attachment_revision ar
  join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
  join continuity_hook_process_generation g on g.process_generation_digest=new.process_generation_digest
  join continuity_hook_invocation_ancestry_receipt r
    on r.receipt_digest=new.ancestry_receipt_digest
  join continuity_reviewed_hook_command c
    on c.command_digest=r.launch_command_digest
  join continuity_session_binding b on b.session_id=a.session_id
  where ar.revision_digest=new.attachment_revision_digest
    and ar.process_generation_digest=g.process_generation_digest
    and g.attachment_id=a.attachment_id
    and r.process_generation_digest=g.process_generation_digest
    and r.ancestry_policy_digest=g.ancestry_policy_digest
    and r.native_pid=g.native_pid and r.native_uid=g.native_uid
    and r.native_start_token=g.native_start_token
    and r.native_artifact_digest=g.native_artifact_digest
    and r.native_artifact_digest=a.native_artifact_digest
    and r.shell_parent_pid=r.native_pid
    and r.shell_parent_uid=r.native_uid
    and r.shell_parent_start_token=r.native_start_token
    and r.hook_parent_pid=r.shell_parent_pid
    and r.hook_parent_uid=r.shell_parent_uid
    and r.hook_parent_start_token=r.shell_parent_start_token
    and r.delivery_id=new.delivery_id and r.observation_digest=new.observation_digest
    and r.shell_pid=new.shell_pid and r.shell_uid=new.shell_uid
    and r.shell_start_token=new.shell_start_token
    and r.hook_pid=new.hook_pid and r.hook_uid=new.hook_uid
    and r.hook_start_token=new.hook_start_token
    and r.shell_artifact_digest=new.shell_artifact_digest
    and r.python_launcher_artifact_digest=new.python_launcher_artifact_digest
    and r.python_runtime_artifact_digest=new.python_runtime_artifact_digest
    and c.attachment_id=a.attachment_id
    and c.external_event_type=new.external_event_type
    and c.external_event_type=r.external_event_type
    and c.topology=r.topology
    and c.client_contract_digest=a.client_contract_digest
    and c.hook_set_digest=a.hook_set_digest
    and c.shell_artifact_digest=r.shell_artifact_digest
    and c.python_launcher_artifact_digest=r.python_launcher_artifact_digest
    and c.python_runtime_artifact_digest=r.python_runtime_artifact_digest
    and (new.hydration_receipt_digest is null or exists(
      select 1 from hydration_receipt h
      where h.receipt_digest=new.hydration_receipt_digest and h.session_id=b.session_id))
)
 or json_extract(new.body_json,'$.event_digest') is not new.event_digest
 or json_extract(new.body_json,'$.attachment_revision_digest')
      is not new.attachment_revision_digest
 or json_extract(new.body_json,'$.process_generation_digest')
      is not new.process_generation_digest
 or json_extract(new.body_json,'$.ancestry_receipt_digest')
      is not new.ancestry_receipt_digest
 or json_extract(new.body_json,'$.spool_digest') is not new.spool_digest
 or json_extract(new.body_json,'$.previous_spool_digest') is not new.previous_spool_digest
 or json_extract(new.body_json,'$.observation_digest') is not new.observation_digest
 or json_extract(new.body_json,'$.delivery_id') is not new.delivery_id
 or json_extract(new.body_json,'$.spool_sequence') is not new.spool_sequence
 or json_extract(new.body_json,'$.external_event_type') is not new.external_event_type
 or json_extract(new.body_json,'$.internal_event_type') is not new.internal_event_type
 or json_extract(new.body_json,'$.shell_pid') is not new.shell_pid
 or json_extract(new.body_json,'$.shell_uid') is not new.shell_uid
 or json_extract(new.body_json,'$.shell_start_token') is not new.shell_start_token
 or json_extract(new.body_json,'$.hook_pid') is not new.hook_pid
 or json_extract(new.body_json,'$.hook_uid') is not new.hook_uid
 or json_extract(new.body_json,'$.hook_start_token') is not new.hook_start_token
 or json_extract(new.body_json,'$.shell_artifact_digest') is not new.shell_artifact_digest
 or json_extract(new.body_json,'$.python_launcher_artifact_digest')
      is not new.python_launcher_artifact_digest
 or json_extract(new.body_json,'$.python_runtime_artifact_digest')
      is not new.python_runtime_artifact_digest
 or json_extract(new.body_json,'$.hydration_receipt_digest') is not new.hydration_receipt_digest
 or json_extract(new.body_json,'$.grants_authority') is not 0
 or json_extract(new.body_json,'$.approval_inherited') is not 0
 or json_extract(new.body_json,'$.created_at') is not new.created_at
 or (new.spool_sequence=1)<>(new.previous_spool_digest is null)
 or (new.spool_sequence>1 and not exists(
   select 1 from continuity_native_event_receipt p
   join continuity_hook_process_generation pg on pg.process_generation_digest=p.process_generation_digest
   join continuity_hook_process_generation ng on ng.process_generation_digest=new.process_generation_digest
   where pg.attachment_id=ng.attachment_id and p.spool_sequence=new.spool_sequence-1
     and p.spool_digest=new.previous_spool_digest
 ))
 or (new.internal_event_type='SESSION_START' and new.hydration_receipt_digest is null)
  or (new.internal_event_type<>'SESSION_START' and new.hydration_receipt_digest is not null)
 or (new.external_event_type='SessionStart'
     and (new.external_turn_id is not null or new.external_trigger_id is not null))
 or (new.external_event_type in ('PreCompact','PostCompact')
     and new.external_trigger_id is null)
begin select raise(abort,'continuity native event kind mismatch'); end;

create trigger continuity_turn_commit_scope_guard before insert on continuity_turn_commit_receipt
when new.body_json<>json_object(
      'binding_digest',new.binding_digest,
      'content_digest',new.content_digest,
      'created_at',new.created_at,
      'item_ref',new.item_ref,
      'previous_turn_commit_digest',new.previous_turn_commit_digest,
      'role',new.role,
      'session_id',new.session_id,
      'store_generation_digest',new.store_generation_digest)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.created_at) is not new.created_at
 or length(new.binding_digest)<>71 or substr(new.binding_digest,1,7)<>'sha256:'
 or substr(new.binding_digest,8) glob '*[^0-9a-f]*'
 or not exists(
  select 1 from continuity_session_binding b
  where b.session_id=new.session_id and b.binding_digest=new.binding_digest
)
 or new.previous_turn_commit_digest is not (
   select receipt_digest from continuity_turn_commit_receipt
   where session_id=new.session_id order by rowid desc limit 1
 )
 or json_extract(new.body_json,'$.session_id') is not new.session_id
 or json_extract(new.body_json,'$.binding_digest') is not new.binding_digest
 or json_extract(new.body_json,'$.role') is not new.role
 or json_extract(new.body_json,'$.item_ref') is not new.item_ref
 or json_extract(new.body_json,'$.content_digest') is not new.content_digest
 or json_extract(new.body_json,'$.store_generation_digest') is not new.store_generation_digest
 or json_extract(new.body_json,'$.previous_turn_commit_digest')
      is not new.previous_turn_commit_digest
 or json_extract(new.body_json,'$.created_at') is not new.created_at
begin select raise(abort,'continuity turn commit scope/body mismatch'); end;

create trigger continuity_internal_event_producer_guard before insert on continuity_internal_event_receipt
when new.body_json<>json_object(
      'attachment_revision_digest',new.attachment_revision_digest,
      'binding_digest',new.binding_digest,
      'created_at',new.created_at,
      'event_digest',new.event_digest,
      'event_kind',new.event_kind,
      'expected_previous_event_digest',new.expected_previous_event_digest,
      'operation_key',new.operation_key,
      'session_id',new.session_id)
 or strftime('%Y-%m-%dT%H:%M:%S+00:00',new.created_at) is not new.created_at
 or length(new.event_digest)<>71 or substr(new.event_digest,1,7)<>'sha256:'
 or substr(new.event_digest,8) glob '*[^0-9a-f]*'
 or length(new.binding_digest)<>71 or substr(new.binding_digest,1,7)<>'sha256:'
 or substr(new.binding_digest,8) glob '*[^0-9a-f]*'
 or (new.expected_previous_event_digest is not null and (
      length(new.expected_previous_event_digest)<>71
      or substr(new.expected_previous_event_digest,1,7)<>'sha256:'
      or substr(new.expected_previous_event_digest,8) glob '*[^0-9a-f]*'))
 or (new.close_request_digest is not null and (
      length(new.close_request_digest)<>71
      or substr(new.close_request_digest,1,7)<>'sha256:'
      or substr(new.close_request_digest,8) glob '*[^0-9a-f]*'))
 or (new.close_receipt_digest is not null and (
      length(new.close_receipt_digest)<>71
      or substr(new.close_receipt_digest,1,7)<>'sha256:'
      or substr(new.close_receipt_digest,8) glob '*[^0-9a-f]*'))
 or not exists(
  select 1 from continuity_session_binding b
  join continuity_hook_attachment_revision ar on ar.revision_digest=new.attachment_revision_digest
  join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
  where b.session_id=new.session_id and b.binding_digest=new.binding_digest
    and a.session_id=b.session_id
)
 or new.expected_previous_event_digest is not (
   select event_digest from session_event_detail
   where session_id=new.session_id order by sequence desc limit 1
 )
 or (
  (new.event_kind in ('USER_TURN_COMMITTED','ASSISTANT_TURN_COMMITTED') and not exists(
    select 1 from continuity_turn_commit_receipt t
    where t.receipt_digest=new.turn_commit_digest and t.session_id=new.session_id
      and t.binding_digest=new.binding_digest
      and ((new.event_kind='USER_TURN_COMMITTED' and t.role='user')
           or (new.event_kind='ASSISTANT_TURN_COMMITTED' and t.role='assistant'))
  ))
  or (new.event_kind='TOOL_EFFECT_CLAIMED' and not exists(
    select 1 from local_effect_claim c
    join continuity_effect_binding b on b.claim_id=c.id and b.job_id=c.job_id
    where c.id=new.effect_claim_id and b.session_id=new.session_id
  ))
  or (new.event_kind='TOOL_EFFECT_COMPLETED' and not exists(
    select 1 from local_effect_receipt er
    join local_effect_claim c on c.id=er.claim_id
    join continuity_effect_binding b on b.claim_id=c.id and b.job_id=c.job_id
    where er.id=new.effect_receipt_id and er.status in ('completed','failed')
      and b.session_id=new.session_id
  ))
  or (new.event_kind='CHECKPOINT_REQUESTED' and new.native_event_receipt_digest is not null
      and not exists(
        select 1 from continuity_native_event_receipt n
        join continuity_hook_attachment_revision ar
          on ar.revision_digest=new.attachment_revision_digest
        join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
        where n.receipt_digest=new.native_event_receipt_digest
          and n.internal_event_type='PRE_COMPACTION'
          and n.attachment_revision_digest=ar.revision_digest
          and a.session_id=new.session_id
      ))
  or (new.event_kind in ('CHECKPOINT_REQUESTED','PRE_CLOSE')
      and new.close_request_digest is not null
      and exists(select 1 from continuity_close_request c
                 where c.request_digest=new.close_request_digest)
      and not exists(
        select 1 from continuity_close_request c
        join continuity_hook_attachment_revision ar
          on ar.revision_digest=new.attachment_revision_digest
        join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
        where c.request_digest=new.close_request_digest and c.session_id=new.session_id
          and a.session_id=new.session_id
      ))
  or (new.event_kind='SESSION_CLOSED' and not exists(
    select 1 from close_receipt c
    join continuity_hook_attachment_revision ar
      on ar.revision_digest=new.attachment_revision_digest and ar.state='frozen'
    join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
    where c.receipt_digest=new.close_receipt_digest and c.session_id=new.session_id
      and a.session_id=new.session_id and ar.close_request_digest=c.request_digest
  ))
  or (new.event_kind='CRASH_RECOVERED' and not (
    exists(
      select 1 from continuity_hook_recovery_resolution r
      join continuity_hook_recovery_case c on c.recovery_case_id=r.recovery_case_id
      join continuity_hook_attachment_revision ar
        on ar.revision_digest=new.attachment_revision_digest
      join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
      where r.resolution_id=new.hook_recovery_resolution_id and r.outcome='restored'
        and ar.state='recovery-required' and ar.hook_recovery_case_id=c.recovery_case_id
        and a.session_id=new.session_id
    )
    or exists(
      select 1 from local_recovery_resolution r
      join local_recovery_case c on c.id=r.recovery_case_id
      join continuity_hook_attachment_revision ar
        on ar.revision_digest=new.attachment_revision_digest
      join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
      where r.id=new.local_recovery_resolution_id
        and r.outcome in ('completed','delivered')
        and ar.state='recovery-required' and ar.local_recovery_case_id=c.id
        and a.session_id=new.session_id
    )
  ))
  or (new.event_kind='CHECKPOINT_REQUESTED' and
      ((new.native_event_receipt_digest is null)=(new.close_request_digest is null)))
  or (new.event_kind='PRE_CLOSE' and new.close_request_digest is null)
  or (new.event_kind='SESSION_CLOSED' and new.close_receipt_digest is null)
  or (new.event_kind='CRASH_RECOVERED' and
      ((new.hook_recovery_resolution_id is null)=(new.local_recovery_resolution_id is null)))
  or json_extract(new.body_json,'$.event_digest') is not new.event_digest
  or json_extract(new.body_json,'$.session_id') is not new.session_id
  or json_extract(new.body_json,'$.binding_digest') is not new.binding_digest
  or json_extract(new.body_json,'$.event_kind') is not new.event_kind
  or json_extract(new.body_json,'$.operation_key') is not new.operation_key
  or json_extract(new.body_json,'$.expected_previous_event_digest')
       is not new.expected_previous_event_digest
  or json_extract(new.body_json,'$.attachment_revision_digest')
       is not new.attachment_revision_digest
  or json_extract(new.body_json,'$.created_at') is not new.created_at
)
begin select raise(abort,'continuity internal event producer mismatch'); end;

create trigger continuity_close_request_internal_scope_guard
before insert on continuity_close_request
when exists(
  select 1 from continuity_internal_event_receipt r
  where r.close_request_digest=new.request_digest and r.session_id<>new.session_id
)
begin select raise(abort,'continuity planned close internal receipt scope mismatch'); end;

create trigger continuity_event_producer_guard before insert on session_event
when exists(select 1 from continuity_session_binding b where b.session_id=new.session_id)
 and (
   new.event_kind not in (
     'SESSION_START','PRE_COMPACTION','POST_COMPACTION',
     'USER_TURN_COMMITTED','ASSISTANT_TURN_COMMITTED',
     'TOOL_EFFECT_CLAIMED','TOOL_EFFECT_COMPLETED','CHECKPOINT_REQUESTED',
     'PRE_CLOSE','SESSION_CLOSED','CRASH_RECOVERED'
   )
   or
   (new.event_kind in ('SESSION_START','PRE_COMPACTION','POST_COMPACTION') and not exists(
      select 1 from continuity_native_event_receipt r
      join continuity_hook_attachment_revision ar
        on ar.revision_digest=r.attachment_revision_digest
      join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
      where r.event_digest=new.event_digest and r.internal_event_type=new.event_kind
        and a.session_id=new.session_id))
   or
   (new.event_kind in ('USER_TURN_COMMITTED','ASSISTANT_TURN_COMMITTED',
                       'TOOL_EFFECT_CLAIMED','TOOL_EFFECT_COMPLETED',
                       'CHECKPOINT_REQUESTED','PRE_CLOSE','SESSION_CLOSED',
                       'CRASH_RECOVERED') and not exists(
      select 1 from continuity_internal_event_receipt r
      where r.event_digest=new.event_digest and r.event_kind=new.event_kind and r.session_id=new.session_id))
 )
begin select raise(abort,'continuity event producer receipt required'); end;

drop trigger continuity_closed_event_guard;
create trigger continuity_closed_event_guard before insert on session_event
when exists(select 1 from continuity_session_binding b where b.session_id=new.session_id)
 and (select status from session where id=new.session_id)<>'open'
 and not (
   (select status from session where id=new.session_id)='closing'
   and (
     (new.event_kind='SESSION_CLOSED' and exists(
       select 1 from continuity_internal_event_receipt r
       join close_receipt cr on cr.receipt_digest=r.close_receipt_digest
       join continuity_hook_attachment_revision ar
         on ar.revision_digest=r.attachment_revision_digest and ar.state='frozen'
       join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
       where r.event_digest=new.event_digest and r.session_id=new.session_id
         and r.event_kind='SESSION_CLOSED' and cr.session_id=new.session_id
         and a.session_id=new.session_id and ar.close_request_digest=cr.request_digest
     ))
     or
     (new.event_kind='CRASH_RECOVERED' and exists(
       select 1 from continuity_internal_event_receipt r
       join continuity_hook_attachment_revision ar
         on ar.revision_digest=r.attachment_revision_digest and ar.state='recovery-required'
       join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
       where r.event_digest=new.event_digest and r.session_id=new.session_id
         and r.event_kind='CRASH_RECOVERED' and a.session_id=new.session_id
         and ar.close_request_digest is not null
         and (
           exists(select 1 from continuity_hook_recovery_resolution hr
                  where hr.resolution_id=r.hook_recovery_resolution_id
                    and hr.recovery_case_id=ar.hook_recovery_case_id
                    and hr.outcome='restored')
           or exists(select 1 from local_recovery_resolution lr
                     join local_recovery_case lc on lc.id=lr.recovery_case_id
                     where lr.id=r.local_recovery_resolution_id
                       and lc.id=ar.local_recovery_case_id
                       and lr.outcome in ('completed','delivered'))
         )
     ))
   )
 )
begin select raise(abort,'continuity session delta frozen'); end;

drop trigger continuity_event_chain_guard;
create trigger continuity_event_chain_guard before insert on session_event_detail
when new.sequence <> coalesce((select max(sequence)+1 from session_event_detail where session_id=new.session_id),1)
 or new.previous_digest is not (select event_digest from session_event_detail where session_id=new.session_id order by sequence desc limit 1)
 or not exists(
   select 1 from session_event e join session s on s.id=e.session_id
   where e.id=new.event_id and e.session_id=new.session_id and e.event_digest=new.event_digest
     and (s.status='open' or (s.status='closing' and exists(
       select 1 from continuity_internal_event_receipt r
       join continuity_hook_attachment_revision ar on ar.revision_digest=r.attachment_revision_digest
       join continuity_hook_attachment a on a.attachment_id=ar.attachment_id
       where r.event_digest=e.event_digest and r.session_id=e.session_id
         and r.event_kind=e.event_kind and a.session_id=e.session_id
         and ((e.event_kind='SESSION_CLOSED' and ar.state='frozen' and r.close_receipt_digest is not null)
              or (e.event_kind='CRASH_RECOVERED' and ar.state='recovery-required'
                  and ar.close_request_digest is not null
                  and (r.hook_recovery_resolution_id is not null
                       or r.local_recovery_resolution_id is not null)))
     )))
 )
begin select raise(abort,'continuity event chain or session state mismatch'); end;

drop trigger continuity_close_receipt_guard;
create trigger continuity_close_receipt_guard before insert on close_receipt
when not exists(
  select 1 from continuity_close_request c
  join continuity_hook_attachment_revision a on a.close_request_digest=c.request_digest and a.state='frozen'
  join continuity_checkpoint cp on cp.checkpoint_digest=c.checkpoint_digest
  join continuity_outbox_binding b on b.close_request_digest=c.request_digest
  join local_outbox_receipt r on r.outbox_id=b.outbox_id
  where c.request_digest=new.request_digest and c.session_id=new.session_id
    and c.checkpoint_digest=new.checkpoint_digest and a.checkpoint_digest=c.checkpoint_digest
    and cp.covered_sequence=c.covered_sequence
    and cp.covered_event_digest=a.pre_close_event_digest
    and b.outbox_id=new.outbox_id
    and json_extract(c.input_json,'$.manifest_digest')=new.manifest_digest
    and exists(select 1 from local_outbox_delivery d where d.outbox_id=r.outbox_id and d.state='delivered')
    and (r.status='delivered' or (r.status='unknown' and exists(
      select 1 from local_recovery_case rc join local_recovery_resolution rr on rr.recovery_case_id=rc.id
      where rc.outbox_id=r.outbox_id and rc.state='resolved' and rr.outcome='delivered')))
)
begin select raise(abort,'continuity terminal close evidence required'); end;

drop trigger continuity_session_close_update_guard;
create trigger continuity_session_close_update_guard before update on session
when exists(select 1 from continuity_session_binding b where b.session_id=old.id)
 and not (
   (new.status is old.status and new.closed_at is old.closed_at and new.close_receipt_digest is old.close_receipt_digest)
   or
   (old.status='open' and new.status='closing' and new.closed_at is null and new.close_receipt_digest is null
    and exists(
      select 1 from continuity_hook_attachment_revision a
      join continuity_hook_attachment h on h.attachment_id=a.attachment_id
      join continuity_close_request c on c.request_digest=a.close_request_digest
      join continuity_checkpoint cp on cp.checkpoint_digest=a.checkpoint_digest
      join session_event_detail d on d.event_digest=a.pre_close_event_digest
      join session_event e on e.id=d.event_id and e.event_kind='PRE_CLOSE'
      join continuity_internal_event_receipt r on r.event_digest=d.event_digest
        and r.event_kind='PRE_CLOSE' and r.close_request_digest=c.request_digest
      where a.state='frozen' and h.session_id=old.id and c.session_id=old.id
        and a.revision_number=(select max(x.revision_number)
          from continuity_hook_attachment_revision x where x.attachment_id=a.attachment_id)
        and cp.session_id=old.id and cp.covered_event_digest=d.event_digest
        and cp.covered_sequence=d.sequence and c.covered_sequence=d.sequence
        and r.attachment_revision_digest=a.previous_revision_digest
    ))
   or
   (old.status='closing' and new.status='closed' and new.closed_at is not null
    and exists(
      select 1 from close_receipt r
      join continuity_hook_attachment_revision a
        on a.close_receipt_digest=r.receipt_digest and a.state='closed'
      join continuity_hook_attachment h on h.attachment_id=a.attachment_id
      join session_event_detail d on d.event_digest=a.session_closed_event_digest
      join session_event e on e.id=d.event_id and e.event_kind='SESSION_CLOSED'
      join continuity_internal_event_receipt ir on ir.event_digest=d.event_digest
        and ir.event_kind='SESSION_CLOSED' and ir.close_receipt_digest=r.receipt_digest
      where r.session_id=old.id and r.receipt_digest=new.close_receipt_digest
        and h.session_id=old.id and a.close_request_digest=r.request_digest
        and a.revision_number=(select max(x.revision_number)
          from continuity_hook_attachment_revision x where x.attachment_id=a.attachment_id)
        and ir.attachment_revision_digest=a.previous_revision_digest
        and new.closed_at=r.created_at
    ))
 )
begin select raise(abort,'continuity session terminal transition evidence required'); end;

create trigger continuity_native_event_no_update before update on continuity_native_event_receipt
begin select raise(abort,'continuity native event receipt append-only'); end;
create trigger continuity_native_event_no_delete before delete on continuity_native_event_receipt
begin select raise(abort,'continuity native event receipt append-only'); end;
create trigger continuity_hook_ancestry_no_update
before update on continuity_hook_invocation_ancestry_receipt
begin select raise(abort,'continuity hook invocation ancestry append-only'); end;
create trigger continuity_hook_ancestry_no_delete
before delete on continuity_hook_invocation_ancestry_receipt
begin select raise(abort,'continuity hook invocation ancestry append-only'); end;
create trigger continuity_reviewed_hook_command_no_update
before update on continuity_reviewed_hook_command
begin select raise(abort,'continuity reviewed hook command append-only'); end;
create trigger continuity_reviewed_hook_command_no_delete
before delete on continuity_reviewed_hook_command
begin select raise(abort,'continuity reviewed hook command append-only'); end;
create trigger continuity_internal_event_no_update before update on continuity_internal_event_receipt
begin select raise(abort,'continuity internal event receipt append-only'); end;
create trigger continuity_internal_event_no_delete before delete on continuity_internal_event_receipt
begin select raise(abort,'continuity internal event receipt append-only'); end;
create trigger continuity_turn_commit_no_update before update on continuity_turn_commit_receipt
begin select raise(abort,'continuity turn commit receipt append-only'); end;
create trigger continuity_turn_commit_no_delete before delete on continuity_turn_commit_receipt
begin select raise(abort,'continuity turn commit receipt append-only'); end;
create trigger continuity_hook_attachment_no_update before update on continuity_hook_attachment
begin select raise(abort,'continuity hook attachment immutable'); end;
create trigger continuity_hook_attachment_no_delete before delete on continuity_hook_attachment
begin select raise(abort,'continuity hook attachment immutable'); end;
create trigger continuity_process_generation_no_update before update on continuity_hook_process_generation
begin select raise(abort,'continuity process generation immutable'); end;
create trigger continuity_process_generation_no_delete before delete on continuity_hook_process_generation
begin select raise(abort,'continuity process generation immutable'); end;
create trigger continuity_process_receipt_no_update before update on continuity_managed_process_receipt
begin select raise(abort,'continuity managed process receipt append-only'); end;
create trigger continuity_process_receipt_no_delete before delete on continuity_managed_process_receipt
begin select raise(abort,'continuity managed process receipt append-only'); end;
create trigger continuity_hook_revision_no_update before update on continuity_hook_attachment_revision
begin select raise(abort,'continuity attachment revision append-only'); end;
create trigger continuity_hook_revision_no_delete before delete on continuity_hook_attachment_revision
begin select raise(abort,'continuity attachment revision append-only'); end;
create trigger continuity_hook_recovery_case_no_update before update on continuity_hook_recovery_case
begin select raise(abort,'continuity hook recovery case append-only'); end;
create trigger continuity_hook_recovery_case_no_delete before delete on continuity_hook_recovery_case
begin select raise(abort,'continuity hook recovery case append-only'); end;
create trigger continuity_hook_recovery_resolution_no_update before update on continuity_hook_recovery_resolution
begin select raise(abort,'continuity hook recovery resolution append-only'); end;
create trigger continuity_hook_recovery_resolution_no_delete before delete on continuity_hook_recovery_resolution
begin select raise(abort,'continuity hook recovery resolution append-only'); end;
"""
