"""Bounded, payload-free logs and Prometheus-compatible process metrics."""

from __future__ import annotations

import json
import logging
import math
import threading
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
from uuid import UUID

_MAX_FIELD_CHARACTERS: Final = 512
_DURATION_BUCKETS: Final = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


def _safe_value(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (str, UUID)):
        return str(value)[:_MAX_FIELD_CHARACTERS]
    # Never invoke an arbitrary object's string representation: callers can make
    # mistakes, and containers commonly hold request arguments or tool results.
    return f"<{type(value).__name__}>"


class JsonFormatter(logging.Formatter):
    """Serialize only explicitly supplied telemetry fields."""

    def format(self, record: logging.LogRecord) -> str:
        event = _safe_value(getattr(record, "event", "unstructured_log"))
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": event if isinstance(event, str) else "invalid_event",
        }
        fields = getattr(record, "telemetry", {})
        if isinstance(fields, Mapping):
            payload.update({str(key): _safe_value(value) for key, value in fields.items()})
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_json_logging(level: str) -> None:
    """Install one JSON handler without capturing request or response bodies."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Emit a structured event with an explicit, bounded field set."""

    logger.log(level, event, extra={"event": event, "telemetry": fields})


class MetricsRegistry:
    """Small process-local registry with bounded labels and OpenMetrics rendering."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(
            float
        )
        self._gauges: defaultdict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(
            float
        )
        self._durations: defaultdict[tuple[tuple[str, str], ...], list[float]] = defaultdict(
            lambda: [0.0] * (len(_DURATION_BUCKETS) + 2)
        )

    @staticmethod
    def _labels(labels: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((key, str(value)[:64]) for key, value in labels.items()))

    def increment(self, name: str, *, amount: float = 1, **labels: object) -> None:
        with self._lock:
            self._counters[(name, self._labels(labels))] += amount

    def gauge(self, name: str, value: float, **labels: object) -> None:
        with self._lock:
            self._gauges[(name, self._labels(labels))] = value

    def observe_http(self, duration_seconds: float, **labels: object) -> None:
        with self._lock:
            histogram = self._durations[self._labels(labels)]
            for index, bucket in enumerate(_DURATION_BUCKETS):
                if duration_seconds <= bucket:
                    histogram[index] += 1
            histogram[-2] += duration_seconds
            histogram[-1] += 1

    def initialize_http(self, **labels: object) -> None:
        """Expose a zero-valued histogram before the first observed request."""

        with self._lock:
            _ = self._durations[self._labels(labels)]

    @staticmethod
    def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        encoded = ",".join(
            f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
            for key, value in labels
        )
        return "{" + encoded + "}"

    def render(self) -> str:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            durations = {labels: tuple(values) for labels, values in self._durations.items()}
        lines = ["# TYPE modall_build_info gauge", 'modall_build_info{version="0.1.0"} 1']
        emitted_types: set[str] = set()
        for (name, labels), value in sorted(counters.items()):
            if name not in emitted_types:
                lines.append(f"# TYPE {name} counter")
                emitted_types.add(name)
            lines.append(f"{name}{self._format_labels(labels)} {value:g}")
        for (name, labels), value in sorted(gauges.items()):
            if name not in emitted_types:
                lines.append(f"# TYPE {name} gauge")
                emitted_types.add(name)
            lines.append(f"{name}{self._format_labels(labels)} {value:g}")
        if durations:
            lines.append("# TYPE modall_http_request_duration_seconds histogram")
        for labels, histogram in sorted(durations.items()):
            for index, bucket in enumerate(_DURATION_BUCKETS):
                bucket_labels = (*labels, ("le", f"{bucket:g}"))
                lines.append(
                    "modall_http_request_duration_seconds_bucket"
                    f"{self._format_labels(tuple(sorted(bucket_labels)))} {histogram[index]:g}"
                )
            infinite_labels = (*labels, ("le", "+Inf"))
            lines.append(
                "modall_http_request_duration_seconds_bucket"
                f"{self._format_labels(tuple(sorted(infinite_labels)))} {histogram[-1]:g}"
            )
            lines.append(
                "modall_http_request_duration_seconds_sum"
                f"{self._format_labels(labels)} {histogram[-2]:g}"
            )
            lines.append(
                "modall_http_request_duration_seconds_count"
                f"{self._format_labels(labels)} {histogram[-1]:g}"
            )
        lines.append("# EOF")
        return "\n".join(lines) + "\n"


def start_metrics_server(metrics: MetricsRegistry, *, host: str, port: int) -> ThreadingHTTPServer:
    """Start the worker's internal-only metrics and liveness listener."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/metrics":
                body = metrics.render().encode()
                content_type = "application/openmetrics-text; version=1.0.0"
                response_status = 200
            elif self.path == "/health/live":
                body = b'{"status":"ok","service":"worker"}\n'
                content_type = "application/json"
                response_status = 200
            else:
                body = b"not found\n"
                content_type = "text/plain"
                response_status = 404
            self.send_response(response_status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, name="modall-metrics", daemon=True).start()
    return server
