
from datetime import datetime
from typing import Literal
from pydantic import BaseModel

class LogRecord(BaseModel):
    ts: datetime
    service: str
    host: str
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"]
    msg: str
    trace_id: str | None = None
    status: int | None = None
    latency_ms: int | None = None

    