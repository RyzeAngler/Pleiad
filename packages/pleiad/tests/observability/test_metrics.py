from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from pleiad.observability.metrics import ObservabilityMetrics


def test_metrics_record_runtime_facts_without_trace_identity_labels() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = ObservabilityMetrics(provider.get_meter("pleiad"))

    metrics.record_run("succeeded")
    metrics.record_model("fake", "succeeded", 0.1, input_tokens=2, output_tokens=3)
    metrics.record_tool("failed", 0.2, failed=True)

    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    scope_metrics = metrics_data.resource_metrics[0].scope_metrics[0]
    observed = {metric.name: metric for metric in scope_metrics.metrics}
    run_point = observed["pleiad.run.count"].data.data_points[0]
    model_point = observed["pleiad.model.call.count"].data.data_points[0]
    tool_point = observed["pleiad.tool.failure.count"].data.data_points[0]
    assert isinstance(run_point, NumberDataPoint)
    assert isinstance(model_point, NumberDataPoint)
    assert isinstance(tool_point, NumberDataPoint)
    assert run_point.value == 1
    assert model_point.attributes == {
        "provider": "fake",
        "status": "succeeded",
    }
    assert tool_point.attributes == {"status": "failed"}
    for point in (run_point, model_point, tool_point):
        assert point.attributes is not None
        assert "run_id" not in point.attributes
        assert "trace_id" not in point.attributes
