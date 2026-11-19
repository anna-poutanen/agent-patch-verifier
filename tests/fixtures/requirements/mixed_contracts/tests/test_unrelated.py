from telemetry import emit_metric


def test_metric_transport_shape():
    assert emit_metric("requests") == {"name": "requests", "sent": True}
