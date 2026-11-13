import asyncio

import httpx

from runweave.tools.fake import fake_health_response


class MockHealthService:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.attempts: dict[str, int] = {}
        self.cancel_target_started = asyncio.Event()

    async def respond(self, request: httpx.Request) -> httpx.Response:
        service = request.url.path.removeprefix("/health/")
        if service == "slow" or service == "cancelled":
            if service == "cancelled":
                self.cancel_target_started.set()
            await asyncio.sleep(1)
        elif service in {"rate-limited", "transient", "disconnect"}:
            self.attempts[service] = self.attempts.get(service, 0) + 1
            if self.attempts[service] == 1:
                if service == "disconnect":
                    raise httpx.ReadError("Synthetic disconnect", request=request)
                code = 429 if service == "rate-limited" else 503
                return httpx.Response(code, headers={"Retry-After": "0"})
        elif service == "forbidden":
            return httpx.Response(403)
        elif service == "malformed":
            return httpx.Response(200, json={"invalid": True})

        await asyncio.sleep(self.delay)
        return await fake_health_response(request)
