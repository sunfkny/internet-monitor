import pytest
from pydantic import ValidationError

from internet_monitor.models import Kind, ProbeResult, Settings, Status, classify, tcp_address


def results(tcp=True, http=True, dns=True, latency=30):
    return [
        ProbeResult(kind, kind.value, success, latency if success else None, "result")
        for kind, success in zip(Kind, [tcp, http, dns], strict=True)
    ]


@pytest.mark.parametrize(
    "tcp,http,dns,latency,expected",
    [
        (True, True, True, 30, Status.CONNECTED),
        (True, True, True, 500, Status.DEGRADED),
        (True, False, True, 30, Status.DEGRADED),
        (False, False, True, 30, Status.DEGRADED),
        (False, False, False, 30, Status.OFFLINE),
    ],
)
def test_classification(tcp, http, dns, latency, expected):
    record = classify(results(tcp, http, dns, latency), 500)
    assert record.status == expected
    assert record.latency_ms == (None if expected == Status.OFFLINE else latency)


def test_any_target_per_layer_is_enough():
    probes = results()
    probes.append(ProbeResult(Kind.TCP, "other", False, None, "timeout"))
    record = classify(probes, 500)
    assert record.status == Status.CONNECTED
    assert record.failure_percent == 25


@pytest.mark.parametrize(
    "field,value",
    [
        ("tcp_targets", []),
        ("dns_targets", ["bad/path"]),
        ("tcp_targets", ["host:0"]),
        ("tcp_targets", ["host:65536"]),
        ("http_targets", ["ftp://example.com"]),
        ("http_targets", ["https://user:password@example.com"]),
        ("dns_targets", ["example.com", "example.com"]),
        ("interval_seconds", 0),
        ("timeout_seconds", float("nan")),
        ("retention_days", 10),
    ],
)
def test_invalid_settings(field, value):
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


def test_ipv6_tcp_address():
    assert tcp_address("[::1]:443") == ("::1", 443)


def test_screenshot_defaults():
    settings = Settings()
    assert settings.tcp_targets == ["223.5.5.5:80", "119.29.29.29:80"]
    assert settings.http_targets == [
        "https://www.aliyun.com/robots.txt",
        "https://www.qq.com/robots.txt",
    ]
    assert settings.dns_targets == ["www.aliyun.com", "www.tencent.com"]
