"""Small display-formatting helpers."""

from __future__ import annotations

import time


def fmt_duration(seconds: float | None) -> str:
    """``93`` -> ``1m33s``; ``4000`` -> ``1h06m``; ``None`` -> ``-``."""
    if seconds is None or seconds < 0:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def fmt_time(ts: float | None, now: float | None = None) -> str:
    """Clock time for today, otherwise month/day plus time."""
    if ts is None:
        return "-"
    now = time.time() if now is None else now
    t = time.localtime(ts)
    if time.localtime(now)[:3] == t[:3]:
        return time.strftime("%H:%M:%S", t)
    return time.strftime("%b %d %H:%M", t)


def truncate(text: str, width: int) -> str:
    """Shorten ``text`` to ``width`` characters with an ellipsis."""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"
