"""Cross-platform process-incarnation tokens for local lease recovery."""

from __future__ import annotations

from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed


def process_incarnation_token(pid: int) -> str | None:
    """Return a PID-reuse-safe token, or ``None`` only for a dead process."""
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2_147_483_647:
        raise ValidationFailed("Process PID 1..2147483647 olmali")
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - installed Mac acceptance profile
        raise ConfigurationError("Process incarnation probe icin psutil gerekli") from exc
    try:
        process = psutil.Process(pid)
        created_micros = round(float(process.create_time()) * 1_000_000)
        return f"psutil-create-time-micros:{created_micros}"
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except psutil.AccessDenied as exc:
        raise PolicyViolation("Process incarnation access denied; lease korunuyor") from exc
