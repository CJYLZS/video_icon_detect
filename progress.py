"""终端进度条（无额外依赖）。"""

from __future__ import annotations

import sys
import time

_progress_t0: float | None = None
_progress_label: str | None = None


def _format_duration(sec: float) -> str:
    sec = max(0, int(sec + 0.5))
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def reset_progress() -> None:
    global _progress_t0, _progress_label
    _progress_t0 = None
    _progress_label = None


def print_step(step: int, total_steps: int, message: str) -> None:
    print(f"[{step}/{total_steps}] {message}")


def print_progress(
    current: int,
    total: int,
    *,
    label: str = "",
    detail: str = "",
) -> None:
    global _progress_t0, _progress_label
    if total <= 0:
        return

    now = time.perf_counter()
    if current <= 1 or _progress_t0 is None or label != _progress_label:
        _progress_t0 = now
        _progress_label = label

    elapsed = now - _progress_t0
    eta_str = ""
    if 0 < current < total:
        eta_sec = elapsed * (total - current) / current
        eta_str = f" ETA {_format_duration(eta_sec)}"

    width = 28
    filled = int(width * current / total)
    bar = "#" * filled + "-" * (width - filled)
    pct = 100.0 * current / total
    parts = [f"\r{label} [{bar}] {current}/{total} ({pct:.0f}%){eta_str}"]
    if detail:
        parts.append(f" {detail}")
    sys.stdout.write("".join(parts))
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()
        reset_progress()
