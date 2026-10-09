import asyncio
import datetime
from collections.abc import Callable

import dns.asyncresolver
import httpx

from internet_monitor.models import CheckRecord, Settings, Status
from internet_monitor.probes import check_network
from internet_monitor.storage import Repository


class Monitor:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        client: httpx.AsyncClient,
        resolver: dns.asyncresolver.Resolver,
        on_change: Callable[[], None],
        on_error: Callable[[str], None],
    ):
        self.settings = settings
        self.repository = repository
        self.client = client
        self.resolver = resolver
        self.on_change = on_change
        self.on_error = on_error
        self.task: asyncio.Task | None = None
        self.latest: CheckRecord | None = None
        self.connected_since: datetime.datetime | None = None
        self.checking = False
        self.lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> None:
        if not self.running:
            self.task = asyncio.create_task(self.run())
            self.on_change()

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        self.repository.pause()
        self.connected_since = None
        self.on_change()

    async def check_once(self) -> None:
        async with self.lock:
            self.checking = True
            self.on_change()
            try:
                record = await check_network(self.settings, self.client, self.resolver)
                self.repository.append(record)
                self.latest = record
                if record.status == Status.OFFLINE:
                    self.connected_since = None
                elif self.connected_since is None:
                    self.connected_since = record.timestamp
                self.repository.prune(self.settings.retention_days)
            finally:
                self.checking = False
                self.on_change()

    async def clear_history(self) -> None:
        async with self.lock:
            self.repository.clear_history()
            self.latest = None
            self.connected_since = None
            self.on_change()

    async def run(self) -> None:
        try:
            while True:
                await self.check_once()
                await asyncio.sleep(self.settings.interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.repository.pause()
            self.on_error(f"监测已停止，请检查数据目录权限。错误类型：{type(exc).__name__}")
        finally:
            self.task = None
            self.on_change()
