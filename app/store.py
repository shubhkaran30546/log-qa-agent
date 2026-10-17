import sqlite3
from datetime import datetime
from pathlib import Path

from app.models.log_record import LogRecord
from app.parser import load_logs

ROOT = Path(__file__).parent.parent
DEFAULT_DB = ROOT / "data/logs.db"
COLUMNS = ["ts", "service", "host", "level", "msg", "trace_id", "status", "latency_ms"]


class Store:
    """SQLite-backed, append-only log store. Reads return LogRecord objects sorted by time."""

    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.connection = sqlite3.connect(db_path)
        self.connection.row_factory = sqlite3.Row  # rows behave like dicts: row["msg"]
        self._create_table()

    def _create_table(self):
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS log_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,          -- ISO 8601, sorts correctly as text
                service TEXT NOT NULL,
                host TEXT NOT NULL,
                level TEXT NOT NULL,
                msg TEXT NOT NULL,
                trace_id TEXT,
                status INTEGER,
                latency_ms INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_service_ts ON log_records(service, ts);
            CREATE INDEX IF NOT EXISTS idx_ts ON log_records(ts);
            CREATE INDEX IF NOT EXISTS idx_trace ON log_records(trace_id);
        ''')

    def load(self, log_records: list[LogRecord]):
        """Replace all stored logs with these. Safe to run repeatedly (no duplicates)."""
        rows = [(r.ts.isoformat(), r.service, r.host, r.level, r.msg, r.trace_id, r.status, r.latency_ms)
                for r in log_records]
        with self.connection:  # one transaction: commits at the end, rolls back on error
            self.connection.execute("DELETE FROM log_records")
            self.connection.executemany(
                f"INSERT INTO log_records ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                rows,
            )

    def query_logs(
        self,
        service: str | None = None,
        level: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        contains: str | None = None,
        limit: int | None = None,
    ) -> list[LogRecord]:
        """Filter logs. Every filter is optional; time range is [start, end)."""
        where, params = [], []
        if service:
            where.append("service = ?"); params.append(service)
        if level:
            where.append("level = ?"); params.append(level)
        if start:
            where.append("ts >= ?"); params.append(start.isoformat())
        if end:
            where.append("ts < ?"); params.append(end.isoformat())
        if contains:
            where.append("msg LIKE ?"); params.append(f"%{contains}%")

        sql = f"SELECT {', '.join(COLUMNS)} FROM log_records"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts"
        if limit:
            sql += " LIMIT ?"; params.append(limit)
        return [self._to_record(row) for row in self.connection.execute(sql, params)]

    def get_trace(self, trace_id: str) -> list[LogRecord]:
        """Every log line from one request, across all services, in time order."""
        rows = self.connection.execute(
            f"SELECT {', '.join(COLUMNS)} FROM log_records WHERE trace_id = ? ORDER BY ts", (trace_id,)
        )
        return [self._to_record(row) for row in rows]

    def time_bounds(self) -> tuple[datetime, datetime] | None:
        row = self.connection.execute("SELECT MIN(ts), MAX(ts) FROM log_records").fetchone()
        return (datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1])) if row[0] else None

    def count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM log_records").fetchone()[0]

    @staticmethod
    def _to_record(row: sqlite3.Row) -> LogRecord:
        return LogRecord(**dict(row))

    def close(self):
        self.connection.close()


if __name__ == "__main__":
    store = Store()
    store.load(load_logs(ROOT / "data/logs.jsonl"))
    print(f"loaded {store.count()} records into {DEFAULT_DB}, spanning {store.time_bounds()}")
