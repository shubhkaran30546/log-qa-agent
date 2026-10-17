"""Generate realistic synthetic JSON-lines logs with planted incidents.

The incidents are the "ground truth" we'll later ask the agent about,
so we can check whether it actually finds them. Ground truth is written
to a sidecar file (incidents.json) next to the logs.

Usage:
    python scripts/generate_logs.py --out data/logs.jsonl --seed 42
"""

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

START = datetime(2026, 10, 14, 12, 0, 0)
DURATION = timedelta(hours=4)

HOSTS = {
    "api-gateway": ["gw-1", "gw-2"],
    "auth": ["auth-1", "auth-2"],
    "orders": ["orders-1", "orders-2", "orders-3"],
    "payments": ["pay-1", "pay-2"],
}

# A checkout flows gateway -> auth -> orders -> payments. Other requests are
# simpler. Each entry: (route, services touched in order).
ROUTES = [
    ("POST /v1/checkout", ["api-gateway", "auth", "orders", "payments"]),
    ("GET /v1/orders", ["api-gateway", "auth", "orders"]),
    ("POST /v1/login", ["api-gateway", "auth"]),
    ("GET /health", ["api-gateway"]),
]
ROUTE_WEIGHTS = [20, 40, 25, 15]

SERVICE_MSGS = {
    "auth": ["token validated", "session refreshed"],
    "orders": ["order lookup ok rows={n}", "order created id=ord_{id}", "inventory reserved sku=SKU-{n}"],
    "payments": ["charge authorized provider=stripe amount_cents={n}", "idempotency key reused, returning cached result"],
}

BACKGROUND = [
    ("orders", "INFO", "db pool stats active={a} idle={i} max=20"),
    ("payments", "INFO", "provider healthcheck ok provider=stripe latency_ms={l}"),
    ("auth", "DEBUG", "jwks cache refreshed keys=3"),
    ("api-gateway", "WARN", "slow request route=GET /v1/orders latency_ms={l}"),
]


class Outage(NamedTuple):
    """A route that can't succeed during [start, end), optionally only on one host."""
    route: str
    start: datetime
    end: datetime
    host: str | None = None


class Incident(NamedTuple):
    events: list[dict]   # the failure log lines
    truth: dict          # answer key, written to incidents.json
    outages: list[Outage]  # where normal successful traffic must be suppressed


def event(ts, service, level, msg, host=None, **extra) -> dict:
    return {
        "ts": ts.isoformat(timespec="milliseconds"),
        "service": service,
        "host": host or random.choice(HOSTS[service]),
        "level": level,
        "msg": msg,
        **extra,
    }


def fmt(template: str) -> str:
    return template.format(
        n=random.randint(1, 9999), id=uuid.uuid4().hex[:8],
        a=random.randint(2, 8), i=random.randint(8, 16), l=random.randint(20, 400),
    )


def is_down(outages: list[Outage], route: str, ts: datetime, hosts: set[str]) -> bool:
    return any(
        o.route == route and o.start <= ts < o.end and (o.host is None or o.host in hosts)
        for o in outages
    )


def normal_request(ts: datetime, outages: list[Outage]) -> list[dict]:
    route, path = random.choices(ROUTES, weights=ROUTE_WEIGHTS)[0]
    hosts = {svc: random.choice(HOSTS[svc]) for svc in path}
    if is_down(outages, route, ts, set(hosts.values())):
        return []  # this request would have failed; the incident already logs failures
    trace = uuid.uuid4().hex[:16]
    events, t = [], ts
    for svc in path[1:]:
        t += timedelta(milliseconds=random.randint(2, 40))
        msg = "login success" if route == "POST /v1/login" else fmt(random.choice(SERVICE_MSGS[svc]))
        events.append(event(t, svc, "INFO", msg, host=hosts[svc], trace_id=trace))
    latency = int((t - ts).total_seconds() * 1000) + random.randint(5, 60)
    events.append(event(ts + timedelta(milliseconds=latency), "api-gateway", "INFO",
                        f"{route} 200", host=hosts["api-gateway"], trace_id=trace, status=200, latency_ms=latency))
    return events


