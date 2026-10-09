from internet_monitor.models import CheckRecord


def latency_ceiling(records: list[CheckRecord]) -> int:
    maximum = max((record.latency_ms or 0 for record in records), default=0)
    return max(100, int(maximum / 100 + 1) * 100)


def latency_svg(records: list[CheckRecord]) -> str:
    width, height = 1000, 176
    left, right, top, bottom = 0, 1000, 0, 176
    ceiling = latency_ceiling(records)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">',
        '<defs><linearGradient id="area" x1="0" y1="0" x2="0" y2="1">',
        '<stop offset="0" stop-color="#46DDB2" stop-opacity="0.18"/>',
        '<stop offset="1" stop-color="#46DDB2" stop-opacity="0.01"/>',
        "</linearGradient></defs>",
    ]
    for step in range(5):
        y = bottom - (bottom - top) * step / 4
        parts.append(f'<line x1="{left}" y1="{y}" x2="{right}" y2="{y}" stroke="#26344A"/>')
    segments: list[list[tuple[float, float]]] = [[]]
    for index, record in enumerate(records):
        x = left + (right - left) * index / max(1, len(records) - 1)
        if record.latency_ms is None:
            if segments[-1]:
                segments.append([])
            parts.append(
                f'<line x1="{x}" y1="{bottom - 5}" x2="{x}" y2="{bottom}" '
                + 'stroke="#FF7C8C" stroke-width="3"/>'
            )
            continue
        y = bottom - (bottom - top) * record.latency_ms / ceiling
        segments[-1].append((x, y))
    for points in segments:
        if not points:
            continue
        coordinates = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        parts.extend(
            [
                f'<polygon points="{points[0][0]},{bottom} {coordinates} '
                + f'{points[-1][0]},{bottom}" fill="url(#area)"/>',
                f'<polyline points="{coordinates}" fill="none" stroke="#46DDB2" '
                + 'stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>',
            ]
        )
        if len(points) == 1:
            x, y = points[0]
            parts.append(f'<circle cx="{x}" cy="{y}" r="3" fill="#46DDB2"/>')
    parts.append("</svg>")
    return "".join(parts)
