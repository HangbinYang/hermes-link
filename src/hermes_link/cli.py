from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import qrcode
import typer

from hermes_link import __version__
from hermes_link.autostart import disable_autostart, enable_autostart, get_autostart_status
from hermes_link.control_plane import (
    ControlPlaneError,
    clear_relay_credentials,
    local_link_config,
    parse_cli_config_value,
    public_link_config,
    public_relay_snapshot,
    update_link_config_value,
)
from hermes_link.hermes_adapter import HermesAdapter, HermesAdapterError
from hermes_link.i18n import t
from hermes_link.maintenance import get_installation_metadata, uninstall_installation, update_installation
from hermes_link.network import build_topology_snapshot, preferred_pairing_urls
from hermes_link.runtime import bootstrap_runtime, get_runtime_paths, load_config, set_runtime_home
from hermes_link.security import SecurityError, SecurityManager
from hermes_link.service import (
    collect_doctor_report,
    collect_status_snapshot,
    read_running_pid,
    run_foreground_service,
    start_background_service,
    stop_background_service,
)
from hermes_link.storage import LinkRepository

app = typer.Typer(
    add_completion=False,
    help=t("app.tagline"),
    no_args_is_help=False,
    rich_markup_mode=None,
)
autostart_app = typer.Typer(help=t("cli.help.autostart"))
devices_app = typer.Typer(help=t("cli.help.devices"))
pairings_app = typer.Typer(help=t("cli.help.pairings"))
audit_app = typer.Typer(help=t("cli.help.audit"))
relay_app = typer.Typer(help=t("cli.help.relay"))
config_app = typer.Typer(help=t("cli.help.config"))
hermes_config_app = typer.Typer(help=t("cli.help.hermes_config"))
env_app = typer.Typer(help=t("cli.help.env"))
providers_app = typer.Typer(help=t("cli.help.providers"))
sessions_app = typer.Typer(help=t("cli.help.sessions"))
logs_app = typer.Typer(help=t("cli.help.logs"))
analytics_app = typer.Typer(help=t("cli.help.analytics"))
cron_app = typer.Typer(help=t("cli.help.cron"))
skills_app = typer.Typer(help=t("cli.help.skills"))
toolsets_app = typer.Typer(help=t("cli.help.toolsets"))
profiles_app = typer.Typer(help=t("cli.help.profiles"))
backup_app = typer.Typer(help=t("cli.help.backup"))
app.add_typer(autostart_app, name="autostart")
app.add_typer(devices_app, name="devices")
app.add_typer(pairings_app, name="pairings")
app.add_typer(audit_app, name="audit")
app.add_typer(relay_app, name="relay")
app.add_typer(config_app, name="config")
app.add_typer(hermes_config_app, name="hermes-config")
app.add_typer(env_app, name="env")
app.add_typer(providers_app, name="providers")
app.add_typer(sessions_app, name="sessions")
app.add_typer(logs_app, name="logs")
app.add_typer(analytics_app, name="analytics")
app.add_typer(cron_app, name="cron")
app.add_typer(skills_app, name="skills")
app.add_typer(toolsets_app, name="toolsets")
app.add_typer(profiles_app, name="profiles")
app.add_typer(backup_app, name="backup")


def _json_dump(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False))


def _bool_word(value: bool, *, truthy_key: str = "common.enabled", falsy_key: str = "common.disabled") -> str:
    return t(truthy_key if value else falsy_key)


def _status_word(value: str) -> str:
    return t(f"status.{value}")


def _display_word(value: str | None) -> str:
    if not value:
        return "-"
    common_key = f"common.{value}"
    translated = t(common_key)
    return translated if translated != common_key else value


def _format_line(key: str, value: object) -> str:
    return f"  {t(key, value=value)}"


def _paths_and_repo() -> tuple[LinkRepository, Path]:
    paths, _ = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    return repository, paths.db_path


def _adapter() -> HermesAdapter:
    _, config = bootstrap_runtime()
    return HermesAdapter(config)


def _adapter_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except HermesAdapterError as exc:
        typer.echo(t("common.error_prefix", message=exc.message))
        raise typer.Exit(code=1) from exc


def _start_service_or_exit(config) -> int:
    try:
        return start_background_service(config)
    except RuntimeError as exc:
        message = t("cli.message.service_failed_to_start") if str(exc) == "service_failed_to_start" else str(exc)
        typer.echo(t("common.error_prefix", message=message))
        raise typer.Exit(code=1) from exc