def traffic_rate(minute: int) -> int:
    """Requests per minute; rises toward a mid-window peak like daytime traffic."""
    total = DURATION.total_seconds() / 60
    peak = 1 - abs(minute - total / 2) / (total / 2)
    return int(15 + 25 * peak + random.randint(-3, 3))


# --- Incidents --------------------------------------------------------------
# Each incident returns an Incident(events, truth, outages).


def incident_db_timeouts() -> Incident:
    """orders DB pool exhausted by a slow query; checkouts and order reads fail."""
    start = START + timedelta(hours=1, minutes=12)
    end = start + timedelta(minutes=9)
    events = []
    # Early warning: pool usage climbs before it saturates
    for k, active in enumerate([14, 17, 19, 20]):
        events.append(event(start - timedelta(minutes=4 - k), "orders", "WARN",
                            f"db pool stats active={active} idle={20 - active} max=20"))
    events.append(event(start - timedelta(minutes=1), "orders", "WARN",
                        "slow query 8412ms: SELECT * FROM orders WHERE customer_email LIKE '%...' (seq scan)"))
    t = start
    while t < end:
        trace = uuid.uuid4().hex[:16]
        events.append(event(t, "orders", "ERROR",
                            "db timeout after 5000ms: could not acquire connection from pool (active=20 max=20)",
                            trace_id=trace))
        route = random.choice(["GET /v1/orders", "POST /v1/checkout"])
        events.append(event(t + timedelta(milliseconds=5010), "api-gateway", "ERROR",
                            f"{route} 504 upstream=orders", trace_id=trace, status=504, latency_ms=5010))
        t += timedelta(seconds=random.uniform(0.5, 4))
    events.append(event(end, "orders", "INFO", "db pool recovered active=6 idle=14 max=20"))
    truth = {
        "id": "db_timeouts", "service": "orders", "root_cause": "slow unindexed query exhausted DB connection pool",
        "start": start.isoformat(), "end": end.isoformat(), "symptoms": ["orders db timeout", "gateway 504"],
    }
    outages = [Outage("GET /v1/orders", start, end), Outage("POST /v1/checkout", start, end)]
    return Incident(events, truth, outages)


def incident_payment_outage() -> Incident:
    """Upstream payment provider returns 503s; retries, then the circuit breaker fails fast."""
    start = START + timedelta(hours=2, minutes=41)
    breaker_open = start + timedelta(minutes=3)
    end = start + timedelta(minutes=14)
    events = [event(breaker_open, "payments", "WARN", "circuit breaker OPEN provider=stripe failure_rate=0.94")]
    t = start
    while t < end:
        trace = uuid.uuid4().hex[:16]
        if t < breaker_open:
            # Breaker closed: each charge retries 3x against the provider, then gives up
            for attempt in range(1, 4):
                events.append(event(t + timedelta(milliseconds=attempt * 300), "payments", "WARN" if attempt < 3 else "ERROR",
                                    f"provider error provider=stripe status=503 attempt={attempt}/3", trace_id=trace))
            events.append(event(t + timedelta(milliseconds=1000), "payments", "ERROR",
                                "charge failed: provider unavailable, giving up after 3 attempts", trace_id=trace))
            events.append(event(t + timedelta(milliseconds=1020), "api-gateway", "ERROR",
                                "POST /v1/checkout 502 upstream=payments", trace_id=trace, status=502, latency_ms=1020))
        else:
            # Breaker open: reject immediately without calling the provider
            events.append(event(t + timedelta(milliseconds=2), "payments", "ERROR",
                                "charge rejected: circuit breaker open provider=stripe", trace_id=trace))
            events.append(event(t + timedelta(milliseconds=15), "api-gateway", "ERROR",
                                "POST /v1/checkout 503 upstream=payments", trace_id=trace, status=503, latency_ms=15))
        t += timedelta(seconds=random.uniform(1, 6))
    events.append(event(end, "payments", "INFO", "circuit breaker CLOSED provider=stripe"))
    truth = {
        "id": "payment_outage", "service": "payments", "root_cause": "third-party payment provider (stripe) returning 503",
        "start": start.isoformat(), "end": end.isoformat(),
        "symptoms": ["provider 503", "circuit breaker open", "gateway 502/503 on checkout"],
    }
    return Incident(events, truth, [Outage("POST /v1/checkout", start, end)])


