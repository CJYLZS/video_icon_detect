"""终端进度条（基于 tqdm）。"""

from __future__ import annotations

import sys
import time

from tqdm import tqdm

_progress_t0: float | None = None


def _format_duration(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}m{s}s"


def print_progress(i: int, total: int, *, label: str = "", detail: str = "") -> None:
    pass


def print_step(step: int, total: int, msg: str) -> None:
    tqdm.write(f"\n{'=' * 48}\n[{step}/{total}] {msg}\n{'=' * 48}")


def create_pbar(total: int, *, desc: str = "") -> tqdm:
    global _progress_t0
    if _progress_t0 is None:
        _progress_t0 = time.monotonic()
    return tqdm(
        total=total,
        desc=desc,
        unit="帧",
        smoothing=0.1,
        file=sys.stdout,
        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] {postfix}",
    )
