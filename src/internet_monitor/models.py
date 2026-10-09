import datetime
import enum
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Kind(enum.StrEnum):
    TCP = "TCP"
    HTTP = "HTTP"
    DNS = "DNS"


class Status(enum.StrEnum):
    CONNECTED = "连接正常"
    DEGRADED = "网络波动"
    OFFLINE = "网络中断"


def valid_host(value: str) -> str:
    if not value or len(value) > 253:
        raise ValueError("请输入有效的域名或 IP 地址")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    ascii_host = value.rstrip(".").encode("idna").decode("ascii")
    if not all(
        re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
        for label in ascii_host.split(".")
    ):
        raise ValueError("请输入有效的域名或 IP 地址")
    return ascii_host


def tcp_address(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("TCP 目标格式为 主机:端口，端口范围 1–65535")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return valid_host(host), int(port)


def validate_target(kind: Kind, value: str) -> str:
    value = value.strip()
    if kind == Kind.TCP:
        tcp_address(value)
    elif kind == Kind.HTTP:
        try:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("请输入 http:// 或 https:// 开头的完整 URL")
            valid_host(url.hostname)
            if url.username or url.password:
                raise ValueError("URL 不应包含用户名或密码")
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError("URL 端口范围为 1–65535")
        except ValueError as exc:
            raise ValueError(f"HTTP 目标无效：{exc}") from exc
    else:
        valid_host(value)
    return value


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tcp_targets: list[str] = Field(
        default_factory=lambda: ["223.5.5.5:80", "119.29.29.29:80"],
        min_length=1,
        max_length=20,
    )
    http_targets: list[str] = Field(
        default_factory=lambda: [
            "https://www.aliyun.com/robots.txt",
            "https://www.qq.com/robots.txt",
        ],
        min_length=1,
        max_length=20,
    )
    dns_targets: list[str] = Field(
        default_factory=lambda: ["www.aliyun.com", "www.tencent.com"],
        min_length=1,
        max_length=20,
    )
    interval_seconds: float = Field(default=5, ge=1, le=3600)
    timeout_seconds: float = Field(default=3, ge=0.2, le=30)
    degraded_latency_ms: float = Field(default=500, ge=50, le=10000)
    retention_days: int = Field(default=90, ge=30, le=365)
    auto_start: bool = True

    @field_validator("tcp_targets", "http_targets", "dns_targets")
    @classmethod
    def validate_targets(cls, values: list[str], info) -> list[str]:
        kind = Kind(info.field_name.split("_")[0].upper())
        targets = [validate_target(kind, value) for value in values]
        if len(set(targets)) != len(targets):
            raise ValueError("同类目标不能重复")
        return targets

    def targets(self, kind: Kind) -> list[str]:
        match kind:
            case Kind.TCP:
                return self.tcp_targets
            case Kind.HTTP:
                return self.http_targets
            case Kind.DNS:
                return self.dns_targets


@dataclass(frozen=True)
class ProbeResult:
    kind: Kind
    target: str
    success: bool
    latency_ms: float | None
    detail: str


@dataclass(frozen=True)
class CheckRecord:
    timestamp: datetime.datetime
    status: Status
    latency_ms: float | None
    reason: str
    results: list[ProbeResult]

    def passed(self, kind: Kind) -> bool:
        return any(result.success for result in self.results if result.kind == kind)

    @property
    def failure_percent(self) -> float:
        return 100 * sum(not result.success for result in self.results) / len(self.results)


@dataclass(frozen=True)
class NetworkEvent:
    started: datetime.datetime
    ended: datetime.datetime | None
    status: Status
    reason: str


@dataclass(frozen=True)
class Summary:
    total: int
    availability: float | None
    average_latency_ms: float | None
    jitter_ms: float | None
    probe_failure_percent: float | None
    drops: int
    today: float | None
    week: float | None
    month: float | None


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def classify(results: list[ProbeResult], threshold: float) -> CheckRecord:
    passed = {kind: any(r.success for r in results if r.kind == kind) for kind in Kind}
    latency = None
    for kind in Kind:
        samples = [r.latency_ms for r in results if r.kind == kind and r.success]
        if samples:
            latency = min(value for value in samples if value is not None)
            break
    if not any(passed.values()):
        status, reason = Status.OFFLINE, "TCP、HTTP 与 DNS 检测均失败"
    elif not all(passed.values()):
        failed = "、".join(kind.value for kind, success in passed.items() if not success)
        status, reason = Status.DEGRADED, f"{failed} 检测失败"
    elif latency is not None and latency >= threshold:
        status, reason = Status.DEGRADED, f"延迟过高（{latency:.0f} 毫秒）"
    else:
        status, reason = Status.CONNECTED, "TCP、HTTP 与 DNS 检测均通过"
    return CheckRecord(utc_now(), status, latency, reason, results)
