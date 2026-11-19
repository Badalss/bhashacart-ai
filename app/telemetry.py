from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter, time

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import settings

METRIC = "bhashacart.hop.duration"
INGRESS_TIME: ContextVar[float | None] = ContextVar("ingress_time", default=None)


def configure(service: str) -> None:
    endpoint = settings().otel_exporter_otlp_endpoint.rstrip("/")
    resource = Resource.create({"service.name": service})
    traces = TracerProvider(resource=resource)
    readers = []
    if endpoint:
        traces.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint + "/v1/traces")))
        readers.append(PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint + "/v1/metrics"), export_interval_millis=10000
        ))
    trace.set_tracer_provider(traces)
    metrics.set_meter_provider(MeterProvider(
        resource=resource,
        metric_readers=readers,
        views=[View(instrument_name=METRIC, aggregation=ExplicitBucketHistogramAggregation(
            boundaries=[5, 10, 20, 30, 50, 80, 120, 160, 220, 280, 400, 600, 800, 1200, 2500, 5000]
        ))],
    ))


def observe(hop: str, elapsed_ms: float, outcome: str = "ok", **dimensions: str) -> None:
    histogram = metrics.get_meter(__name__).create_histogram(METRIC, unit="ms")
    histogram.record(max(0, elapsed_ms), {"hop": hop, "outcome": outcome, **dimensions})


@contextmanager
def response_clock(ingress_time: float) -> Iterator[None]:
    token = INGRESS_TIME.set(ingress_time)
    try:
        yield
    finally:
        INGRESS_TIME.reset(token)


def response_ready(kind: str) -> None:
    start = INGRESS_TIME.get()
    if start is not None:
        observe("live_ingress_to_response_ready", (time() - start) * 1000, response_kind=kind)
        INGRESS_TIME.set(None)


@contextmanager
def hop(name: str) -> Iterator[None]:
    start = perf_counter()
    outcome = "ok"
    with trace.get_tracer(__name__).start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield
        except BaseException:
            outcome = "error"
            span.set_status(trace.StatusCode.ERROR)
            raise
        finally:
            observe(name, (perf_counter() - start) * 1000, outcome)
