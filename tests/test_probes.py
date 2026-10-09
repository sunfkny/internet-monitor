import asyncio
from unittest.mock import AsyncMock

import dns.asyncresolver
import dns.resolver
import httpx
import pytest

from internet_monitor.models import Kind, ProbeResult, Settings
from internet_monitor.probes import check_network, probe


@pytest.mark.parametrize("status,success", [(200, True), (204, True), (501, False)])
async def test_http_status(status, success):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as client:
        result = await probe(
            Kind.HTTP, "https://example.com", 1, client, dns.asyncresolver.Resolver()
        )
    assert result.success is success
    assert (result.latency_ms is not None) is success
    assert str(status) in result.detail


async def test_real_tcp_connection_is_closed():
    closed = asyncio.Event()

    async def handle(reader, writer):
        await reader.read()
        writer.close()
        await writer.wait_closed()
        closed.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server, httpx.AsyncClient(trust_env=False) as client:
        port = server.sockets[0].getsockname()[1]
        result = await probe(Kind.TCP, f"127.0.0.1:{port}", 1, client, dns.asyncresolver.Resolver())
        assert result.success
        await asyncio.wait_for(closed.wait(), 1)


async def test_dns_failure_is_a_result():
    resolver = dns.asyncresolver.Resolver()
    resolver.resolve = AsyncMock(side_effect=dns.resolver.NXDOMAIN())
    async with httpx.AsyncClient(trust_env=False) as client:
        result = await probe(Kind.DNS, "missing.example", 1, client, resolver)
    assert not result.success
    assert result.detail == "域名不存在"


async def test_timeout_and_cancellation():
    resolver = dns.asyncresolver.Resolver()
    resolver.resolve = AsyncMock(side_effect=lambda *args, **kwargs: None)

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    resolver.resolve = slow
    async with httpx.AsyncClient(trust_env=False) as client:
        result = await probe(Kind.DNS, "example.com", 0.02, client, resolver)
        assert not result.success
        assert "超时" in result.detail
        task = asyncio.create_task(probe(Kind.DNS, "example.com", 1, client, resolver))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_all_targets_are_concurrent(monkeypatch):
    active = 0
    peak = 0

    async def fake(kind, target, *args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ProbeResult(kind, target, True, 10, "success")

    monkeypatch.setattr("internet_monitor.probes.probe", fake)
    async with httpx.AsyncClient(trust_env=False) as client:
        record = await check_network(Settings(), client, dns.asyncresolver.Resolver())
    assert len(record.results) == 6
    assert peak == 6
