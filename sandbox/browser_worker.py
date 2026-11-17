import asyncio
import json
import signal
import sys
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


async def main(request: dict[str, Any]) -> None:
    resources = request["resources"]
    allowed = set(request["origins"])
    blocked = 0
    requests = 0
    visited: list[str] = []

    async def route_request(route: Any) -> None:
        nonlocal blocked, requests
        requests += 1
        url = route.request.url
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        resource = resources.get(url)
        if (
            requests > 32
            or origin not in allowed
            or route.request.method != "GET"
            or resource is None
            or parsed.username
            or parsed.password
        ):
            blocked += 1
            await route.abort("blockedbyclient")
            return
        # Redirects cannot bypass the broker, including same-origin redirect chains.
        if resource["status"] in range(300, 400):
            blocked += 1
            await route.abort("blockedbyclient")
            return
        visited.append(url)
        await route.fulfill(
            status=resource["status"], body=resource["body"], content_type=resource["content_type"]
        )

    async def websocket(socket: Any) -> None:
        nonlocal blocked
        blocked += 1
        await socket.close(code=1008)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            chromium_sandbox=True,
            args=[
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-features=WebRtcHideLocalIpsWithMdns",
            ],
        )
        try:
            context = await browser.new_context(service_workers="block", accept_downloads=False)
            await context.route("**/*", route_request)
            await context.route_web_socket("**/*", websocket)
            page = await context.new_page()
            page.set_default_timeout(3000)
            await page.goto(request["url"], wait_until="load", timeout=5000)
            await page.wait_for_function("window.__rw_ready__ === true", timeout=2000)
            text = (await page.locator("body").inner_text()).encode()[:4096].decode(errors="ignore")
            print(
                json.dumps({"ok": True, "text": text, "blocked": blocked, "visited": visited[:32]}),
                flush=True,
            )
            await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        signal.alarm(20)
        raw = sys.stdin.buffer.read(131073)
        if len(raw) > 131072:
            raise ValueError("INPUT_LIMIT")
        asyncio.run(main(json.loads(raw)))
    except Exception:
        print(json.dumps({"ok": False, "code": "BROWSER_READ_REJECTED"}), flush=True)
