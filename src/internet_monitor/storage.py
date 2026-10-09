import csv
import datetime
import io
import json
import os
import pathlib
import sqlite3

from platformdirs import user_data_path
from pydantic import TypeAdapter, ValidationError

from internet_monitor.models import (
    CheckRecord,
    Kind,
    NetworkEvent,
    Settings,
    Status,
    Summary,
    utc_now,
)

RECORD_ADAPTER = TypeAdapter(CheckRecord)


def data_directory() -> pathlib.Path:
    override = os.environ.get("INTERNET_MONITOR_DATA_DIR")
    return (
        pathlib.Path(override) if override else user_data_path("internet-monitor", appauthor=False)
    )


def load_settings(path: pathlib.Path) -> Settings:
    if not path.exists():
        return Settings()
    return Settings.model_validate_json(path.read_text(encoding="utf-8"))


def save_settings(path: pathlib.Path, settings: Settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)


class Repository:
    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                latency REAL,
                failed INTEGER NOT NULL,
                probes INTEGER NOT NULL,
                record TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS checks_time ON checks(timestamp);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                started TEXT NOT NULL,
                ended TEXT,
                status TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE VIEW IF NOT EXISTS state_changes AS
            SELECT id, started, ended, status, reason FROM (
                SELECT *, LAG(status) OVER (ORDER BY id) AS previous_status FROM events
            ) WHERE previous_status IS NULL OR status != previous_status;
        """)
        self.connection.commit()
        self.active_event_id: int | None = None
        latest = self.connection.execute(
            "SELECT status FROM checks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.last_status = Status(latest[0]) if latest else None
        # Unfinished sessions end at their last actual observation.
        with self.connection:
            self.connection.execute("""
                UPDATE events SET ended = COALESCE(
                    (SELECT MAX(timestamp) FROM checks WHERE timestamp >= events.started),
                    started
                ) WHERE ended IS NULL
            """)

    def append(self, record: CheckRecord) -> None:
        timestamp = record.timestamp.isoformat()
        with self.connection:
            self.connection.execute(
                "INSERT INTO checks(timestamp,status,latency,failed,probes,record) VALUES(?,?,?,?,?,?)",
                (
                    timestamp,
                    record.status.value,
                    record.latency_ms,
                    sum(not result.success for result in record.results),
                    len(record.results),
                    RECORD_ADAPTER.dump_json(record).decode(),
                ),
            )
            if record.status != self.last_status:
                self.finish_event(record.timestamp)
                cursor = self.connection.execute(
                    "INSERT INTO events(started,status,reason) VALUES(?,?,?)",
                    (timestamp, record.status.value, record.reason),
                )
                self.active_event_id = cursor.lastrowid
                self.last_status = record.status

    def finish_event(self, now: datetime.datetime) -> None:
        if self.active_event_id is not None:
            self.connection.execute(
                "UPDATE events SET ended=? WHERE id=?",
                (now.isoformat(), self.active_event_id),
            )
        self.active_event_id = None

    def pause(self) -> None:
        with self.connection:
            self.finish_event(utc_now())

    def clear_history(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM checks")
            self.connection.execute("DELETE FROM events")
        self.active_event_id = None
        self.last_status = None

    def recent(self, limit: int = 60) -> list[CheckRecord]:
        rows = self.connection.execute(
            "SELECT record FROM checks ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [RECORD_ADAPTER.validate_json(row[0]) for row in rows]

    def events(self, limit: int = 100) -> list[NetworkEvent]:
        rows = self.connection.execute(
            "SELECT started,ended,status,reason FROM state_changes ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            NetworkEvent(
                datetime.datetime.fromisoformat(started),
                datetime.datetime.fromisoformat(ended) if ended else None,
                Status(status),
                reason,
            )
            for started, ended, status, reason in rows
        ]

    def availability_since(self, since: datetime.datetime) -> float | None:
        total, available = self.connection.execute(
            "SELECT COUNT(*), SUM(status != ?) FROM checks WHERE timestamp >= ?",
            (Status.OFFLINE.value, since.isoformat()),
        ).fetchone()
        return 100 * available / total if total else None

    def summary(self, now: datetime.datetime | None = None) -> Summary:
        now = now or utc_now()
        midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        total, available, latency, failed, probes = self.connection.execute(
            "SELECT COUNT(*), SUM(status != ?), AVG(latency), SUM(failed), SUM(probes) FROM checks",
            (Status.OFFLINE.value,),
        ).fetchone()
        drops = self.connection.execute(
            "SELECT COUNT(*) FROM state_changes WHERE status=?",
            (Status.OFFLINE.value,),
        ).fetchone()[0]
        recent = self.recent()
        differences = [
            abs(a.latency_ms - b.latency_ms)
            for a, b in zip(recent, recent[1:], strict=False)
            if a.latency_ms is not None
            and b.latency_ms is not None
            and a.passed(Kind.TCP)
            and b.passed(Kind.TCP)
        ]
        return Summary(
            total,
            100 * available / total if total else None,
            latency,
            sum(differences) / len(differences) if differences else None,
            100 * failed / probes if probes else None,
            drops,
            self.availability_since(midnight.astimezone(datetime.UTC)),
            self.availability_since(now - datetime.timedelta(days=7)),
            self.availability_since(now - datetime.timedelta(days=30)),
        )

    def prune(self, days: int) -> None:
        cutoff = (utc_now() - datetime.timedelta(days=days)).isoformat()
        with self.connection:
            self.connection.execute("DELETE FROM checks WHERE timestamp < ?", (cutoff,))
            self.connection.execute("DELETE FROM events WHERE ended < ?", (cutoff,))

    def csv_bytes(self) -> bytes:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "时间",
                "网络状态",
                "延迟（毫秒）",
                "TCP",
                "HTTP",
                "DNS",
                "探测失败率（%）",
                "原因",
                "目标详情",
            ]
        )
        cursor = self.connection.execute("SELECT record FROM checks ORDER BY id")
        for (raw,) in cursor:
            record = RECORD_ADAPTER.validate_json(raw)
            writer.writerow(
                [
                    record.timestamp.astimezone().isoformat(timespec="seconds"),
                    record.status.value,
                    round(record.latency_ms, 2) if record.latency_ms is not None else "",
                    *["通过" if record.passed(kind) else "失败" for kind in Kind],
                    round(record.failure_percent, 2),
                    record.reason,
                    json.dumps(
                        [
                            {
                                "类型": r.kind.value,
                                "目标": r.target,
                                "成功": r.success,
                                "延迟毫秒": r.latency_ms,
                                "详情": r.detail,
                            }
                            for r in record.results
                        ],
                        ensure_ascii=False,
                    ),
                ]
            )
        return stream.getvalue().encode("utf-8-sig")

    def close(self) -> None:
        self.pause()
        self.connection.close()


def settings_error(exc: ValidationError) -> str:
    names = {
        "tcp_targets": "TCP 目标",
        "http_targets": "HTTP 目标",
        "dns_targets": "DNS 目标",
        "interval_seconds": "检测间隔",
        "timeout_seconds": "超时时间",
        "degraded_latency_ms": "延迟警戒值",
        "retention_days": "记录保留天数",
    }
    messages = []
    for error in exc.errors():
        name = names.get(str(error["loc"][0]), "设置")
        if error["type"] == "value_error":
            detail = str(error["ctx"]["error"])
        elif error["type"] in {"too_short", "too_long"}:
            detail = "每类须设置 1–20 个目标"
        else:
            detail = "数值无效或超出允许范围"
        messages.append(f"{name}：{detail}")
    return "\n".join(messages)
