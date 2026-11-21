from backend.app.adapters import AWSAdapter, SyntheticAWSAdapter


def test_synthetic_adapter_contains_peak_hidden_by_average():
    adapter = SyntheticAWSAdapter()
    resource = adapter.list_resources()[0]
    assert resource.average_cpu_pct < 25
    assert resource.monthly_peak_cpu_pct > 80
    assert all(point["peak_day"] == 28 for point in adapter.workload_history(resource.id))


def test_real_adapter_fails_closed():
    try:
        AWSAdapter().list_resources()
    except RuntimeError as exc:
        assert "not enabled" in str(exc)
    else:
        raise AssertionError("Credentialed adapter must not operate in local mode")