def _print_help_summary() -> None:
    lines = [
        t("cli.summary.title"),
        "  hermes-link pair",
        "  hermes-link start",
        "  hermes-link status",
        "  hermes-link doctor",
        "  hermes-link install-service",
        "  hermes-link relay status",
        "  hermes-link pairings list",
        "  hermes-link audit list",
        "  hermes-link sessions list",
        "  hermes-link hermes-config show",
        "  hermes-link env list",
        "  hermes-link cron list",
        "  hermes-link backup create",
        "",
        t("cli.summary.full_help"),
    ]
    typer.echo("\n".join(lines))


def _render_status(snapshot: dict) -> None:
    service = snapshot["service"]
    topology = snapshot["topology"]
    hermes = snapshot["hermes"]
    relay = snapshot["relay"]
    lines = [
        t("cli.status.heading"),
        _format_line("cli.status.installed_version", snapshot["version"]),
        _format_line("cli.status.link_id", snapshot["link_id"]),
        _format_line("cli.status.display_name", snapshot["display_name"]),
        _format_line("cli.status.service", _bool_word(service["running"], truthy_key="common.online", falsy_key="common.offline")),
        _format_line("cli.status.pid", service["pid"] or "-"),
        _format_line("cli.status.health_url", service["health_url"]),
        _format_line("cli.status.log_file", service["log_path"]),
        _format_line("cli.status.autostart", _bool_word(service["autostart_enabled"])),
        _format_line("cli.status.paired_devices", snapshot["paired_devices"]),
        _format_line("cli.status.pending_pairings", snapshot["pending_pairing_sessions"]),
        _format_line("cli.status.hermes_detected", _bool_word(hermes["found"], truthy_key="common.yes", falsy_key="common.no")),
        _format_line("cli.status.hermes_cli", hermes["executable_path"] or "-"),
        _format_line("cli.status.hermes_home", hermes["hermes_home"] or "-"),
        _format_line("cli.status.lan_direct", _status_word(topology["lan_direct"]["status"])),
        _format_line("cli.status.public_direct", _status_word(topology["public_direct"]["status"])),
        _format_line("cli.status.relay", _status_word(topology["relay"]["status"])),
        _format_line("cli.status.relay_connection", _status_word(relay["connection_status"])),
    ]
    if topology["lan_direct"]["urls"]:
        lines.append(_format_line("cli.status.lan_urls", ", ".join(topology["lan_direct"]["urls"])))
    if topology["public_direct"]["urls"]:
        lines.append(_format_line("cli.status.public_url", ", ".join(topology["public_direct"]["urls"])))
    if topology["relay"]["urls"]:
        lines.append(_format_line("cli.status.relay_url", ", ".join(topology["relay"]["urls"])))
    if relay.get("access_token_expires_at"):
        lines.append(_format_line("cli.status.relay_access_expires_at", relay["access_token_expires_at"]))
    if relay.get("refresh_token_expires_at"):
        lines.append(_format_line("cli.status.relay_refresh_expires_at", relay["refresh_token_expires_at"]))
    if relay.get("last_error"):
        lines.append(_format_line("cli.status.relay_last_error", relay["last_error"]))
    typer.echo("\n".join(lines))


def _render_doctor(report: dict) -> None:
    typer.echo(t("cli.doctor.summary", value=t(f"common.{report['summary']}")))
    for check in report["checks"]:
        level_label = t(f"common.{check['level']}").upper()
        typer.echo(f"  [{level_label}] {check['message']}")
        if check.get("hint"):
            typer.echo(f"    {t('cli.doctor.hint', value=check['hint'])}")


def _print_pairing_qr(payload: dict) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    qr = qrcode.QRCode(border=1)
    qr.add_data(encoded)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def _restart_service_if_running(config) -> tuple[bool, int | None]:
    if not read_running_pid():
        return False, None
    stop_background_service()
    pid = _start_service_or_exit(config)
    return True, pid


def _apply_local_config_changes(
    changes: list[tuple[str, object]],
    *,
    restart_if_running: bool = True,
) -> tuple[object, dict]:
    config = load_config()
    outcome = {
        "changed": False,
        "changed_keys": [],
        "restart_required": False,
        "relay_sync_required": False,
        "clear_relay_credentials": False,
    }

    for key, value in changes:
        try:
            config, result = update_link_config_value(config, key, value)
        except ControlPlaneError as exc:
            typer.echo(t("common.error_prefix", message=exc.message))
            raise typer.Exit(code=1) from exc
        if result["changed"]:
            outcome["changed"] = True
            outcome["changed_keys"].append(key)
        outcome["restart_required"] = outcome["restart_required"] or result["restart_required"]
        outcome["relay_sync_required"] = outcome["relay_sync_required"] or result["relay_sync_required"]
        outcome["clear_relay_credentials"] = outcome["clear_relay_credentials"] or result["clear_relay_credentials"]

    restarted, pid = (False, None)
    if outcome["changed"] and restart_if_running and (outcome["restart_required"] or outcome["relay_sync_required"]):
        restarted, pid = _restart_service_if_running(config)

    outcome["service_restarted"] = restarted
    outcome["service_pid"] = pid
    outcome["runtime_reload_pending"] = bool(
        outcome["changed"]
        and read_running_pid()
        and not restarted
        and (outcome["restart_required"] or outcome["relay_sync_required"])
    )
    return config, outcome


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    home: Annotated[Path | None, typer.Option("--home", help=t("cli.option.home"))] = None,
) -> None:
    if home:
        set_runtime_home(home)
    if ctx.invoked_subcommand is None:
        _print_help_summary()


