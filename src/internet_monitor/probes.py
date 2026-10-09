import asyncio
import socket
import time

import dns.asyncresolver
import dns.exception
import httpx

from internet_monitor.models import (
    CheckRecord,
    Kind,
    ProbeResult,
    Settings,
    classify,
    tcp_address,
    validate_target,
)


async def probe(
    kind: Kind,
    target: str,
    timeout: float,
    client: httpx.AsyncClient,
    resolver: dns.asyncresolver.Resolver,
) -> ProbeResult:
    started = time.perf_counter()
    try:
        validate_target(kind, target)
        async with asyncio.timeout(timeout):
            match kind:
                case Kind.TCP:
                    host, port = tcp_address(target)
                    reader, writer = await asyncio.open_connection(host, port)
                    try:
                        detail = "TCP 连接成功"
                    finally:
                        writer.close()
                        await writer.wait_closed()
                case Kind.HTTP:
                    async with client.stream("GET", target, timeout=timeout) as response:
                        if response.status_code not in {200, 204}:
                            return ProbeResult(
                                kind,
                                target,
                                False,
                                None,
                                f"HTTP 状态码 {response.status_code}（需要 200 或 204）",
                            )
                        detail = f"HTTP {response.status_code}"
                case Kind.DNS:
                    answer = await resolver.resolve(target, "A", lifetime=timeout)
                    detail = "解析成功：" + ", ".join(str(item) for item in answer)
        return ProbeResult(kind, target, True, (time.perf_counter() - started) * 1000, detail)
    except (TimeoutError, httpx.TimeoutException, dns.exception.Timeout):
        detail = f"检测超时（{timeout:g} 秒）"
    except dns.resolver.NXDOMAIN:
        detail = "域名不存在"
    except dns.resolver.NoAnswer:
        detail = "未返回 IPv4 地址记录"
    except dns.resolver.NoNameservers:
        detail = "DNS 服务器不可用"
    except socket.gaierror:
        detail = "主机名解析失败"
    except ConnectionRefusedError:
        detail = "连接被目标拒绝"
    except (OSError, httpx.HTTPError):
        detail = "连接失败，请检查网络、证书或目标地址"
    except ValueError as exc:
        detail = str(exc)
    return ProbeResult(kind, target, False, None, detail)


async def check_network(
    settings: Settings,
    client: httpx.AsyncClient,
    resolver: dns.asyncresolver.Resolver,
) -> CheckRecord:
    results = await asyncio.gather(
        *(
            probe(kind, target, settings.timeout_seconds, client, resolver)
            for kind in Kind
            for target in settings.targets(kind)
        )
    )
    return classify(list(results), settings.degraded_latency_ms)
