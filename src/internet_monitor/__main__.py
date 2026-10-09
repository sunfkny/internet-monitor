import argparse
import asyncio
import os

import dns.asyncresolver
import flet as ft
import httpx

from internet_monitor.probes import check_network
from internet_monitor.storage import data_directory, load_settings
from internet_monitor.ui import main as app_main


async def diagnostic() -> None:
    settings = load_settings(data_directory() / "settings.json")
    async with httpx.AsyncClient(follow_redirects=True, trust_env=False) as client:
        record = await check_network(settings, client, dns.asyncresolver.Resolver())
    print(f"网络状态：{record.status.value}；{record.reason}")
    for result in record.results:
        latency = f"{result.latency_ms:.0f} 毫秒" if result.latency_ms is not None else "—"
        print(f"{result.kind.value} {result.target}：{result.detail}；{latency}")


def main() -> None:
    parser = argparse.ArgumentParser(description="网络质量监测")
    parser.add_argument("--web", action="store_true", help="在浏览器中运行")
    parser.add_argument("--port", type=int, default=8550, help="浏览器模式端口")
    parser.add_argument("--check", action="store_true", help="执行一次检测并输出结果")
    args = parser.parse_args()
    if args.check:
        asyncio.run(diagnostic())
    else:
        if args.web:
            os.environ["FLET_FORCE_WEB_SERVER"] = "true"
            print(f"请在浏览器中打开：http://127.0.0.1:{args.port}")
        ft.run(
            app_main,
            view=ft.AppView.WEB_BROWSER if args.web else ft.AppView.FLET_APP,
            host="127.0.0.1",
            port=args.port if args.web else 0,
        )


if __name__ == "__main__":
    main()
