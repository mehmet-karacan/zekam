"""Compatibility entry point for the packaged OpenCode provider ledger probe."""

from zekam.infrastructure.opencode_provider_ledger import main

if __name__ == "__main__":
    raise SystemExit(main())
