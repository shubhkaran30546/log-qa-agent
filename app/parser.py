from pathlib import Path

from app.models.log_record import LogRecord


def load_logs(path: Path) -> list[LogRecord]:
    """Parse a JSON-lines log file into LogRecords, skipping blank lines."""
    with open(path) as f:
        return [LogRecord.model_validate_json(line) for line in f if line.strip()]


if __name__ == "__main__":
    path = Path(__file__).parent.parent / "data/logs.jsonl"
    logs = load_logs(path)
    print(len(logs), logs[0])
