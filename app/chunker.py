"""Turn raw logs into compact per-time-window summaries.

The agent can't read 19k log lines, but it can read ~50 short summaries,
spot the window where something changed, then zoom in with Store.query_logs.
Each window covers ALL services, so cause and symptom (e.g. orders DB timeout
-> gateway 504) land in the same summary.
"""

from collections import Counter
from datetime import datetime, timedelta

from pydantic import BaseModel

from app.store import Store


class WindowSummary(BaseModel):
    start: datetime
    end: datetime
    total: int
    levels: dict[str, int]              # {"INFO": 400, "ERROR": 12, ...}
    errors_by_service: dict[str, int]   # only services with ERROR lines
    warns_by_service: dict[str, int]
    top_problems: list[tuple[str, str, int]]  # (service, msg, count) for WARN/ERROR, most frequent first
    max_latency_ms: int | None


def summarize_window(store: Store, start: datetime, end: datetime, top_n: int = 3) -> WindowSummary:
    records = store.query_logs(start=start, end=end)
    problems = [r for r in records if r.level in ("WARN", "ERROR")]
    latencies = [r.latency_ms for r in records if r.latency_ms is not None]
    return WindowSummary(
        start=start,
        end=end,
        total=len(records),
        levels=dict(Counter(r.level for r in records)),
        errors_by_service=dict(Counter(r.service for r in problems if r.level == "ERROR")),
        warns_by_service=dict(Counter(r.service for r in problems if r.level == "WARN")),
        top_problems=[(svc, msg, n) for (svc, msg), n in Counter((r.service, r.msg) for r in problems).most_common(top_n)],
        max_latency_ms=max(latencies, default=None),
    )


def summarize_windows(
    store: Store,
    start: datetime | None = None,
    end: datetime | None = None,
    window: timedelta = timedelta(minutes=5),
) -> list[WindowSummary]:
    """Summaries for consecutive windows covering [start, end). Defaults to all stored logs."""
    if start is None or end is None:
        bounds = store.time_bounds()
        if bounds is None:
            return []
        start = start or bounds[0].replace(second=0, microsecond=0)
        end = end or bounds[1] + timedelta(microseconds=1)
    summaries = []
    t = start
    while t < end:
        summaries.append(summarize_window(store, t, min(t + window, end)))
        t += window
    return summaries


if __name__ == "__main__":
    store = Store()
    for s in summarize_windows(store):
        errs = ", ".join(f"{svc}={n}" for svc, n in s.errors_by_service.items()) or "-"
        print(f"{s.start:%H:%M}  total={s.total:4}  errors: {errs}")
        for svc, msg, n in s.top_problems if s.errors_by_service else []:
            print(f"         {n:3}x {svc}: {msg[:80]}")
