import asyncio

import dns.asyncresolver
import httpx

from internet_monitor.models import Kind, ProbeResult, Settings, classify
from internet_monitor.monitor import Monitor
from internet_monitor.storage import Repository


async def test_pause_cancels_pending_probe_without_recording(monkeypatch, tmp_path):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending(*args):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    monkeypatch.setattr("internet_monitor.monitor.check_network", pending)
    repository = Repository(tmp_path / "history.sqlite3")
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            monitor = Monitor(
                Settings(),
                repository,
                client,
                dns.asyncresolver.Resolver(),
                lambda: None,
                lambda error: None,
            )
            monitor.start()
            await entered.wait()
            await monitor.stop()
            assert cancelled.is_set()
            assert not monitor.running
            assert not monitor.checking
            assert repository.summary().total == 0
    finally:
        repository.close()


async def test_manual_checks_do_not_overlap(monkeypatch, tmp_path):
    active = 0
    peak = 0

    async def check(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return classify([ProbeResult(kind, kind.value, True, 10, "ok") for kind in Kind], 500)

    monkeypatch.setattr("internet_monitor.monitor.check_network", check)
    repository = Repository(tmp_path / "history.sqlite3")
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            monitor = Monitor(
                Settings(),
                repository,
                client,
                dns.asyncresolver.Resolver(),
                lambda: None,
                lambda error: None,
            )
            await asyncio.gather(monitor.check_once(), monitor.check_once())
            assert repository.summary().total == 2
            assert peak == 1
    finally:
        repository.close()


async def test_clear_history_waits_for_pending_check_and_keeps_monitoring(monkeypatch, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pending(*args):
        entered.set()
        await release.wait()
        return classify([ProbeResult(kind, kind.value, True, 10, "ok") for kind in Kind], 500)

    monkeypatch.setattr("internet_monitor.monitor.check_network", pending)
    repository = Repository(tmp_path / "history.sqlite3")
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            monitor = Monitor(
                Settings(),
                repository,
                client,
                dns.asyncresolver.Resolver(),
                lambda: None,
                lambda error: None,
            )
            try:
                monitor.start()
                await entered.wait()
                clearing = asyncio.create_task(monitor.clear_history())
                await asyncio.sleep(0)
                assert not clearing.done()
                release.set()
                await clearing
                assert monitor.running
                assert monitor.latest is None
                assert monitor.connected_since is None
                assert repository.recent() == []
                assert repository.events() == []
                await monitor.check_once()
                assert repository.summary().total == 1
                assert len(repository.events()) == 1
            finally:
                await monitor.stop()
    finally:
        repository.close()
