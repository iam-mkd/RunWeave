import asyncio
import json
import logging
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener
from queue import Full, Queue
from time import monotonic
from typing import TYPE_CHECKING, cast
from uuid import UUID

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import StatusCode

if TYPE_CHECKING:
    from runweave.orchestration.models import RunSnapshot

BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 30, 60)


class EventQueueHandler(QueueHandler):
    dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except Full:
            self.dropped += 1


class EventQueueListener(QueueListener):
    def enqueue_sentinel(self) -> None:
        cast(Queue[logging.LogRecord | None], self.queue).put(None)


class Histogram:
    def __init__(self) -> None:
        self.counts = [0] * len(BUCKETS)
        self.count = 0
        self.total = 0.0

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        for index, bound in enumerate(BUCKETS):
            if value <= bound:
                self.counts[index] += 1

    def render(self, name: str) -> Iterator[str]:
        yield f"# TYPE {name} histogram"
        for bound, count in zip(BUCKETS, self.counts, strict=True):
            yield f'{name}_bucket{{le="{bound}"}} {count}'
        yield f'{name}_bucket{{le="+Inf"}} {self.count}'
        yield f"{name}_sum {self.total}"
        yield f"{name}_count {self.count}"


class Telemetry:
    def __init__(self, *, json_logs: bool = False, console_traces: bool = False) -> None:
        self.admitted = 0
        self.rejected: Counter[str] = Counter()
        self.outcomes: Counter[str] = Counter()
        self.failure_kinds: Counter[str] = Counter()
        self.retry_attempts = 0
        self.queue_wait = Histogram()
        self.run_duration = Histogram()
        self.model_duration = Histogram()
        self.tool_duration = Histogram()
        self.loop_lag = Histogram()
        self.tool_in_flight = 0
        self.peak_tools = 0
        self.peak_runs = 0
        self._logger: logging.Logger | None = None
        self._log_handler: EventQueueHandler | None = None
        self._listener: EventQueueListener | None = None
        if json_logs:
            events: Queue[logging.LogRecord | None] = Queue(maxsize=1024)
            self._logger = logging.Logger("runweave.events", level=logging.INFO)
            self._log_handler = EventQueueHandler(events)
            self._logger.addHandler(self._log_handler)
            self._listener = EventQueueListener(events, logging.StreamHandler())
            self._listener.start()
        self._provider = (
            TracerProvider(resource=Resource({"service.name": "runweave"}), shutdown_on_exit=False)
            if console_traces
            else None
        )
        self._tracer = self._provider.get_tracer("runweave") if self._provider else None
        if self._provider:
            self._provider.add_span_processor(
                BatchSpanProcessor(ConsoleSpanExporter(out=sys.stdout), max_queue_size=2048)
            )

    async def close(self) -> None:
        if self._provider:
            await asyncio.to_thread(self._provider.shutdown)
        if self._listener:
            await asyncio.to_thread(self._listener.stop)

    async def sample_loop_lag(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            target = loop.time() + 0.02
            await asyncio.sleep(0.02)
            self.loop_lag.observe(max(0.0, loop.time() - target))

    @contextmanager
    def span(self, operation: str, *, run_id: UUID | None = None) -> Iterator[None]:
        if self._tracer is None:
            yield
            return
        with self._tracer.start_as_current_span(
            operation,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            if run_id is not None:
                span.set_attribute("runweave.run.id", str(run_id))
            try:
                yield
            except asyncio.CancelledError:
                span.set_attribute("runweave.cancelled", True)
                raise
            except Exception:
                span.set_status(StatusCode.ERROR)
                raise

    @contextmanager
    def step(self, *, tool: bool) -> Iterator[None]:
        start = monotonic()
        if tool:
            self.tool_in_flight += 1
            self.peak_tools = max(self.peak_tools, self.tool_in_flight)
        try:
            with self.span("agent.tool" if tool else "agent.model"):
                yield
        finally:
            (self.tool_duration if tool else self.model_duration).observe(monotonic() - start)
            if tool:
                self.tool_in_flight -= 1

    def finished(self, snapshot: "RunSnapshot", duration: float) -> None:
        self.outcomes[snapshot.status.value] += 1
        self.run_duration.observe(duration)
        if self._logger:
            self._logger.info(
                json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "event": "run.finished",
                        "run_id": str(snapshot.id),
                        "status": snapshot.status.value,
                        "duration_seconds": duration,
                    }
                )
            )

    def render(self, queue_depth: int, active_runs: int, retained_runs: int) -> str:
        scalars = {
            "dropped_log_events_total": (
                "counter",
                self._log_handler.dropped if self._log_handler else 0,
            ),
            "admitted_total": ("counter", self.admitted),
            "tool_retries_total": ("counter", self.retry_attempts),
            "queue_depth": ("gauge", queue_depth),
            "active_runs": ("gauge", active_runs),
            "retained_runs": ("gauge", retained_runs),
            "peak_active_runs": ("gauge", self.peak_runs),
            "active_tool_steps": ("gauge", self.tool_in_flight),
            "peak_active_tool_steps": ("gauge", self.peak_tools),
        }
        lines: list[str] = []
        for name, (kind, value) in scalars.items():
            lines.extend((f"# TYPE runweave_{name} {kind}", f"runweave_{name} {value}"))
        for name, label, counts in (
            ("rejected_total", "reason", self.rejected),
            ("runs_finished_total", "status", self.outcomes),
            ("run_errors_total", "category", self.failure_kinds),
        ):
            lines.append(f"# TYPE runweave_{name} counter")
            for key, count in sorted(counts.items()):
                lines.append(f'runweave_{name}{{{label}="{key}"}} {count}')
        for name, histogram in (
            ("queue_wait", self.queue_wait),
            ("run_duration", self.run_duration),
            ("model_duration", self.model_duration),
            ("tool_duration", self.tool_duration),
            ("event_loop_lag", self.loop_lag),
        ):
            lines.extend(histogram.render(f"runweave_{name}_seconds"))
        return "\n".join(lines) + "\n"
