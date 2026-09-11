"""Console-free Windows entry point for the bounded OS supervisor tick."""

from __future__ import annotations

import argparse
import os
from contextlib import redirect_stderr, redirect_stdout
from typing import NoReturn


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("operation", choices=("tick",))
    parser.add_argument("--home", required=True)
    return parser.parse_args()


def run() -> None:
    """Run only the reviewed supervisor operation without creating a console window."""

    with (
        open(os.devnull, "w", encoding="utf-8") as sink,
        redirect_stdout(sink),
        redirect_stderr(sink),
    ):
        arguments = _arguments()
        if arguments.operation != "tick":  # pragma: no cover - argparse enforces this
            _reject()
        from zekam.interfaces.cli.local_runtime import tick_command

        tick_command(owner_id="zekam-os-supervisor", home=str(arguments.home))


def _reject() -> NoReturn:
    raise SystemExit(2)
