"""Counters, gauges and timers in the Prometheus text format, with no extra dependency.

The numbers of one process: how many comments were handled and what was done with them, how long the AI answers,
how long its waiting line is, how often Telegram asked to slow down, how many payments came. `render()` makes the text
for the `/metrics` page; `snapshot()` gives plain numbers for the log.
"""

import time
from collections.abc import Callable, Iterable

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


def _labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    pairs = ",".join(f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True))
    return "{" + pairs + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Metric:
    kind = ""

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()):
        self.name, self.help, self.label_names = name, help_text, labels

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        if set(labels) != set(self.label_names):
            raise ValueError(f"{self.name} takes the labels {self.label_names}, got {tuple(labels)}")
        return tuple(str(labels[n]) for n in self.label_names)

    def header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Metric):
    kind = "counter"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1, **labels: str) -> None:
        key = self._key(labels)
        self.values[key] = self.values.get(key, 0) + amount

    def lines(self) -> list[str]:
        return [f"{self.name}{_labels(self.label_names, k)} {v:g}" for k, v in sorted(self.values.items())]


class Gauge(_Metric):
    kind = "gauge"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        self.values[self._key(labels)] = value

    def lines(self) -> list[str]:
        return [f"{self.name}{_labels(self.label_names, k)} {v:g}" for k, v in sorted(self.values.items())]


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name, help_text, labels=(), buckets: Iterable[float] = DEFAULT_BUCKETS):
        super().__init__(name, help_text, labels)
        self.buckets = tuple(sorted(buckets))
        self.series: dict[tuple[str, ...], list[float]] = {}  # per label set: bucket counts, then sum and count

    def observe(self, value: float, **labels: str) -> None:
        row = self.series.setdefault(self._key(labels), [0.0] * (len(self.buckets) + 2))
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                row[i] += 1
        row[-2] += value
        row[-1] += 1

    def lines(self) -> list[str]:
        out = []
        for key, row in sorted(self.series.items()):
            names = (*self.label_names, "le")
            for i, bound in enumerate(self.buckets):
                out.append(f"{self.name}_bucket{_labels(names, (*key, f'{bound:g}'))} {row[i]:g}")
            out.append(f"{self.name}_bucket{_labels(names, (*key, '+Inf'))} {row[-1]:g}")
            out.append(f"{self.name}_sum{_labels(self.label_names, key)} {row[-2]:g}")
            out.append(f"{self.name}_count{_labels(self.label_names, key)} {row[-1]:g}")
        return out


class Timer:
    """`with timer(histogram, label=...)`: observes how long the block took."""

    def __init__(self, histogram: Histogram, **labels: str):
        self.histogram, self.labels = histogram, labels

    def __enter__(self) -> "Timer":
        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.histogram.observe(time.perf_counter() - self.started, **self.labels)


class Registry:
    def __init__(self) -> None:
        self.metrics: list[_Metric] = []
        self.started = time.time()
        self._collectors: list[Callable[[], None]] = []

    def _add(self, metric):
        self.metrics.append(metric)
        return metric

    def counter(self, name, help_text, labels=()) -> Counter:
        return self._add(Counter(name, help_text, tuple(labels)))

    def gauge(self, name, help_text, labels=()) -> Gauge:
        return self._add(Gauge(name, help_text, tuple(labels)))

    def histogram(self, name, help_text, labels=(), buckets=DEFAULT_BUCKETS) -> Histogram:
        return self._add(Histogram(name, help_text, tuple(labels), buckets))

    def on_scrape(self, fn: Callable[[], None]) -> None:
        """Register a function that refreshes gauges right before the page is built (queue length and the like)."""
        self._collectors.append(fn)

    def render(self) -> str:
        for fn in self._collectors:
            fn()
        lines: list[str] = []
        uptime = self.gauge_value("defenceai_uptime_seconds", "Seconds since the process started", time.time() - self.started)
        for metric in [*self.metrics]:
            body = metric.lines()  # type: ignore[attr-defined]
            if body:
                lines += metric.header() + body
        return "\n".join(lines + uptime) + "\n"

    @staticmethod
    def gauge_value(name: str, help_text: str, value: float) -> list[str]:
        return [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {value:g}"]

    def snapshot(self) -> dict[str, float]:
        """Totals of the counters, for a line in the log."""
        out: dict[str, float] = {}
        for metric in self.metrics:
            if isinstance(metric, Counter):
                for key, value in metric.values.items():
                    suffix = ",".join(key)
                    out[f"{metric.name.removeprefix('defenceai_')}{{{suffix}}}" if suffix else metric.name.removeprefix("defenceai_")] = value
        return out


METRICS = Registry()
COMMENTS = METRICS.counter("defenceai_comments_total", "Comments handled, by what was decided", ("action",))
COMMENT_SECONDS = METRICS.histogram("defenceai_comment_seconds", "Time to handle one comment, from arrival to action")
JEV_REQUESTS = METRICS.counter("defenceai_jev_requests_total", "Requests to the AI service, by outcome", ("outcome",))
JEV_SECONDS = METRICS.histogram("defenceai_jev_seconds", "Time of one AI request without the waiting line")
JEV_WAIT_SECONDS = METRICS.histogram("defenceai_jev_wait_seconds", "Time a request waited for a free AI slot")
JEV_QUEUE = METRICS.gauge("defenceai_jev_queue", "Requests waiting for a free AI slot")
JEV_RUNNING = METRICS.gauge("defenceai_jev_running", "Requests being answered by the AI right now")
TELEGRAM_ERRORS = METRICS.counter("defenceai_telegram_errors_total", "Telegram refused an action", ("action",))
FLOOD_WAITS = METRICS.counter("defenceai_telegram_flood_waits_total", "Times Telegram asked to slow down")
PAYMENTS = METRICS.counter("defenceai_payments_total", "Payments received", ("currency",))
PAYMENT_AMOUNT = METRICS.counter("defenceai_payment_amount_total", "Money received (roubles or Stars)", ("currency",))
CHATS = METRICS.gauge("defenceai_chats", "Chats by access state", ("state",))
PENDING_REVIEWS = METRICS.gauge("defenceai_pending_reviews", "Comments waiting for an owner's decision, all chats")
DB_BYTES = METRICS.gauge("defenceai_db_bytes", "Size of the database file")
