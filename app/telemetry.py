import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


# One JSON document per line, so `docker compose logs app | grep ...` works.
def _one_line(item):
    return item.to_json(indent=None) + os.linesep


def _exporters():
    # Send to a Collector when OTEL_EXPORTER_OTLP_ENDPOINT is set (the exporters read it themselves);
    # otherwise print to stdout.
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()
    return (
        ConsoleSpanExporter(formatter=_one_line),
        ConsoleMetricExporter(formatter=_one_line),
        ConsoleLogRecordExporter(formatter=_one_line),
    )


def setup_telemetry(service_name="order-tracker"):
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", service_name)})
    span_exporter, metric_exporter, log_exporter = _exporters()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Export interval comes from OTEL_METRIC_EXPORT_INTERVAL (milliseconds, default 60000).
    reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(LoggingHandler(logger_provider=logger_provider))

    return tracer_provider, meter_provider, logger_provider


def flush_telemetry(providers):
    for provider in providers:
        provider.force_flush()