def incident_bad_deploy() -> Incident:
    """Auth deploy v2.3.1 rolls out host by host and breaks logins; rolled back to v2.3.0."""
    start = START + timedelta(hours=3, minutes=10)
    end = start + timedelta(minutes=20)
    # Rolling deploy: auth-1 first, auth-2 three minutes later
    rollout = {"auth-1": start, "auth-2": start + timedelta(minutes=3)}
    events = [event(at, "auth", "INFO", "deploy started version=v2.3.1", host=h) for h, at in rollout.items()]
    t = start + timedelta(seconds=20)
    while t < end:
        broken_hosts = [h for h, at in rollout.items() if at <= t]
        trace = uuid.uuid4().hex[:16]
        events.append(event(t, "auth", "ERROR", "login failed: KeyError 'tenant_id'",
                            host=random.choice(broken_hosts), trace_id=trace))
        events.append(event(t + timedelta(milliseconds=20), "api-gateway", "ERROR",
                            "POST /v1/login 500 upstream=auth", trace_id=trace, status=500, latency_ms=20))
        # Fewer failures while only half the fleet is on the bad version
        t += timedelta(seconds=random.uniform(0.5, 2) * (2 if len(broken_hosts) == 1 else 1))
    events += [event(end, "auth", "INFO", "deploy rolled back version=v2.3.0", host=h) for h in rollout]
    truth = {
        "id": "bad_deploy", "service": "auth",
        "root_cause": "auth deploy v2.3.1 raised KeyError 'tenant_id' on login",
        "start": start.isoformat(), "end": end.isoformat(),
        "symptoms": ["gateway 500 on login", "auth KeyError 'tenant_id'", "errors on auth-1 before auth-2"],
    }
    outages = [Outage("POST /v1/login", at, end, host=h) for h, at in rollout.items()]
    return Incident(events, truth, outages)


INCIDENTS = [incident_db_timeouts, incident_payment_outage, incident_bad_deploy]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/logs.jsonl")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)
    uuid.uuid4 = lambda: uuid.UUID(int=random.getrandbits(128))  # deterministic ids per seed

    # Incidents first, so normal traffic knows which requests to suppress
    incidents = [fn() for fn in INCIDENTS]
    outages = [o for inc in incidents for o in inc.outages]
    events = [e for inc in incidents for e in inc.events]

    total_minutes = int(DURATION.total_seconds() // 60)
    for minute in range(total_minutes):
        for _ in range(traffic_rate(minute)):
            ts = START + timedelta(minutes=minute, seconds=random.uniform(0, 60))
            events.extend(normal_request(ts, outages))
        if random.random() < 0.3:
            svc, level, tmpl = random.choice(BACKGROUND)
            events.append(event(START + timedelta(minutes=minute, seconds=random.uniform(0, 60)), svc, level, fmt(tmpl)))

    events.sort(key=lambda e: e["ts"])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    ground_truth = [inc.truth for inc in incidents]
    (out.parent / "incidents.json").write_text(json.dumps(ground_truth, indent=2))

    print(f"wrote {len(events)} events to {out}, {len(ground_truth)} incidents to {out.parent / 'incidents.json'}")


if __name__ == "__main__":
    main()
