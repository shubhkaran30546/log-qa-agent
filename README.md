# log-qa-agent

An agent that answers questions about application logs, for example *"What went wrong around 13:15?"* or *"Why did checkouts fail this afternoon?"*, by searching a log store, not by reading every line.

The project includes a synthetic log generator with **planted incidents and an answer key**, so you can measure whether the agent actually finds the real root cause.

> **Status:** early work in progress. Log generation, parsing, storage and windowed summaries work. The agent loop, evals, tests and Docker setup are not built yet.

## How it works

```
scripts/generate_logs.py ──► data/logs.jsonl ──► app/parser.py ──► app/store.py (SQLite)
                         └─► data/incidents.json (ground truth)          │
                                                                         ▼
                                                     app/chunker.py (5-min window summaries)
                                                                         │
                                                                         ▼
                                                                 agent (planned)
```

There are about 19k log lines, too many for a model to read at once. Instead the agent:

1. **Skims** about 50 short per-window summaries (level counts, errors per service, top problems, max latency).
2. **Spots** the window where something changed.
3. **Zooms in** with filtered queries (`query_logs`) and follows single requests across services (`get_trace`).

Each window covers **all services**, so a cause and its symptom (for example an orders DB timeout followed by a gateway 504) appear in the same summary.

## Synthetic data

`scripts/generate_logs.py` simulates 4 hours of traffic (2026-10-14 12:00–16:00) across four services:

| Service       | Hosts                          |
|---------------|--------------------------------|
| `api-gateway` | gw-1, gw-2                     |
| `auth`        | auth-1, auth-2                 |
| `orders`      | orders-1, orders-2, orders-3   |
| `payments`    | pay-1, pay-2                   |

Traffic rises toward a midday peak. Background noise (pool stats, health checks, slow-request warnings) is mixed in so the incidents aren't trivially obvious. Three incidents are planted:

| ID               | Window      | Root cause                                                        |
|------------------|-------------|-------------------------------------------------------------------|
| `db_timeouts`    | 13:12–13:21 | Slow unindexed query exhausts the orders DB pool, so the gateway returns 504s |
| `payment_outage` | 14:41–14:55 | Stripe returns 503s; retries, then the circuit breaker opens       |
| `bad_deploy`     | 15:10–15:30 | auth v2.3.1 raises `KeyError 'tenant_id'` on login, rolling out host by host |

While an incident is active, normal successful requests on the affected routes are suppressed, so the logs stay consistent. Generation is deterministic for a given `--seed`.

### Log format

Logs are written as JSON lines, one event per line:

```json
{"ts": "2026-10-14T12:00:03.705", "service": "api-gateway", "host": "gw-2", "level": "INFO",
 "msg": "GET /v1/orders 200", "trace_id": "3bd66a06a22fc331", "status": 200, "latency_ms": 68}
```

`trace_id`, `status` and `latency_ms` are optional. `level` is one of `DEBUG`, `INFO`, `WARN` or `ERROR`. The schema is defined in [app/models/log_record.py](app/models/log_record.py).

## Getting started

Requires Python 3.11+.

```bash
python -m venv venv
source venv/bin/activate
pip install anthropic "pydantic>=2"
```

Run these commands from the repo root. Modules are run with `-m` because they import from `app.`.

```bash
# 1. Generate logs and the answer key
python scripts/generate_logs.py --out data/logs.jsonl --seed 42

# 2. Load the logs into SQLite (data/logs.db)
python -m app.store

# 3. Print 5-minute window summaries; the three incidents should stand out
python -m app.chunker
```

`data/logs.jsonl` and `data/logs.db` are generated files and are git-ignored. Regenerate them with the commands above.

## Using the store

```python
from datetime import datetime
from app.store import Store

store = Store()  # opens data/logs.db

# All orders errors in a time range [start, end)
store.query_logs(service="orders", level="ERROR",
                 start=datetime(2026, 10, 14, 13, 10), end=datetime(2026, 10, 14, 13, 25))

# Substring search
store.query_logs(contains="circuit breaker", limit=20)

# One request across every service, in time order
store.get_trace("3bd66a06a22fc331")
```

## Project layout

```
app/
  models/log_record.py   LogRecord schema (pydantic)
  parser.py              JSON lines to LogRecord
  store.py               SQLite store: load, query_logs, get_trace, time_bounds
  chunker.py             Per-window summaries for the agent to skim
scripts/
  generate_logs.py       Synthetic logs with planted incidents
data/
  incidents.json         Ground truth for evals (tracked)
eval/                    Agent evaluation against incidents.json (planned)
tests/                   Unit tests (planned)
```

## Roadmap

- [ ] Agent loop using Claude with tool use (`summarize_windows`, `query_logs`, `get_trace`)
- [ ] Eval harness: score the agent's answers against `data/incidents.json` (service, root cause, time window)
- [ ] Unit tests for the parser, store and chunker
- [ ] `Dockerfile` and `.env.example` (`ANTHROPIC_API_KEY`)