@app.command("help")
def help_command() -> None:
    _print_help_summary()


@app.command()
def version(
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_version"))] = False,
) -> None:
    metadata = get_installation_metadata()
    if json_output:
        _json_dump(metadata)
        return
    typer.echo(metadata["version"])


@app.command()
def init() -> None:
    paths, config = bootstrap_runtime()
    typer.echo(t("cli.message.runtime_initialized"))
    typer.echo(_format_line("cli.output.config", paths.config_path))
    typer.echo(_format_line("cli.output.database", paths.db_path))
    typer.echo(_format_line("cli.output.log_file", paths.log_path))
    typer.echo(_format_line("cli.output.link_id", config.link_id))


@app.command()
def install(
    start_service: Annotated[bool, typer.Option("--start/--no-start", help=t("cli.option.start_after_init"))] = True,
    autostart: Annotated[bool, typer.Option("--autostart/--no-autostart", help=t("cli.option.enable_autostart"))] = False,
) -> None:
    _, config = bootstrap_runtime()
    typer.echo(t("cli.message.install_completed"))
    typer.echo(_format_line("cli.output.runtime_home", get_runtime_paths().base_home))
    if autostart:
        ok, message = enable_autostart()
        if not ok:
            typer.echo(t("cli.message.autostart_enable_failed", message=message))
            raise typer.Exit(code=1)
        typer.echo(_format_line("cli.output.autostart", message))
    if start_service:
        pid = _start_service_or_exit(config)
        typer.echo(_format_line("cli.output.service_started_pid", pid))


@app.command("install-service")
def install_service() -> None:
    _, config = bootstrap_runtime()
    ok, message = enable_autostart()
    if not ok:
        typer.echo(t("cli.message.autostart_enable_failed", message=message))
        raise typer.Exit(code=1)
    pid = _start_service_or_exit(config)
    typer.echo(t("cli.message.install_service_completed", pid=pid))


@app.command("uninstall-service")
def uninstall_service() -> None:
    stop_background_service()
    ok, message = disable_autostart()
    if not ok:
        typer.echo(t("cli.message.autostart_disable_failed", message=message))
        raise typer.Exit(code=1)
    typer.echo(message)


@app.command()
def run(
    host: Annotated[str | None, typer.Option("--host", help=t("cli.option.host"))] = None,
    port: Annotated[int | None, typer.Option("--port", help=t("cli.option.port"))] = None,
) -> None:
    _, config = bootstrap_runtime()
    run_foreground_service(config, host=host, port=port)


@app.command()
def start() -> None:
    _, config = bootstrap_runtime()
    pid = _start_service_or_exit(config)
    typer.echo(t("cli.message.background_running", pid=pid))


@app.command()
def stop() -> None:
    stopped = stop_background_service()
    if stopped:
        typer.echo(t("cli.message.stopped"))
    else:
        typer.echo(t("cli.message.not_running"))


@app.command()
def restart() -> None:
    _, config = bootstrap_runtime()
    stop_background_service()
    pid = _start_service_or_exit(config)
    typer.echo(t("cli.message.restarted", pid=pid))


@app.command()
def update(
    spec: Annotated[str | None, typer.Option("--spec", help=t("cli.option.update_spec"))] = None,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_update"))] = False,
) -> None:
    result = update_installation(spec)
    if json_output:
        _json_dump(result)
        return
    if result["ok"]:
        typer.echo(t("cli.message.update_completed"))
        after = result.get("after") or {}
        typer.echo(_format_line("cli.output.version", after.get("version")))
        return
    typer.echo(t("cli.message.update_failed"))
    typer.echo(result.get("stderr") or result.get("stdout") or t("cli.message.unknown_pip_failure"))
    raise typer.Exit(code=1)


