# Internet Monitor

A Chinese desktop application built with [Flet](https://flet.dev/) and managed with
[uv](https://docs.astral.sh/uv/). Its monitoring workflow is inspired by
[FutureSolutionDev/internet-monitor](https://github.com/FutureSolutionDev/internet-monitor).
The implementation is independent; the interface, messages, settings and exported
records are in Simplified Chinese.

## Run

Install uv, then run these commands from the project directory:

```powershell
uv sync --locked
uv run internet-monitor
```

Python 3.12 or later is required. The `.python-version` file selects the development
interpreter; uv can install that interpreter when needed. Flet's desktop client is
included in the default development dependency group.

For a local browser preview:

```powershell
uv run internet-monitor --web --port 8550
```

Open `http://127.0.0.1:8550` in a browser. The listener binds to loopback only.
Only one monitoring session per data directory can run at a time. Close an existing
desktop window or browser session before opening another one.

Run a diagnostic without opening a window or writing monitoring history:

```powershell
uv run internet-monitor --check
```

## Features

- Concurrent TCP connection, HTTP request and DNS resolution probes.
- Live connection status, continuous online duration, availability, outages,
  average latency, jitter and a chart of the last 60 observations.
- Per-target results and Chinese failure reasons.
- Editable targets, individual tests, tests of all targets and default restoration.
- Configurable interval, timeout, latency threshold and history retention.
- Atomic settings persistence, SQLite history, state transition events and restart recovery.
- Recent checks with target details, status filtering, summary copying and CSV export.
- Clear all history with confirmation; settings remain saved and monitoring continues.
- Start, pause and manual checks; automatic monitoring when the app opens.
- Single-instance protection for each data directory.

Minimizing the desktop window keeps monitoring active. Closing the window ends the
session. Browser mode stops monitoring when its client disconnects; reload the
page to start a new session. The app does not provide a system tray, OS login
startup registration, bandwidth speed tests or external notifications.

## Default targets

The default monitoring targets are:

| Check | Targets |
| --- | --- |
| TCP | `223.5.5.5:80`, `119.29.29.29:80` |
| HTTP | `https://www.aliyun.com/robots.txt`, `https://www.qq.com/robots.txt` |
| DNS | `www.aliyun.com`, `www.tencent.com` |

Each family requires 1–20 targets. TCP targets use `host:port`, including
`[IPv6]:port`. HTTP targets require an HTTP or HTTPS URL without embedded
credentials. DNS targets accept domain names.

In the **监测设置** page, edit a target or use **添加目标**, then **测试** or
**测试全部目标** to inspect connectivity. Use **保存并应用** to persist and apply
the settings. Tests use the draft target and timeout, and do not affect history.
**恢复默认设置** fills the form; saving applies it. An unreachable but syntactically
valid target can be saved so outages can be monitored.

## Measurement semantics

Each configured target is probed in parallel. A family passes when at least one of
its targets passes; individual failures remain visible. All three families passing
with representative latency below the threshold produces **连接正常**. Partial
family failure or high latency produces **网络波动**. All families failing produces
**网络中断**.

- TCP measures connection establishment and closes the socket immediately.
- HTTP measures time through response headers, follows redirects, validates TLS
  certificates and accepts status 200 or 204. Response bodies are not downloaded.
- DNS queries IPv4 address records using the system-configured DNS servers without
  an application resolver cache. The OS or recursive DNS server may still cache answers.
- Representative latency is the minimum successful TCP latency, falling back to
  HTTP and then DNS when necessary. It is not ICMP round-trip time.
- Availability is the fraction of recorded rounds that are not fully offline.
  Degraded rounds count as available; pauses and periods when the app is closed
  are excluded. Today uses the machine's local calendar day; 7/30-day windows roll.
- Jitter is the mean absolute difference between adjacent successful TCP round
  latencies among the last 60 observations.
- Probe failure rate is failed target probes divided by all target probes. It is
  **not ICMP packet loss**.
- The interval is the wait after a complete round, so actual sampling spacing
  includes the round's duration. A pause cancels an unfinished round.
- Events record status changes. Pauses terminate events, and a recovered unfinished
  event ends at its last observation rather than counting time when the app was closed.

HTTP checks ignore environment proxy settings so they measure the direct connection
consistently with TCP and DNS. OS-level VPNs, transparent proxies and DNS settings
still affect the observed path.

## Local data

Settings and history are stored in the platform's user data directory. On Windows,
this is usually `%LOCALAPPDATA%\internet-monitor`:

| File | Purpose |
| --- | --- |
| `settings.json` | Validated monitoring configuration |
| `history.sqlite3` | Observations, per-target details and events |
| `monitor.lock` | Prevents concurrent writers |

The history retention default is 90 days and can be set to 30–365 days. Statistics
and exports cover retained observations. CSV files use UTF-8 with a BOM for Excel
compatibility and include local timestamps and per-target details.

Use `INTERNET_MONITOR_DATA_DIR` to select a separate data directory:

```powershell
$env:INTERNET_MONITOR_DATA_DIR = "$PWD\test-data"
uv run internet-monitor
```

No account, telemetry or cloud storage is used. Probes contact the configured targets.
In browser mode data is stored on the computer running the Python process.

## Development

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

Tests use local TCP servers, mocked HTTP responses and DNS resolvers. They cover
status classification, target validation, concurrency, cancellation, persistence,
retention, availability windows, event recovery and CSV contents.

The package is organized into validated models, asynchronous probes, the monitoring
lifecycle, SQLite storage, SVG chart generation and the Flet interface. Clients,
resolvers and the repository are created at the application boundary and injected
into the monitor. `main.py` is also available as a Flet CLI entry point.
