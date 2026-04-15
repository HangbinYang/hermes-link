import json
import sqlite3
import sys
import zipfile

import pytest
import yaml

from hermes_link.hermes import discover_hermes_installation
from hermes_link.hermes_adapter import HermesAdapter, HermesAdapterError
from hermes_link.runtime import bootstrap_runtime, load_config, save_config, set_runtime_home


def _seed_fake_hermes_home(base_path):
    hermes_home = base_path / "fake-hermes"
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    (hermes_home / "cron").mkdir(parents=True, exist_ok=True)
    (hermes_home / "skills" / "utilities" / "demo-skill").mkdir(parents=True, exist_ok=True)
    (hermes_home / "profiles" / "work").mkdir(parents=True, exist_ok=True)

    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": "openai/gpt-5.4",
                "toolsets": ["web", "file"],
                "skills": {"disabled": ["demo-skill"]},
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text("OPENAI_API_KEY=sk-test-openai\nANTHROPIC_API_KEY=anthropic-token\n", encoding="utf-8")
    (hermes_home / "logs" / "agent.log").write_text("line1\nline2 error\nline3\n", encoding="utf-8")
    (hermes_home / "skills" / "utilities" / "demo-skill" / "SKILL.md").write_text(
        "# Demo Skill\n\nUseful test skill.\n",
        encoding="utf-8",
    )
    (hermes_home / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {"nous": {"access_token": "nous-secret"}}}),
        encoding="utf-8",
    )
    (hermes_home / "active_profile").write_text("work\n", encoding="utf-8")
    (hermes_home / "profiles" / "work" / "config.yaml").write_text(
        yaml.safe_dump({"model": "anthropic/claude-sonnet-4.6"}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job_1",
                        "name": "Daily Brief",
                        "prompt": "Summarize the day",
                        "skills": ["demo-skill"],
                        "skill": "demo-skill",
                        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
                        "schedule_display": "every 60m",
                        "repeat": {"times": None, "completed": 0},
                        "enabled": True,
                        "state": "scheduled",
                        "created_at": "2026-04-14T00:00:00+00:00",
                        "next_run_at": "2026-04-14T01:00:00+00:00",
                        "deliver": "local",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    conn = sqlite3.connect(hermes_home / "state.db")
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            model TEXT,
            parent_session_id TEXT,
            title TEXT,
            started_at REAL,
            ended_at REAL,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            reasoning_tokens INTEGER,
            estimated_cost_usd REAL,
            actual_cost_usd REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            tool_call_id TEXT,
            timestamp REAL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sessions (
            id, source, model, parent_session_id, title, started_at, ended_at, message_count, tool_call_count,
            input_tokens, output_tokens, cache_read_tokens, reasoning_tokens,
            estimated_cost_usd, actual_cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "sess_1",
            "cli",
            "openai/gpt-5.4",
            None,
            "My Session",
            1_710_000_000,
            None,
            1,
            0,
            120,
            240,
            0,
            12,
            0.12,
            0.10,
        ),
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, role, content, tool_calls, tool_name, tool_call_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("sess_1", "user", "hello hermes", None, None, None, 1_710_000_101),
    )
    conn.commit()
    conn.close()
    return hermes_home


def test_adapter_fallback_control_plane(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    bootstrap_runtime()
    hermes_home = _seed_fake_hermes_home(tmp_path)

    config = load_config()
    config.hermes.home = str(hermes_home)
    save_config(config)

    monkeypatch.setattr(HermesAdapter, "_resolve_bridge_python", lambda self: None)
    adapter = HermesAdapter(load_config())

    assert adapter.get_config()["model"] == "openai/gpt-5.4"
    assert len(adapter.list_env_vars()) == 2
    providers = {item["provider"]: item for item in adapter.get_provider_auth_status()}
    assert providers["anthropic"]["connected"] is True
    assert providers["nous"]["connected"] is True

    sessions = adapter.list_sessions()
    assert sessions["total"] == 1
    assert sessions["sessions"][0]["id"] == "sess_1"
    assert sessions["sessions"][0]["message_count"] == 1
    assert sessions["sessions"][0]["preview"] == "hello hermes"

    messages = adapter.get_session_messages("sess_1")
    assert messages["messages"][0]["content"] == "hello hermes"
    assert messages["messages"][0]["timestamp"] == 1_710_000_101

    logs = adapter.list_logs(search="error")
    assert logs["lines"] == ["line2 error"]

    analytics = adapter.get_usage_analytics(days=3650)
    assert analytics["totals"]["total_sessions"] == 1

    cron_jobs = adapter.list_cron_jobs()
    assert cron_jobs[0]["id"] == "job_1"

    skills = adapter.list_skills()
    demo_skill = next(skill for skill in skills if skill["name"] == "demo-skill")
    assert demo_skill["enabled"] is False

    toolsets = adapter.list_toolsets()
    assert any(item["name"] == "web" and item["enabled"] for item in toolsets)

    profiles = adapter.list_profiles()
    assert profiles["active_profile"] == "work"
    work_profile = next(profile for profile in profiles["profiles"] if profile["name"] == "work")
    assert work_profile["active"] is True
    assert work_profile["model"] == "anthropic/claude-sonnet-4.6"

    backup = adapter.create_backup()
    assert backup["ok"] is True


def test_provider_bridge_response_matches_upstream_shape(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()
    hermes_home = _seed_fake_hermes_home(tmp_path)

    config = load_config()
    config.hermes.home = str(hermes_home)
    save_config(config)

    adapter = HermesAdapter(load_config())
    adapter._run_bridge = lambda *args, **kwargs: {
        "providers": [
            {
                "id": "claude-code",
                "name": "Claude Code (subscription)",
                "flow": "external",
                "cli_command": "claude setup-token",
                "docs_url": "https://docs.claude.com/en/docs/claude-code",
                "status": {
                    "logged_in": True,
                    "source": "claude_code_cli",
                    "source_label": "~/.claude/.credentials.json",
                    "token_preview": "abcd...wxyz",
                    "expires_at": None,
                    "has_refresh_token": True,
                },
            }
        ]
    }

    providers = {item["provider"]: item for item in adapter.get_provider_auth_status()}

    assert providers["claude-code"]["connected"] is True
    assert providers["claude-code"]["status"] == "connected"
    assert providers["claude-code"]["flow"] == "external"
    assert providers["claude-code"]["source"] == "claude_code_cli"


def test_run_bridge_executes_multiline_body(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    bootstrap_runtime()
    hermes_home = _seed_fake_hermes_home(tmp_path)

    config = load_config()
    config.hermes.home = str(hermes_home)
    save_config(config)

    monkeypatch.setattr(HermesAdapter, "_resolve_bridge_python", lambda self: sys.executable)
    adapter = HermesAdapter(load_config())

    result = adapter._run_bridge(
        """
        value = int(payload.get("delta", 0))
        return {
            "sum": value + 2,
            "profile": "ok",
        }
        """,
        {"delta": 5},
    )

    assert result == {"sum": 7, "profile": "ok"}


def test_profiles_bridge_matches_upstream_profile_info(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()
    hermes_home = _seed_fake_hermes_home(tmp_path)

    config = load_config()
    config.hermes.home = str(hermes_home)
    save_config(config)

    adapter = HermesAdapter(load_config())
    if adapter.resolve_bridge_python() is None:
        pytest.skip("bridge python is unavailable")

    profiles = adapter.list_profiles()
    work_profile = next(profile for profile in profiles["profiles"] if profile["name"] == "work")

    assert profiles["active_profile"] == "work"
    assert work_profile["active"] is True
    assert work_profile["model"] == "anthropic/claude-sonnet-4.6"
    assert work_profile["is_default"] is False


def test_session_fallback_excludes_children_and_orphans_them_on_delete(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    bootstrap_runtime()
    hermes_home = _seed_fake_hermes_home(tmp_path)

    config = load_config()
    config.hermes.home = str(hermes_home)
    save_config(config)

    conn = sqlite3.connect(hermes_home / "state.db")
    conn.execute(
        """
        INSERT INTO sessions (
            id, source, model, parent_session_id, title, started_at, ended_at, message_count, tool_call_count,
            input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "sess_child",
            "cli",
            "openai/gpt-5.4",
            "sess_1",
            "Child Session",
            1_710_000_050,
            None,
            1,
            0,
            10,
            20,
            0,
            0,
            0.01,
            0.01,
        ),
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, role, content, tool_calls, tool_name, tool_call_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("sess_child", "user", "child session", None, None, None, 1_710_000_051),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(HermesAdapter, "_resolve_bridge_python", lambda self: None)
    adapter = HermesAdapter(load_config())

    sessions = adapter.list_sessions()
    assert sessions["total"] == 2
    assert [item["id"] for item in sessions["sessions"]] == ["sess_1"]

    deleted = adapter.delete_session("sess_1")
    assert deleted["ok"] is True

    conn = sqlite3.connect(hermes_home / "state.db")
    conn.row_factory = sqlite3.Row
    child = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", ("sess_child",)).fetchone()
    conn.close()

    assert child["parent_session_id"] is None

    with pytest.raises(HermesAdapterError) as exc_info:
        adapter.delete_session("missing-session")
    assert exc_info.value.code == "session_not_found"


def test_discovery_respects_active_profile_and_explicit_profile(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    hermes_root = tmp_path / ".hermes"
    active_profile_home = hermes_root / "profiles" / "coder"
    explicit_profile_home = hermes_root / "profiles" / "ops"
    active_profile_home.mkdir(parents=True, exist_ok=True)
    explicit_profile_home.mkdir(parents=True, exist_ok=True)
    (hermes_root / "active_profile").write_text("coder\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr("hermes_link.hermes.shutil.which", lambda name: None)

    config = load_config()
    discovered = discover_hermes_installation(config)
    assert discovered.hermes_home == str(active_profile_home)

    config.hermes.profile = "ops"
    discovered = discover_hermes_installation(config)
    assert discovered.hermes_home == str(explicit_profile_home)


def test_toolsets_string_config_is_normalized(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    bootstrap_runtime()
    hermes_home = _seed_fake_hermes_home(tmp_path)

    config = load_config()
    config.hermes.home = str(hermes_home)
    save_config(config)

    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"toolsets": "web"}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    monkeypatch.setattr(HermesAdapter, "_resolve_bridge_python", lambda self: None)
    adapter = HermesAdapter(load_config())

    toolsets = {item["name"]: item for item in adapter.list_toolsets()}
    assert toolsets["web"]["enabled"] is True
    assert toolsets["browser"]["enabled"] is False


def test_create_backup_skips_archive_inside_hermes_home(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    bootstrap_runtime()
    hermes_home = _seed_fake_hermes_home(tmp_path)

    config = load_config()
    config.hermes.home = str(hermes_home)
    save_config(config)

    monkeypatch.setattr(HermesAdapter, "_resolve_bridge_python", lambda self: None)
    adapter = HermesAdapter(load_config())

    archive_path = hermes_home / "backups" / "nested-backup.zip"
    result = adapter.create_backup(str(archive_path))

    with zipfile.ZipFile(result["archive_path"], "r") as archive:
        names = archive.namelist()

    assert "backups/nested-backup.zip" not in names


def test_restore_backup_rejects_unsafe_members(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    bootstrap_runtime()
    hermes_home = tmp_path / "fake-hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)

    config = load_config()
    config.hermes.home = str(hermes_home)
    save_config(config)

    monkeypatch.setattr(HermesAdapter, "_resolve_bridge_python", lambda self: None)
    adapter = HermesAdapter(load_config())

    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../../escape.txt", "boom")

    with pytest.raises(HermesAdapterError) as exc_info:
        adapter.restore_backup(str(archive_path))

    assert exc_info.value.code == "backup_restore_unsafe_member"