@app.command()
def uninstall(
    yes: Annotated[bool, typer.Option("--yes", help=t("cli.option.confirm_uninstall"))] = False,
    remove_data: Annotated[bool, typer.Option("--remove-data", help=t("cli.option.remove_data"))] = False,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_uninstall"))] = False,
) -> None:
    if not yes:
        typer.echo(t("cli.message.refuse_uninstall_without_yes"))
        raise typer.Exit(code=1)
    result = uninstall_installation(remove_data=remove_data)
    if json_output:
        _json_dump(result)
        return
    if result["ok"]:
        typer.echo(t("cli.message.uninstall_completed"))
        if result["removed_paths"]:
            typer.echo(_format_line("cli.output.removed_data", ", ".join(result["removed_paths"])))
        return
    typer.echo(t("cli.message.uninstall_failed"))
    typer.echo(result.get("stderr") or result.get("stdout") or t("cli.message.unknown_pip_failure"))
    raise typer.Exit(code=1)


@app.command()
def status(json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_status"))] = False) -> None:
    snapshot = collect_status_snapshot().model_dump(mode="json")
    if json_output:
        _json_dump(snapshot)
        return
    _render_status(snapshot)


@app.command()
def doctor(json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_doctor"))] = False) -> None:
    report = collect_doctor_report().model_dump(mode="json")
    if json_output:
        _json_dump(report)
        return
    _render_doctor(report)


@app.command()
def pair(
    scope: Annotated[list[str] | None, typer.Option("--scope", help=t("cli.option.scope"))] = None,
    note: Annotated[str | None, typer.Option("--note", help=t("cli.option.note"))] = None,
    no_start: Annotated[bool, typer.Option("--no-start", help=t("cli.option.no_start"))] = False,
) -> None:
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()

    if not no_start and not read_running_pid():
        _start_service_or_exit(config)

    security = SecurityManager(repository, config)
    try:
        session = security.create_pairing_session(scopes=scope or None, note=note)
    except SecurityError as exc:
        typer.echo(t("common.error_prefix", message=exc.message))
        raise typer.Exit(code=1) from exc

    topology = build_topology_snapshot(config)
    payload = {
        "kind": "hermes_link_pairing",
        "version": 1,
        "link_id": config.link_id,
        "display_name": config.display_name,
        "session_id": session.session_id,
        "code": session.code,
        "expires_at": session.expires_at,
        "preferred_urls": preferred_pairing_urls(topology),
    }
    typer.echo(t("cli.message.pairing_created"))
    typer.echo(t("cli.message.pairing_next_step"))
    typer.echo(_format_line("cli.output.session_id", session.session_id))
    typer.echo(_format_line("cli.output.pairing_code", session.code))
    typer.echo(_format_line("cli.output.expires_at", session.expires_at))
    if payload["preferred_urls"]:
        typer.echo(_format_line("cli.output.app_should_try", ", ".join(payload["preferred_urls"])))
    typer.echo("")
    _print_pairing_qr(payload)


@app.command()
def unpair(
    yes: Annotated[bool, typer.Option("--yes", help=t("cli.option.confirm_unpair"))] = False,
) -> None:
    if not yes:
        typer.echo(t("cli.message.refuse_unpair_without_yes"))
        raise typer.Exit(code=1)

    repository, _ = _paths_and_repo()
    revoked_devices = repository.revoke_all_devices()
    cancelled_pairings = repository.cancel_all_pending_pairings()
    repository.append_audit_event(
        "security.reset",
        actor_type="local_cli",
        detail={"revoked_devices": revoked_devices, "cancelled_pairings": cancelled_pairings},
    )
    typer.echo(t("cli.message.security_reset", revoked_devices=revoked_devices, cancelled_pairings=cancelled_pairings))


@autostart_app.command("on")
def autostart_on() -> None:
    ok, message = enable_autostart()
    if not ok:
        typer.echo(t("cli.message.autostart_enable_failed", message=message))
        raise typer.Exit(code=1)
    typer.echo(t("cli.message.autostart_enabled", message=message))


@autostart_app.command("off")
def autostart_off() -> None:
    ok, message = disable_autostart()
    if not ok:
        typer.echo(t("cli.message.autostart_disable_failed", message=message))
        raise typer.Exit(code=1)
    typer.echo(message)


@autostart_app.command("status")
def autostart_status() -> None:
    _json_dump(get_autostart_status())


@devices_app.command("list")
def devices_list(json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_devices"))] = False) -> None:
    repository, _ = _paths_and_repo()
    devices = [device.model_dump(mode="json") for device in repository.list_devices()]
    if json_output:
        _json_dump({"devices": devices})
        return
    if not devices:
        typer.echo(t("cli.message.no_devices"))
        return
    typer.echo(t("cli.message.paired_devices"))
    for device in devices:
        typer.echo(f"  {device['device_id']}  {device['label']}  {device['platform']}  {_display_word(device['status'])}")


@devices_app.command("revoke")
def devices_revoke(device_id: str) -> None:
    repository, _ = _paths_and_repo()
    revoked = repository.revoke_device(device_id)
    if not revoked:
        typer.echo(t("cli.message.device_not_active", device_id=device_id))
        raise typer.Exit(code=1)
    repository.append_audit_event(
        "device.revoked",
        actor_type="local_cli",
        detail={"revoked_device_id": device_id},
    )
    typer.echo(t("cli.message.device_revoked", device_id=device_id))


@pairings_app.command("list")
def pairings_list(
    include_non_pending: Annotated[bool, typer.Option("--all", help=t("cli.option.pairings_all"))] = False,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_pairings"))] = False,
) -> None:
    repository, _ = _paths_and_repo()
    sessions = [session.model_dump(mode="json") for session in repository.list_pairing_sessions(include_non_pending=include_non_pending)]
    if json_output:
        _json_dump({"sessions": sessions})
        return
    if not sessions:
        typer.echo(t("cli.message.no_pairing_sessions"))
        return
    for session in sessions:
        typer.echo(
            f"{session['session_id']}  {_display_word(session['status'])}  "
            f"{session['code']}  {session['expires_at']}"
        )


@pairings_app.command("cancel")
def pairings_cancel(session_id: str) -> None:
    repository, _ = _paths_and_repo()
    session = repository.cancel_pairing_session(session_id)
    if session is None:
        typer.echo(t("common.error_prefix", message=t("security.pairing_session_not_found")))
        raise typer.Exit(code=1)
    if session.status != "cancelled":
        typer.echo(t("common.error_prefix", message=t("security.pairing_session_not_pending")))
        raise typer.Exit(code=1)
    repository.append_audit_event(
        "pairing.session.cancelled",
        actor_type="local_cli",
        detail={"session_id": session_id},
    )
    typer.echo(t("cli.message.pairing_session_cancelled", session_id=session_id))


@audit_app.command("list")
def audit_list(
    limit: Annotated[int, typer.Option("--limit", help=t("cli.option.limit"))] = 20,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_audit"))] = False,
) -> None:
    repository, _ = _paths_and_repo()
    events = [event.model_dump(mode="json") for event in repository.list_audit_events(limit=max(1, limit))]
    if json_output:
        _json_dump({"events": events})
        return
    if not events:
        typer.echo(t("cli.message.no_audit_events"))
        return
    typer.echo(t("cli.message.audit_events"))
    for event in events:
        actor = event["actor_id"] or event["actor_type"]
        typer.echo(f"  #{event['event_id']}  {event['occurred_at']}  {event['event_type']}  {actor}")


@relay_app.command("status")
def relay_status(json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_relay"))] = False) -> None:
    snapshot = public_relay_snapshot(load_config())
    if json_output:
        _json_dump(snapshot)
        return
    typer.echo(t("cli.message.relay_status_heading"))
    typer.echo(_format_line("cli.status.relay", _bool_word(snapshot["enabled"] and snapshot["configured"], truthy_key="common.enabled", falsy_key="common.disabled")))
    typer.echo(_format_line("cli.status.relay_connection", _status_word(snapshot["connection_status"])))
    typer.echo(_format_line("cli.status.relay_url", snapshot["relay_base_url"] or "-"))
    typer.echo(_format_line("cli.status.relay_proxy_url", snapshot["proxy_base_url"] or "-"))
    if snapshot.get("access_token_expires_at"):
        typer.echo(_format_line("cli.status.relay_access_expires_at", snapshot["access_token_expires_at"]))
    if snapshot.get("refresh_token_expires_at"):
        typer.echo(_format_line("cli.status.relay_refresh_expires_at", snapshot["refresh_token_expires_at"]))
    if snapshot.get("last_error"):
        typer.echo(_format_line("cli.status.relay_last_error", snapshot["last_error"]))


@relay_app.command("enable")
def relay_enable(
    url: Annotated[str | None, typer.Option("--url", help=t("cli.option.relay_url"))] = None,
    restart_if_running: Annotated[bool, typer.Option("--restart-if-running/--no-restart-if-running", help=t("cli.option.restart_if_running"))] = True,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_relay"))] = False,
) -> None:
    changes: list[tuple[str, object]] = [("network.allow_relay", True)]
    if url is not None:
        changes.append(("network.relay_url", url))
    config, outcome = _apply_local_config_changes(changes, restart_if_running=restart_if_running)
    payload = outcome | {"relay": public_relay_snapshot(config)}
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.message.relay_enabled"))
    if outcome["service_restarted"]:
        typer.echo(_format_line("cli.output.service_started_pid", outcome["service_pid"]))
    elif outcome["runtime_reload_pending"]:
        typer.echo(t("cli.message.restart_required"))


@relay_app.command("disable")
def relay_disable(
    restart_if_running: Annotated[bool, typer.Option("--restart-if-running/--no-restart-if-running", help=t("cli.option.restart_if_running"))] = True,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_relay"))] = False,
) -> None:
    config, outcome = _apply_local_config_changes([("network.allow_relay", False)], restart_if_running=restart_if_running)
    payload = outcome | {"relay": public_relay_snapshot(config)}
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.message.relay_disabled"))
    if outcome["service_restarted"]:
        typer.echo(_format_line("cli.output.service_started_pid", outcome["service_pid"]))
    elif outcome["runtime_reload_pending"]:
        typer.echo(t("cli.message.restart_required"))


@relay_app.command("set-url")
def relay_set_url(
    url: str,
    restart_if_running: Annotated[bool, typer.Option("--restart-if-running/--no-restart-if-running", help=t("cli.option.restart_if_running"))] = True,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_relay"))] = False,
) -> None:
    config, outcome = _apply_local_config_changes([("network.relay_url", url)], restart_if_running=restart_if_running)
    payload = outcome | {"relay": public_relay_snapshot(config)}
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.message.relay_url_set", url=url))
    if outcome["service_restarted"]:
        typer.echo(_format_line("cli.output.service_started_pid", outcome["service_pid"]))
    elif outcome["runtime_reload_pending"]:
        typer.echo(t("cli.message.restart_required"))


@relay_app.command("clear-credentials")
def relay_clear_credentials_command(
    restart_if_running: Annotated[bool, typer.Option("--restart-if-running/--no-restart-if-running", help=t("cli.option.restart_if_running"))] = True,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_relay"))] = False,
) -> None:
    config = load_config()
    clear_relay_credentials(config)
    restarted = False
    pid = None
    if restart_if_running:
        restarted, pid = _restart_service_if_running(config)
    payload = {
        "ok": True,
        "service_restarted": restarted,
        "service_pid": pid,
        "relay": public_relay_snapshot(config),
    }
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.message.relay_credentials_cleared"))
    if restarted:
        typer.echo(_format_line("cli.output.service_started_pid", pid))
    elif read_running_pid():
        typer.echo(t("cli.message.restart_required"))


@relay_app.command("reconnect")
def relay_reconnect_command() -> None:
    _, config = bootstrap_runtime()
    if read_running_pid():
        stop_background_service()
    pid = _start_service_or_exit(config)
    typer.echo(t("cli.message.relay_reconnected", pid=pid))


@config_app.command("show")
def config_show(
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_config"))] = True,
    include_secrets: Annotated[bool, typer.Option("--include-secrets", help=t("cli.option.include_secrets"))] = False,
) -> None:
    config = load_config()
    payload = local_link_config(config, include_secrets=include_secrets)
    if json_output:
        _json_dump(payload)
        return
    _json_dump(payload)


@config_app.command("path")
def config_path() -> None:
    typer.echo(str(get_runtime_paths().config_path))


@config_app.command("set")
def config_set(
    key: str,
    value: str,
    restart_if_running: Annotated[bool, typer.Option("--restart-if-running/--no-restart-if-running", help=t("cli.option.restart_if_running"))] = True,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_config"))] = False,
) -> None:
    config, outcome = _apply_local_config_changes(
        [(key, parse_cli_config_value(value))],
        restart_if_running=restart_if_running,
    )
    payload = outcome | {"config": public_link_config(config)}
    if json_output:
        _json_dump(payload)
        return
    if outcome["changed"]:
        typer.echo(t("cli.message.link_config_updated", key=key))
    else:
        typer.echo(t("cli.message.link_config_unchanged", key=key))
    if outcome["service_restarted"]:
        typer.echo(_format_line("cli.output.service_started_pid", outcome["service_pid"]))
    elif outcome["runtime_reload_pending"]:
        typer.echo(t("cli.message.restart_required"))


@hermes_config_app.command("show")
def hermes_config_show(
    raw: Annotated[bool, typer.Option("--raw", help=t("cli.option.raw_yaml"))] = False,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_config"))] = True,
) -> None:
    adapter = _adapter()
    if raw:
        typer.echo(_adapter_call(adapter.get_config_raw))
        return
    payload = _adapter_call(adapter.get_config)
    if json_output:
        _json_dump(payload)
        return
    typer.echo(payload)


@hermes_config_app.command("set")
def hermes_config_set(key: str, value: str) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.set_config_value, key, value)
    typer.echo(t("cli.message.hermes_config_updated", key=key))
    _json_dump(payload)


@hermes_config_app.command("path")
def hermes_config_path() -> None:
    adapter = _adapter()
    typer.echo(str(adapter.config_path))


@env_app.command("list")
def env_list(json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_env"))] = False) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.list_env_vars)
    if json_output:
        _json_dump({"vars": payload})
        return
    if not payload:
        typer.echo(t("cli.message.no_env_vars"))
        return
    for row in payload:
        typer.echo(f"{row['key']}  {row['redacted_value'] or ''}")


@env_app.command("set")
def env_set(key: str, value: str) -> None:
    adapter = _adapter()
    _adapter_call(adapter.set_env_value, key, value)
    typer.echo(t("cli.message.env_var_set", key=key))


@env_app.command("unset")
def env_unset(key: str) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.delete_env_value, key)
    if payload["ok"]:
        typer.echo(t("cli.message.env_var_removed", key=key))
        return
    typer.echo(t("cli.message.env_var_missing", key=key))


@providers_app.command("list")
def providers_list(json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_env"))] = False) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.get_provider_auth_status)
    if json_output:
        _json_dump({"providers": payload})
        return
    for provider in payload:
        typer.echo(f"{provider['provider']}  {_display_word(provider['status'])}")


@sessions_app.command("list")
def sessions_list(
    limit: Annotated[int, typer.Option("--limit", help=t("cli.option.limit"))] = 20,
    offset: Annotated[int, typer.Option("--offset", help=t("cli.option.offset"))] = 0,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_sessions"))] = False,
) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.list_sessions, limit=limit, offset=offset)
    if json_output:
        _json_dump(payload)
        return
    for session in payload["sessions"]:
        typer.echo(
            f"{session['id']}  {session.get('source') or '-'}  "
            f"{session.get('model') or '-'}  {session.get('title') or '-'}"
        )


@sessions_app.command("search")
def sessions_search(
    query: str,
    limit: Annotated[int, typer.Option("--limit", help=t("cli.option.limit"))] = 20,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_search"))] = False,
) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.search_sessions, query, limit=limit)
    if json_output:
        _json_dump(payload)
        return
    for result in payload["results"]:
        typer.echo(f"{result['session_id']}  {result.get('snippet') or ''}")


@sessions_app.command("get")
def sessions_get(session_id: str, json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_session"))] = True) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.get_session, session_id)
    if json_output:
        _json_dump(payload)
        return
    typer.echo(payload)


@sessions_app.command("messages")
def sessions_messages(
    session_id: str,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_messages"))] = True,
) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.get_session_messages, session_id)
    if json_output:
        _json_dump(payload)
        return
    for message in payload["messages"]:
        typer.echo(f"[{message.get('role')}] {message.get('content') or ''}")


@sessions_app.command("delete")
def sessions_delete(
    session_id: str,
    yes: Annotated[bool, typer.Option("--yes", help=t("cli.option.confirm_delete_session"))] = False,
) -> None:
    if not yes:
        typer.echo(t("cli.message.refuse_delete_session_without_yes"))
        raise typer.Exit(code=1)
    adapter = _adapter()
    payload = _adapter_call(adapter.delete_session, session_id)
    if payload["ok"]:
        typer.echo(t("cli.message.session_deleted", session_id=session_id))
        return
    typer.echo(t("cli.message.session_not_found", session_id=session_id))
    raise typer.Exit(code=1)


@logs_app.command("show")
def logs_show(
    file: Annotated[str, typer.Option("--file", help=t("cli.option.log_file"))] = "agent",
    lines: Annotated[int, typer.Option("--lines", help=t("cli.option.log_lines"))] = 100,
    search: Annotated[str | None, typer.Option("--search", help=t("cli.option.log_search"))] = None,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_logs"))] = False,
) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.list_logs, file=file, lines=lines, search=search)
    if json_output:
        _json_dump(payload)
        return
    for line in payload["lines"]:
        typer.echo(line)


@analytics_app.command("usage")
def analytics_usage(
    days: Annotated[int, typer.Option("--days", help=t("cli.option.analytics_days"))] = 30,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_analytics"))] = False,
) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.get_usage_analytics, days=days)
    if json_output:
        _json_dump(payload)
        return
    totals = payload.get("totals") or {}
    typer.echo(t("cli.output.sessions", value=totals.get("total_sessions", 0)))
    typer.echo(t("cli.output.input_tokens", value=totals.get("input_tokens", 0)))
    typer.echo(t("cli.output.output_tokens", value=totals.get("output_tokens", 0)))
    typer.echo(t("cli.output.estimated_cost", value=totals.get("estimated_cost", 0)))
    typer.echo(t("cli.output.actual_cost", value=totals.get("actual_cost", 0)))


