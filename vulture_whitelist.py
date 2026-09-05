"""Intentional keyword names required by structural Protocol compatibility.

Vulture cannot distinguish unused parameters in Protocol stubs from dead local
variables.  These names form public keyword contracts implemented by concrete
stores, so renaming or deleting them would break structural compatibility.
"""

benchmark_suite_id
project_context_id
gap_code
gap_ref
recovery_ref
expected_predecessor_storage_id
predecessor_id
subject_type
call_digest
cooldown_until
purged_at
purge_receipt_digest
dispatch_id
event_types
watermark_claim_id
