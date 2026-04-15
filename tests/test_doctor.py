from hermes_link.runtime import bootstrap_runtime, load_config, save_config, set_runtime_home
from hermes_link.service import collect_doctor_report


def test_doctor_warns_when_service_and_hermes_are_missing(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()
    report = collect_doctor_report()

    assert report.summary in {"warning", "error"}
    assert any(check.code == "service_not_running" for check in report.checks)


def test_doctor_reports_degraded_relay_when_configured(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    config = load_config()
    config.network.relay_url = "https://relay.example.com"
    config.relay.connection_status = "degraded"
    config.relay.last_error = "upstream timeout"
    save_config(config)

    report = collect_doctor_report()

    assert any(check.code == "relay_degraded" for check in report.checks)