@cron_app.command("list")
def cron_list(json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_cron"))] = False) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.list_cron_jobs)
    if json_output:
        _json_dump({"jobs": payload})
        return
    for job in payload:
        typer.echo(f"{job['id']}  {job.get('name') or '-'}  {job.get('schedule_display') or '-'}  {_display_word(job.get('state'))}")


@cron_app.command("create")
def cron_create(
    prompt: str,
    schedule: Annotated[str, typer.Option("--schedule", help=t("cli.option.cron_schedule"))],
    name: Annotated[str, typer.Option("--name", help=t("cli.option.cron_name"))] = "",
    deliver: Annotated[str, typer.Option("--deliver", help=t("cli.option.cron_deliver"))] = "local",
    skill: Annotated[list[str] | None, typer.Option("--skill", help=t("cli.option.cron_skill"))] = None,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_created_job"))] = False,
) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.create_cron_job, prompt=prompt, schedule=schedule, name=name, deliver=deliver, skills=skill or [])
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.message.cron_created", job_id=payload["id"]))


@cron_app.command("pause")
def cron_pause(job_id: str, json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_updated_job"))] = False) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.pause_cron_job, job_id)
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.message.cron_paused", job_id=job_id))


@cron_app.command("resume")
def cron_resume(job_id: str, json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_updated_job"))] = False) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.resume_cron_job, job_id)
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.message.cron_resumed", job_id=job_id))


@cron_app.command("trigger")
def cron_trigger(job_id: str, json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_updated_job"))] = False) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.trigger_cron_job, job_id)
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.message.cron_triggered", job_id=job_id))


@cron_app.command("delete")
def cron_delete(
    job_id: str,
    yes: Annotated[bool, typer.Option("--yes", help=t("cli.option.confirm_delete_cron"))] = False,
) -> None:
    if not yes:
        typer.echo(t("cli.message.refuse_delete_cron_without_yes"))
        raise typer.Exit(code=1)
    adapter = _adapter()
    _adapter_call(adapter.delete_cron_job, job_id)
    typer.echo(t("cli.message.cron_deleted", job_id=job_id))


@skills_app.command("list")
def skills_list(json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_skills"))] = False) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.list_skills)
    if json_output:
        _json_dump({"skills": payload})
        return
    for skill in payload:
        state = _bool_word(bool(skill.get("enabled")))
        typer.echo(f"{skill['name']}  {skill.get('category') or '-'}  {state}")


@skills_app.command("enable")
def skills_enable(name: str) -> None:
    adapter = _adapter()
    _adapter_call(adapter.toggle_skill, name, enabled=True)
    typer.echo(t("cli.message.skill_enabled", name=name))


@skills_app.command("disable")
def skills_disable(name: str) -> None:
    adapter = _adapter()
    _adapter_call(adapter.toggle_skill, name, enabled=False)
    typer.echo(t("cli.message.skill_disabled", name=name))


@toolsets_app.command("list")
def toolsets_list(json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_toolsets"))] = False) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.list_toolsets)
    if json_output:
        _json_dump({"toolsets": payload})
        return
    for toolset in payload:
        state = _bool_word(bool(toolset.get("enabled")))
        typer.echo(f"{toolset['name']}  {state}  {toolset.get('description') or ''}")


@profiles_app.command("list")
def profiles_list(json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_profiles"))] = False) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.list_profiles)
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.output.active_profile", value=payload["active_profile"]))
    for profile in payload["profiles"]:
        status = t("common.active") if profile.get("active") else "-"
        typer.echo(f"{profile['name']}  {status}  {profile['path']}")


@backup_app.command("create")
def backup_create(
    output: Annotated[str | None, typer.Argument(help=t("cli.option.backup_output"))] = None,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_backup"))] = False,
) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.create_backup, output)
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.output.backup_created_at", value=payload["archive_path"]))


@backup_app.command("restore")
def backup_restore(
    archive_path: str,
    force: Annotated[bool, typer.Option("--force", help=t("cli.option.backup_force"))] = False,
    json_output: Annotated[bool, typer.Option("--json", help=t("cli.option.json_backup"))] = False,
) -> None:
    adapter = _adapter()
    payload = _adapter_call(adapter.restore_backup, archive_path, force=force)
    if json_output:
        _json_dump(payload)
        return
    typer.echo(t("cli.output.backup_restored_to", value=payload["restored_to"]))


def main() -> None:
    app(prog_name="hermes-link")
