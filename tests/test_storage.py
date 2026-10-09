import csv
import datetime
import io
import sqlite3
from dataclasses import replace

import pytest

from internet_monitor.models import Kind, ProbeResult, Settings, Status, classify, utc_now
from internet_monitor.storage import Repository, load_settings, save_settings


def record(status: Status, timestamp=None):
    success = status != Status.OFFLINE
    result = classify(
        [
            ProbeResult(kind, kind.value, success, 25 if success else None, "detail")
            for kind in Kind
        ],
        500,
    )
    return replace(result, status=status, timestamp=timestamp or utc_now())


def test_roundtrip_and_pause_events(tmp_path):
    repository = Repository(tmp_path / "history.sqlite3")
    try:
        repository.append(record(Status.CONNECTED))
        repository.append(record(Status.CONNECTED))
        repository.append(record(Status.OFFLINE))
        repository.append(record(Status.DEGRADED))
        assert len(repository.events()) == 3
        summary = repository.summary()
        assert summary.total == 4
        assert summary.availability == 75
        assert summary.drops == 1
        assert summary.probe_failure_percent == 25
        assert repository.recent()[0].status == Status.DEGRADED
        repository.pause()
        assert all(event.ended is not None for event in repository.events())
        repository.append(record(Status.DEGRADED))
        assert len(repository.events()) == 3
    finally:
        repository.close()


def test_recovery_does_not_count_time_without_observations(tmp_path):
    path = tmp_path / "history.sqlite3"
    repository = Repository(path)
    first = record(Status.CONNECTED, utc_now() - datetime.timedelta(hours=1))
    repository.append(first)
    repository.connection.close()
    recovered = Repository(path)
    try:
        event = recovered.events()[0]
        assert event.ended == first.timestamp
        assert recovered.active_event_id is None
    finally:
        recovered.close()


def test_retention_and_windows(tmp_path):
    repository = Repository(tmp_path / "history.sqlite3")
    now = utc_now()
    try:
        repository.append(record(Status.OFFLINE, now - datetime.timedelta(days=31)))
        repository.append(record(Status.CONNECTED, now - datetime.timedelta(days=8)))
        repository.append(record(Status.OFFLINE, now - datetime.timedelta(days=2)))
        repository.append(record(Status.CONNECTED, now))
        summary = repository.summary(now)
        assert summary.week == 50
        assert summary.month == pytest.approx(200 / 3)
        repository.prune(30)
        assert repository.summary().total == 3
    finally:
        repository.close()


def test_csv_contains_details_and_chinese_headers(tmp_path):
    repository = Repository(tmp_path / "history.sqlite3")
    try:
        repository.append(record(Status.CONNECTED))
        payload = repository.csv_bytes()
        assert payload.startswith(b"\xef\xbb\xbf")
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        assert rows[0][0] == "时间"
        assert rows[1][1] == "连接正常"
        assert '"目标"' in rows[1][-1]
    finally:
        repository.close()


def test_settings_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(interval_seconds=10, dns_targets=["example.com"])
    save_settings(path, settings)
    assert load_settings(path) == settings
    assert not path.with_suffix(".tmp").exists()


def test_same_status_after_restart_is_not_a_new_event(tmp_path):
    path = tmp_path / "history.sqlite3"
    repository = Repository(path)
    repository.append(record(Status.OFFLINE))
    repository.close()
    recovered = Repository(path)
    try:
        recovered.append(record(Status.OFFLINE))
        recovered.pause()
        recovered.append(record(Status.OFFLINE))
        assert len(recovered.events()) == 1
        assert recovered.summary().drops == 1
        recovered.append(record(Status.CONNECTED))
        recovered.append(record(Status.OFFLINE))
        assert [event.status for event in recovered.events()] == [
            Status.OFFLINE,
            Status.CONNECTED,
            Status.OFFLINE,
        ]
        assert recovered.summary().drops == 2
        assert recovered.summary().total == 5
    finally:
        recovered.close()


def test_existing_consecutive_events_only_show_state_changes(tmp_path):
    repository = Repository(tmp_path / "history.sqlite3")
    try:
        statuses = [
            Status.CONNECTED,
            Status.CONNECTED,
            Status.OFFLINE,
            Status.OFFLINE,
            Status.CONNECTED,
        ]
        with repository.connection:
            repository.connection.executemany(
                "INSERT INTO events(started,ended,status,reason) VALUES(?,?,?,?)",
                [
                    (utc_now().isoformat(), utc_now().isoformat(), status.value, "detail")
                    for status in statuses
                ],
            )
        assert [event.status for event in repository.events(2)] == [
            Status.CONNECTED,
            Status.OFFLINE,
        ]
        assert len(repository.events()) == 3
        assert repository.summary().drops == 1
        assert repository.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 5
    finally:
        repository.close()


def test_clear_history_resets_records_events_and_statistics(tmp_path):
    repository = Repository(tmp_path / "history.sqlite3")
    settings_path = tmp_path / "settings.json"
    settings = Settings(interval_seconds=10)
    save_settings(settings_path, settings)
    try:
        repository.append(record(Status.CONNECTED))
        repository.append(record(Status.OFFLINE))
        repository.clear_history()
        assert repository.recent() == []
        assert repository.events() == []
        assert repository.summary().total == 0
        assert repository.summary().drops == 0
        assert repository.summary().availability is None
        assert repository.active_event_id is None
        assert repository.last_status is None
        assert load_settings(settings_path) == settings
        assert len(list(csv.reader(io.StringIO(repository.csv_bytes().decode("utf-8-sig"))))) == 1
        repository.append(record(Status.OFFLINE))
        assert repository.summary().total == 1
        assert repository.summary().drops == 1
        assert len(repository.events()) == 1
    finally:
        repository.close()


def test_clear_history_rolls_back_both_tables_on_failure(tmp_path):
    repository = Repository(tmp_path / "history.sqlite3")
    try:
        repository.append(record(Status.CONNECTED))
        active_id = repository.active_event_id
        repository.connection.executescript("""
            CREATE TRIGGER prevent_delete BEFORE DELETE ON events
            BEGIN SELECT RAISE(ABORT, 'test failure'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError):
            repository.clear_history()
        assert repository.summary().total == 1
        assert len(repository.events()) == 1
        assert repository.active_event_id == active_id
        assert repository.last_status == Status.CONNECTED
    finally:
        repository.close()
