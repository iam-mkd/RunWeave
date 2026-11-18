from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOOL_INPUT_LIMIT = 16384


class RequestBodyLimit:
    """Bound request bodies before JSON/Pydantic allocation, including chunked requests.

    Covers isolated-tool calls and run submissions, whose agent input is arbitrary JSON.
    """

    def __init__(self, app: ASGIApp, *, runs_max_bytes: int) -> None:
        self.app = app
        self._runs_max_bytes = runs_max_bytes

    def _limit(self, scope: Scope) -> tuple[int, dict[str, object]] | None:
        path = scope.get("path", "")
        if path.startswith("/v1/tools/"):
            return TOOL_INPUT_LIMIT, {"code": "TOOL_INPUT_LIMIT"}
        if scope.get("method") == "POST" and path.rstrip("/") == "/v1/runs":
            return self._runs_max_bytes, {
                "code": "INPUT_TOO_LARGE",
                "message": f"Run requests are limited to {self._runs_max_bytes} bytes",
            }
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self._limit(scope) if scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return
        maximum, detail = limit
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > maximum:
                await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        used = False

        async def replay() -> Message:
            nonlocal used
            if not used:
                used = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
