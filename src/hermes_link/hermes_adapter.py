from __future__ import annotations

from collections import deque
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import yaml
from croniter import croniter

from hermes_link.hermes import discover_hermes_installation
from hermes_link.i18n import t
from hermes_link.models import LinkConfig

LOG_FILE_MAP = {
    "agent": "agent.log",
    "gateway": "gateway.log",
    "web": "web.log",
    "errors": "errors.log",
}

TOOLSET_CATALOG = [
    ("web", "Web Search & Scraping", "web_search, web_extract"),
    ("browser", "Browser Automation", "navigate, click, type, scroll"),
    ("terminal", "Terminal & Processes", "terminal, process"),
    ("file", "File Operations", "read, write, patch, search"),
    ("code_execution", "Code Execution", "execute_code"),
    ("vision", "Vision / Image Analysis", "vision_analyze"),
    ("image_gen", "Image Generation", "image_generate"),
    ("moa", "Mixture of Agents", "mixture_of_agents"),
    ("tts", "Text-to-Speech", "text_to_speech"),
    ("skills", "Skills", "list, view, manage"),
    ("todo", "Task Planning", "todo"),
    ("memory", "Memory", "persistent memory across sessions"),
    ("session_search", "Session Search", "search past conversations"),
    ("clarify", "Clarifying Questions", "clarify"),
    ("delegation", "Task Delegation", "delegate_task"),
    ("cronjob", "Cron Jobs", "create/list/update/pause/resume/run"),
    ("rl", "RL Training", "training tools"),
    ("homeassistant", "Home Assistant", "smart home device control"),
]

PROVIDER_ENV_GROUPS = {
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "google": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "firecrawl": ["FIRECRAWL_API_KEY", "FIRECRAWL_API_URL"],
    "exa": ["EXA_API_KEY"],
    "tavily": ["TAVILY_API_KEY"],
    "parallel": ["PARALLEL_API_KEY"],
    "fal": ["FAL_KEY"],
    "elevenlabs": ["ELEVENLABS_API_KEY"],
    "qwen-oauth": [],
    "openai-codex": [],
    "nous": [],
}


class HermesAdapterError(RuntimeError):
    def __init__(self, code: str, message: str, *, detail: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _coerce_json_value(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return raw
    return parsed if parsed is not None else raw


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise HermesAdapterError("hermes_config_invalid", t("adapter.hermes_config_invalid"))
    return payload


def _write_yaml_mapping(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config_", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(value, handle, sort_keys=False, allow_unicode=True)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = raw_line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={value}" for key, value in sorted(values.items())]
    content = "\n".join(lines)
    if content:
        content += "\n"
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".env_", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _redact_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _nested_set(payload: dict[str, Any], dotted_key: str, value: Any) -> dict[str, Any]:
    parts = [part for part in dotted_key.split(".") if part]
    if not parts:
        raise HermesAdapterError("invalid_key", t("adapter.invalid_key_empty"))
    cursor = payload
    for part in parts[:-1]:
        next_value = cursor.get(part)
        if next_value is None:
            next_value = {}
            cursor[part] = next_value
        if not isinstance(next_value, dict):
            raise HermesAdapterError("invalid_key", t("adapter.invalid_key_conflict", dotted_key=dotted_key))
        cursor = next_value
    cursor[parts[-1]] = value
    return payload


def _first_nonempty_line(text: str) -> str | None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and line != "---":
            return line
    return None


def _normalize_name_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        normalized: list[str] = []
        for item in value:
            normalized.extend(_normalize_name_list(item))
        return normalized
    return []


def _tail_log_lines(path: Path, *, limit: int, search: str | None) -> list[str]:
    needle = search.lower() if search else None
    buffer: deque[str] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            if needle and needle not in line.lower():
                continue
            buffer.append(line)
    return list(buffer)


def _reference_hermes_repo() -> Path:
    return Path(__file__).resolve().parents[4] / "reference" / "hermes-agent"


def _parse_profile_model_info(profile_dir: Path) -> tuple[str | None, str | None]:
    config_path = profile_dir / "config.yaml"
    if not config_path.exists():
        return None, None

    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None, None

    if not isinstance(payload, dict):
        return None, None

    model_cfg = payload.get("model")
    if isinstance(model_cfg, str):
        return model_cfg, None
    if isinstance(model_cfg, dict):
        return model_cfg.get("default") or model_cfg.get("model"), model_cfg.get("provider")
    return None, None


def _gateway_pid_is_running(profile_dir: Path) -> bool:
    pid_path = profile_dir / "gateway.pid"
    if not pid_path.exists():
        return False

    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        if not raw:
            return False
        data = json.loads(raw) if raw.startswith("{") else {"pid": int(raw)}
        pid = int(data["pid"])
        os.kill(pid, 0)
        return True
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
        return False


def _count_profile_skills(profile_dir: Path) -> int:
    skills_dir = profile_dir / "skills"
    if not skills_dir.is_dir():
        return 0

    count = 0
    for skill_file in skills_dir.rglob("SKILL.md"):
        path_text = str(skill_file)
        if "/.hub/" in path_text or "/.git/" in path_text:
            continue
        count += 1
    return count


def _zip_member_path(root: Path, member: str) -> Path:
    relative = PurePosixPath(member)
    if (
        not member
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or (relative.parts and relative.parts[0].endswith(":"))
    ):
        raise HermesAdapterError("backup_restore_unsafe_member", t("adapter.backup_restore_unsafe_member", member=member))
    destination = (root / Path(*relative.parts)).resolve()
    root_resolved = root.resolve()
    if destination != root_resolved and root_resolved not in destination.parents:
        raise HermesAdapterError("backup_restore_unsafe_member", t("adapter.backup_restore_unsafe_member", member=member))
    return destination


class HermesAdapter:
    def __init__(self, config: LinkConfig):
        self.config = config
        self.discovery = discover_hermes_installation(config)
        self._bridge_python: str | None = None

    @property
    def hermes_home(self) -> Path:
        if not self.discovery.hermes_home:
            raise HermesAdapterError("hermes_home_missing", t("adapter.hermes_home_missing"))
        return Path(self.discovery.hermes_home).expanduser()

    @property
    def state_db_path(self) -> Path:
        return self.hermes_home / "state.db"

    @property
    def config_path(self) -> Path:
        return self.hermes_home / "config.yaml"

    @property
    def env_path(self) -> Path:
        return self.hermes_home / ".env"

    @property
    def auth_path(self) -> Path:
        return self.hermes_home / "auth.json"

    def _ensure_home(self) -> Path:
        home = self.hermes_home
        home.mkdir(parents=True, exist_ok=True)
        return home

    def _ensure_detected(self) -> None:
        if not self.discovery.found:
            raise HermesAdapterError("hermes_not_found", t("adapter.hermes_not_found"))

    def _resolve_bridge_python(self) -> str | None:
        if self._bridge_python is not None:
            return self._bridge_python

        env_python = os.getenv("HERMES_PYTHON", "").strip()
        if env_python:
            self._bridge_python = env_python
            return env_python

        executable = self.discovery.executable_path
        if executable:
            exec_path = Path(executable)
            if exec_path.suffix.lower() == ".exe":
                candidate = exec_path.with_name("python.exe")
                if candidate.exists():
                    self._bridge_python = str(candidate)
                    return self._bridge_python
            try:
                first_line = exec_path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            except Exception:
                first_line = ""
            if first_line.startswith("#!"):
                shebang = first_line[2:].strip()
                if shebang.startswith("/usr/bin/env "):
                    parts = shebang.split()
                    if len(parts) >= 2:
                        resolved = shutil.which(parts[1])
                        if resolved:
                            self._bridge_python = resolved
                            return resolved
                else:
                    candidate = shebang.split()[0]
                    if candidate:
                        self._bridge_python = candidate
                        return candidate
            sibling = exec_path.with_name("python")
            if sibling.exists():
                self._bridge_python = str(sibling)
                return self._bridge_python

        monorepo_reference = Path(__file__).resolve().parents[4] / "reference" / "hermes-agent"
        hermes_repo = Path(self.discovery.hermes_home).expanduser() / "hermes-agent" if self.discovery.hermes_home else None
        if monorepo_reference.exists() or (hermes_repo and hermes_repo.exists()):
            self._bridge_python = sys.executable
            return self._bridge_python

        self._bridge_python = ""
        return None

    def _build_bridge_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.discovery.hermes_home:
            env["HERMES_HOME"] = self.discovery.hermes_home

        python_paths: list[str] = []
        monorepo_reference = Path(__file__).resolve().parents[4] / "reference" / "hermes-agent"
        hermes_repo = Path(self.discovery.hermes_home).expanduser() / "hermes-agent" if self.discovery.hermes_home else None
        for candidate in (monorepo_reference, hermes_repo):
            if candidate and candidate.exists():
                python_paths.append(str(candidate))
        existing = env.get("PYTHONPATH", "").strip()
        if existing:
            python_paths.append(existing)
        if python_paths:
            env["PYTHONPATH"] = os.pathsep.join(python_paths)
        return env

    def resolve_bridge_python(self) -> str | None:
        return self._resolve_bridge_python()

    def build_bridge_env(self) -> dict[str, str]:
        return self._build_bridge_env()

    def _run_bridge(self, body: str, payload: dict[str, Any] | None = None, *, timeout: float = 20.0) -> Any | None:
        python_exec = self._resolve_bridge_python()
        if not python_exec:
            return None

        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        bridge_body = textwrap.indent(textwrap.dedent(body).strip(), "    ")
        script = "\n".join(
            [
                "import asyncio",
                "import json",
                "import traceback",
                "",
                f"payload = json.loads({payload_json!r})",
                "",
                "async def _run(payload):",
                bridge_body,
                "",
                'marker = "__HERMES_LINK_JSON__"',
                "try:",
                "    result = asyncio.run(_run(payload))",
                "    print(marker)",
                '    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))',
                "except Exception as exc:",
                "    print(marker)",
                "    print(",
                "        json.dumps(",
                "            {",
                '                "ok": False,',
                '                "error": str(exc),',
                '                "type": exc.__class__.__name__,',
                '                "traceback": traceback.format_exc(),',
                "            },",
                "            ensure_ascii=False,",
                "            default=str,",
                "        )",
                "    )",
            ]
        )

        try:
            completed = subprocess.run(
                [python_exec, "-c", script],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=self._build_bridge_env(),
            )
        except Exception:
            return None

        marker = "__HERMES_LINK_JSON__"
        combined = completed.stdout or ""
        if marker not in combined:
            return None
        payload_text = combined.split(marker, 1)[1].strip().splitlines()[0]
        try:
            decoded = json.loads(payload_text)
        except json.JSONDecodeError:
            return None
        if not decoded.get("ok"):
            return None
        return decoded.get("result")

    def get_config_raw(self) -> str:
        return self.config_path.read_text(encoding="utf-8") if self.config_path.exists() else ""

    def get_config(self) -> dict[str, Any]:
        return _read_yaml_mapping(self.config_path)

    def save_config(self, value: dict[str, Any]) -> dict[str, Any]:
        _write_yaml_mapping(self.config_path, value)
        return self.get_config()

    def save_config_raw(self, yaml_text: str) -> dict[str, Any]:
        parsed = yaml.safe_load(yaml_text) or {}
        if not isinstance(parsed, dict):
            raise HermesAdapterError("hermes_config_invalid", t("adapter.yaml_mapping_required"))
        _write_yaml_mapping(self.config_path, parsed)
        return parsed

    def set_config_value(self, dotted_key: str, raw_value: str) -> dict[str, Any]:
        current = self.get_config()
        updated = _nested_set(current, dotted_key, _coerce_json_value(raw_value))
        return self.save_config(updated)

    def list_env_vars(self) -> list[dict[str, Any]]:
        values = _parse_env_file(self.env_path)
        rows = []
        for key in sorted(values):
            rows.append(
                {
                    "key": key,
                    "is_set": True,
                    "redacted_value": _redact_secret(values[key]),
                    "value_length": len(values[key]),
                }
            )
        return rows

    def get_provider_auth_status(self) -> list[dict[str, Any]]:
        env_values = _parse_env_file(self.env_path)
        auth_store = {}
        if self.auth_path.exists():
            try:
                auth_store = json.loads(self.auth_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                auth_store = {}

        providers = []
        for provider, keys in PROVIDER_ENV_GROUPS.items():
            env_matches = [key for key in keys if env_values.get(key)]
            oauth_state = None
            if isinstance(auth_store, dict):
                provider_state = ((auth_store.get("providers") or {}) if isinstance(auth_store.get("providers"), dict) else {}).get(provider)
                if provider_state:
                    oauth_state = "connected"
            providers.append(
                {
                    "provider": provider,
                    "auth_kind": "oauth" if not keys else "api_key",
                    "connected": bool(env_matches or oauth_state),
                    "keys": env_matches,
                    "status": oauth_state or ("configured" if env_matches else "missing"),
                }
            )

        bridge_value = self._run_bridge(
            """
            from hermes_cli.web_server import list_oauth_providers
            return await list_oauth_providers()
            """,
            timeout=25.0,
        )
        bridge_providers = bridge_value.get("providers") if isinstance(bridge_value, dict) else None
        if isinstance(bridge_providers, list):
            existing = {item["provider"]: item for item in providers}
            for item in bridge_providers:
                provider_id = str(item.get("id") or "")
                if not provider_id:
                    continue
                bridge_status = item.get("status") if isinstance(item.get("status"), dict) else {}
                current = existing.get(provider_id, {"provider": provider_id})
                current.update(
                    {
                        "provider": provider_id,
                        "auth_kind": "oauth",
                        "connected": bool(bridge_status.get("logged_in")),
                        "status": "connected" if bridge_status.get("logged_in") else ("error" if bridge_status.get("error") else "missing"),
                        "label": item.get("name"),
                        "flow": item.get("flow"),
                        "cli_command": item.get("cli_command"),
                        "docs_url": item.get("docs_url"),
                        "source": bridge_status.get("source"),
                        "source_label": bridge_status.get("source_label"),
                        "token_preview": bridge_status.get("token_preview"),
                        "expires_at": bridge_status.get("expires_at"),
                        "has_refresh_token": bool(bridge_status.get("has_refresh_token")),
                        "detail": item,
                    }
                )
                existing[provider_id] = current
            providers = sorted(existing.values(), key=lambda item: item["provider"])
        return providers

    def set_env_value(self, key: str, value: str) -> dict[str, Any]:
        current = _parse_env_file(self.env_path)
        current[key.strip()] = value
        _write_env_file(self.env_path, current)
        return {"ok": True, "key": key.strip()}

    def delete_env_value(self, key: str) -> dict[str, Any]:
        current = _parse_env_file(self.env_path)
        removed = current.pop(key.strip(), None) is not None
        _write_env_file(self.env_path, current)
        return {"ok": removed, "key": key.strip()}

    def _state_db_connection(self) -> sqlite3.Connection:
        if not self.state_db_path.exists():
            raise HermesAdapterError("sessions_db_missing", t("adapter.sessions_db_missing"))
        conn = sqlite3.connect(self.state_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _message_timestamp_column(self, conn: sqlite3.Connection) -> str | None:
        columns = self._table_columns(conn, "messages")
        if "timestamp" in columns:
            return "timestamp"
        if "created_at" in columns:
            return "created_at"
        return None

    @staticmethod
    def _session_value_expr(columns: set[str], name: str, fallback_sql: str = "NULL") -> str:
        if name in columns:
            return f"s.{name} AS {name}"
        return f"{fallback_sql} AS {name}"

    @staticmethod
    def _session_coalesce_expr(columns: set[str], name: str, fallback_sql: str = "0") -> str:
        if name in columns:
            return f"COALESCE(s.{name}, {fallback_sql}) AS {name}"
        return f"{fallback_sql} AS {name}"

    def _resolve_session_id_fallback(self, conn: sqlite3.Connection, session_id_or_prefix: str) -> str | None:
        exact = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id_or_prefix,)).fetchone()
        if exact:
            return str(exact["id"])

        escaped = (
            session_id_or_prefix.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        matches = conn.execute(
            """
            SELECT id
            FROM sessions
            WHERE id LIKE ? ESCAPE '\\'
            ORDER BY started_at DESC
            LIMIT 2
            """,
            (f"{escaped}%",),
        ).fetchall()
        if len(matches) == 1:
            return str(matches[0]["id"])
        return None

    def _profiles_root_home(self) -> Path:
        home = self.hermes_home.resolve()
        if home.parent.name == "profiles":
            return home.parent.parent
        return home

    def list_sessions(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            import time
            from hermes_state import SessionDB

            db = SessionDB()
            try:
                sessions = db.list_sessions_rich(limit=int(payload.get("limit", 20)), offset=int(payload.get("offset", 0)))
                now = time.time()
                for session in sessions:
                    session["is_active"] = (
                        session.get("ended_at") is None
                        and (now - session.get("last_active", session.get("started_at", 0))) < 300
                    )
                return {
                    "sessions": sessions,
                    "total": db.session_count(),
                    "limit": int(payload.get("limit", 20)),
                    "offset": int(payload.get("offset", 0)),
                }
            finally:
                db.close()
            """,
            {"limit": limit, "offset": offset},
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            return bridge_value

        with self._state_db_connection() as conn:
            session_columns = self._table_columns(conn, "sessions")
            timestamp_column = self._message_timestamp_column(conn)
            message_order = f"m.{timestamp_column}, m.id" if timestamp_column else "m.id"
            last_active_expr = (
                f"COALESCE((SELECT MAX(m2.{timestamp_column}) FROM messages m2 WHERE m2.session_id = s.id), s.started_at)"
                if timestamp_column
                else "s.started_at"
            )
            preview_expr = f"""
                COALESCE(
                    (
                        SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                        FROM messages m
                        WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                        ORDER BY {message_order}
                        LIMIT 1
                    ),
                    ''
                ) AS _preview_raw
            """
            message_count_expr = (
                self._session_coalesce_expr(session_columns, "message_count", "0")
                if "message_count" in session_columns
                else "(SELECT COUNT(*) FROM messages m3 WHERE m3.session_id = s.id) AS message_count"
            )
            tool_call_count_expr = (
                self._session_coalesce_expr(session_columns, "tool_call_count", "0")
                if "tool_call_count" in session_columns
                else """
                (
                    SELECT COUNT(*)
                    FROM messages m4
                    WHERE m4.session_id = s.id
                      AND (
                        m4.role = 'tool'
                        OR m4.tool_call_id IS NOT NULL
                        OR m4.tool_name IS NOT NULL
                      )
                ) AS tool_call_count
                """
            )
            where_clause = "WHERE s.parent_session_id IS NULL" if "parent_session_id" in session_columns else ""
            total_row = conn.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()
            rows = conn.execute(
                f"""
                SELECT
                    s.id,
                    {self._session_value_expr(session_columns, "source")},
                    {self._session_value_expr(session_columns, "model")},
                    {self._session_value_expr(session_columns, "title")},
                    {self._session_value_expr(session_columns, "started_at", "0")},
                    {self._session_value_expr(session_columns, "ended_at")},
                    {last_active_expr} AS last_active,
                    {message_count_expr},
                    {tool_call_count_expr},
                    {self._session_coalesce_expr(session_columns, "input_tokens", "0")},
                    {self._session_coalesce_expr(session_columns, "output_tokens", "0")},
                    {self._session_coalesce_expr(session_columns, "cache_read_tokens", "0")},
                    {self._session_coalesce_expr(session_columns, "reasoning_tokens", "0")},
                    {self._session_coalesce_expr(session_columns, "estimated_cost_usd", "0")},
                    {self._session_coalesce_expr(session_columns, "actual_cost_usd", "0")},
                    {preview_expr}
                FROM sessions
                s
                {where_clause}
                ORDER BY s.started_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            sessions = []
            for row in rows:
                raw_preview = (row["_preview_raw"] or "").strip()
                sessions.append(
                    {
                        "id": row["id"],
                        "source": row["source"],
                        "model": row["model"],
                        "title": row["title"],
                        "started_at": row["started_at"],
                        "ended_at": row["ended_at"],
                        "last_active": row["last_active"],
                        "is_active": row["ended_at"] is None and (datetime.now().timestamp() - (row["last_active"] or row["started_at"] or 0)) < 300,
                        "message_count": int(row["message_count"] or 0),
                        "tool_call_count": int(row["tool_call_count"] or 0),
                        "input_tokens": row["input_tokens"],
                        "output_tokens": row["output_tokens"],
                        "cache_read_tokens": row["cache_read_tokens"],
                        "reasoning_tokens": row["reasoning_tokens"],
                        "estimated_cost_usd": row["estimated_cost_usd"],
                        "actual_cost_usd": row["actual_cost_usd"],
                        "preview": raw_preview[:60] + ("..." if len(raw_preview) > 60 else ""),
                    }
                )
            return {
                "sessions": sessions,
                "total": int(total_row["count"]) if total_row else 0,
                "limit": limit,
                "offset": offset,
            }

    def search_sessions(self, query: str, *, limit: int = 20) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            import re
            from hermes_state import SessionDB

            db = SessionDB()
            try:
                terms = []
                for token in re.findall(r'"[^"]*"|\\S+', str(payload.get("query", "")).strip()):
                    if token.startswith('"') or token.endswith("*"):
                        terms.append(token)
                    else:
                        terms.append(token + "*")
                matches = db.search_messages(query=" ".join(terms), limit=int(payload.get("limit", 20)))
                seen = {}
                for item in matches:
                    session_id = item["session_id"]
                    if session_id not in seen:
                        seen[session_id] = {
                            "session_id": session_id,
                            "snippet": item.get("snippet", ""),
                            "role": item.get("role"),
                            "source": item.get("source"),
                            "model": item.get("model"),
                            "session_started": item.get("session_started"),
                        }
                return {"results": list(seen.values())}
            finally:
                db.close()
            """,
            {"query": query, "limit": limit},
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            return bridge_value

        needle = query.strip()
        if not needle:
            return {"results": []}

        with self._state_db_connection() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT
                        m.session_id,
                        m.role,
                        m.content,
                        s.source,
                        s.model,
                        s.started_at AS session_started
                    FROM messages m
                    LEFT JOIN sessions s ON s.id = m.session_id
                    WHERE m.content LIKE ?
                    ORDER BY m.id DESC
                    LIMIT ?
                    """,
                    (f"%{needle}%", limit * 4),
                ).fetchall()
            except sqlite3.Error:
                return {"results": []}

        seen: dict[str, dict[str, Any]] = {}
        for row in rows:
            session_id = row["session_id"]
            if session_id in seen:
                continue
            seen[session_id] = {
                "session_id": session_id,
                "snippet": row["content"][:240] if row["content"] else "",
                "role": row["role"],
                "source": row["source"],
                "model": row["model"],
                "session_started": row["session_started"],
            }
            if len(seen) >= limit:
                break
        return {"results": list(seen.values())}

    def get_session(self, session_id: str) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            from hermes_state import SessionDB

            db = SessionDB()
            try:
                resolved = db.resolve_session_id(payload["session_id"])
                session = db.get_session(resolved) if resolved else None
                if not session:
                    raise RuntimeError("session_not_found")
                return session
            finally:
                db.close()
            """,
            {"session_id": session_id},
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            return bridge_value

        with self._state_db_connection() as conn:
            resolved = self._resolve_session_id_fallback(conn, session_id)
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (resolved,)).fetchone() if resolved else None
            if row is None:
                raise HermesAdapterError("session_not_found", t("adapter.session_not_found", session_id=session_id))
            return dict(row)

    def get_session_messages(self, session_id: str) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            from hermes_state import SessionDB

            db = SessionDB()
            try:
                resolved = db.resolve_session_id(payload["session_id"])
                if not resolved:
                    raise RuntimeError("session_not_found")
                return {"session_id": resolved, "messages": db.get_messages(resolved)}
            finally:
                db.close()
            """,
            {"session_id": session_id},
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            return bridge_value

        with self._state_db_connection() as conn:
            resolved = self._resolve_session_id_fallback(conn, session_id)
            if not resolved:
                raise HermesAdapterError("session_not_found", t("adapter.session_not_found", session_id=session_id))
            timestamp_column = self._message_timestamp_column(conn)
            order_by = f"{timestamp_column}, id" if timestamp_column else "id"
            timestamp_alias = ""
            if timestamp_column and timestamp_column != "timestamp":
                timestamp_alias = f", {timestamp_column} AS timestamp"
            try:
                rows = conn.execute(
                    f"""
                    SELECT *{timestamp_alias}
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY {order_by}
                    """,
                    (resolved,),
                ).fetchall()
            except sqlite3.Error as exc:
                raise HermesAdapterError("messages_unavailable", t("adapter.messages_unavailable")) from exc
            messages: list[dict[str, Any]] = []
            for row in rows:
                message = dict(row)
                if timestamp_column != "timestamp":
                    message.pop("created_at", None)
                if "timestamp" not in message:
                    message["timestamp"] = None
                tool_calls = message.get("tool_calls")
                if tool_calls:
                    try:
                        message["tool_calls"] = json.loads(tool_calls)
                    except (json.JSONDecodeError, TypeError):
                        message["tool_calls"] = []
                messages.append(message)
            return {
                "session_id": resolved,
                "messages": messages,
            }

    def delete_session(self, session_id: str) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            from hermes_state import SessionDB

            db = SessionDB()
            try:
                return {"ok": bool(db.delete_session(payload["session_id"]))}
            finally:
                db.close()
            """,
            {"session_id": session_id},
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            if not bridge_value.get("ok"):
                raise HermesAdapterError("session_not_found", t("adapter.session_not_found", session_id=session_id))
            return bridge_value

        with self._state_db_connection() as conn:
            session_columns = self._table_columns(conn, "sessions")
            exists = conn.execute("SELECT COUNT(*) AS count FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not exists or int(exists["count"]) == 0:
                raise HermesAdapterError("session_not_found", t("adapter.session_not_found", session_id=session_id))
            if "parent_session_id" in session_columns:
                conn.execute(
                    "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
                    (session_id,),
                )
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return {"ok": True, "session_id": session_id}

    def list_logs(self, *, file: str = "agent", lines: int = 100, search: str | None = None) -> dict[str, Any]:
        filename = LOG_FILE_MAP.get(file, file)
        log_path = self.hermes_home / "logs" / filename
        if not log_path.exists():
            return {"file": file, "path": str(log_path), "lines": []}

        limit = max(1, min(lines, 1000))
        return {
            "file": file,
            "path": str(log_path),
            "lines": _tail_log_lines(log_path, limit=limit, search=search),
        }

    def get_usage_analytics(self, *, days: int = 30) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            import time
            from hermes_state import SessionDB

            db = SessionDB()
            try:
                cutoff = time.time() - (int(payload.get("days", 30)) * 86400)
                cur = db._conn.execute(
                    '''
                    SELECT date(started_at, 'unixepoch') AS day,
                           SUM(input_tokens) AS input_tokens,
                           SUM(output_tokens) AS output_tokens,
                           SUM(cache_read_tokens) AS cache_read_tokens,
                           SUM(reasoning_tokens) AS reasoning_tokens,
                           COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                           COALESCE(SUM(actual_cost_usd), 0) AS actual_cost,
                           COUNT(*) AS sessions
                    FROM sessions
                    WHERE started_at > ?
                    GROUP BY day
                    ORDER BY day
                    ''',
                    (cutoff,),
                )
                daily = [dict(row) for row in cur.fetchall()]
                by_model = [dict(row) for row in db._conn.execute(
                    '''
                    SELECT model,
                           SUM(input_tokens) AS input_tokens,
                           SUM(output_tokens) AS output_tokens,
                           COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                           COUNT(*) AS sessions
                    FROM sessions
                    WHERE started_at > ? AND model IS NOT NULL
                    GROUP BY model
                    ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
                    ''',
                    (cutoff,),
                ).fetchall()]
                totals_row = db._conn.execute(
                    '''
                    SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(output_tokens), 0) AS output_tokens,
                           COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                           COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                           COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                           COALESCE(SUM(actual_cost_usd), 0) AS actual_cost,
                           COUNT(*) AS total_sessions
                    FROM sessions
                    WHERE started_at > ?
                    ''',
                    (cutoff,),
                ).fetchone()
                return {"daily": daily, "by_model": by_model, "totals": dict(totals_row)}
            finally:
                db.close()
            """,
            {"days": days},
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            return bridge_value

        cutoff = datetime.now().timestamp() - (days * 86400)
        with self._state_db_connection() as conn:
            daily = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT date(started_at, 'unixepoch') AS day,
                           COALESCE(SUM(input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(output_tokens), 0) AS output_tokens,
                           COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                           COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                           COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                           COALESCE(SUM(actual_cost_usd), 0) AS actual_cost,
                           COUNT(*) AS sessions
                    FROM sessions
                    WHERE started_at > ?
                    GROUP BY day
                    ORDER BY day
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            by_model = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT model,
                           COALESCE(SUM(input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(output_tokens), 0) AS output_tokens,
                           COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                           COUNT(*) AS sessions
                    FROM sessions
                    WHERE started_at > ? AND model IS NOT NULL
                    GROUP BY model
                    ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            totals = conn.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                       COALESCE(SUM(actual_cost_usd), 0) AS actual_cost,
                       COUNT(*) AS total_sessions
                FROM sessions
                WHERE started_at > ?
                """,
                (cutoff,),
            ).fetchone()
            return {"daily": daily, "by_model": by_model, "totals": dict(totals) if totals else {}}

    def _cron_jobs_path(self) -> Path:
        return self.hermes_home / "cron" / "jobs.json"

    def _load_cron_jobs(self) -> list[dict[str, Any]]:
        path = self._cron_jobs_path()
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        jobs = raw.get("jobs", [])
        return jobs if isinstance(jobs, list) else []

    def _save_cron_jobs(self, jobs: list[dict[str, Any]]) -> None:
        path = self._cron_jobs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": jobs, "updated_at": _utc_now_iso()}
        fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".jobs_", suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _parse_schedule(self, schedule_text: str) -> dict[str, Any]:
        schedule = schedule_text.strip()
        lowered = schedule.lower()
        duration_match = re.fullmatch(r"(\\d+)\\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)", lowered)
        if lowered.startswith("every "):
            duration = lowered[6:].strip()
            interval_match = re.fullmatch(r"(\\d+)\\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)", duration)
            if not interval_match:
                raise HermesAdapterError("invalid_schedule", t("adapter.invalid_schedule", schedule=schedule_text))
            minutes = self._duration_to_minutes(int(interval_match.group(1)), interval_match.group(2))
            return {"kind": "interval", "minutes": minutes, "display": t("adapter.schedule_every_minutes", minutes=minutes)}
        if duration_match:
            minutes = self._duration_to_minutes(int(duration_match.group(1)), duration_match.group(2))
            run_at = _utc_now() + timedelta(minutes=minutes)
            return {"kind": "once", "run_at": run_at.isoformat(), "display": t("adapter.schedule_once_in", schedule=schedule_text)}
        if "T" in schedule or re.fullmatch(r"\\d{4}-\\d{2}-\\d{2}.*", schedule):
            dt = datetime.fromisoformat(schedule.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return {"kind": "once", "run_at": dt.isoformat(), "display": t("adapter.schedule_once_at", value=dt.strftime("%Y-%m-%d %H:%M"))}
        croniter(schedule, _utc_now())
        return {"kind": "cron", "expr": schedule, "display": schedule}

    @staticmethod
    def _duration_to_minutes(value: int, unit: str) -> int:
        token = unit[0]
        multipliers = {"m": 1, "h": 60, "d": 1440}
        return value * multipliers[token]

    def _compute_next_run(self, schedule: dict[str, Any]) -> str | None:
        kind = schedule.get("kind")
        now = _utc_now()
        if kind == "once":
            return str(schedule.get("run_at"))
        if kind == "interval":
            return (now + timedelta(minutes=int(schedule.get("minutes", 0)))).isoformat()
        if kind == "cron":
            return croniter(str(schedule.get("expr")), now).get_next(datetime).isoformat()
        return None

    def list_cron_jobs(self) -> list[dict[str, Any]]:
        bridge_value = self._run_bridge(
            """
            from cron.jobs import list_jobs
            return list_jobs(include_disabled=True)
            """,
            timeout=25.0,
        )
        if isinstance(bridge_value, list):
            return bridge_value
        return self._load_cron_jobs()

    def get_cron_job(self, job_id: str) -> dict[str, Any]:
        for job in self.list_cron_jobs():
            if job.get("id") == job_id:
                return job
        raise HermesAdapterError("cron_job_not_found", t("adapter.cron_job_not_found", job_id=job_id))

    def create_cron_job(
        self,
        *,
        prompt: str,
        schedule: str,
        name: str = "",
        deliver: str = "local",
        skills: list[str] | None = None,
    ) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            from cron.jobs import create_job
            return create_job(
                prompt=str(payload.get("prompt", "")),
                schedule=str(payload.get("schedule", "")),
                name=str(payload.get("name", "")),
                deliver=str(payload.get("deliver", "local")),
                skills=payload.get("skills") or None,
            )
            """,
            {"prompt": prompt, "schedule": schedule, "name": name, "deliver": deliver, "skills": skills or []},
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            return bridge_value

        parsed = self._parse_schedule(schedule)
        repeat = 1 if parsed["kind"] == "once" else None
        job = {
            "id": uuid4().hex[:12],
            "name": (name or prompt[:50] or t("adapter.default_cron_name")).strip(),
            "prompt": prompt,
            "skills": skills or [],
            "skill": (skills or [None])[0],
            "schedule": parsed,
            "schedule_display": parsed.get("display", schedule),
            "repeat": {"times": repeat, "completed": 0},
            "enabled": True,
            "state": "scheduled",
            "paused_at": None,
            "paused_reason": None,
            "created_at": _utc_now_iso(),
            "next_run_at": self._compute_next_run(parsed),
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "last_delivery_error": None,
            "deliver": deliver or "local",
            "origin": None,
        }
        jobs = self._load_cron_jobs()
        jobs.append(job)
        self._save_cron_jobs(jobs)
        return job

    def update_cron_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            from cron.jobs import update_job
            job = update_job(str(payload["job_id"]), payload.get("updates") or {})
            if not job:
                raise RuntimeError("cron_job_not_found")
            return job
            """,
            {"job_id": job_id, "updates": updates},
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            return bridge_value

        jobs = self._load_cron_jobs()
        for index, job in enumerate(jobs):
            if job.get("id") != job_id:
                continue
            next_job = {**job, **updates}
            if "schedule" in updates and isinstance(updates["schedule"], str):
                next_job["schedule"] = self._parse_schedule(updates["schedule"])
                next_job["schedule_display"] = next_job["schedule"].get("display", updates["schedule"])
                if next_job.get("state") != "paused":
                    next_job["next_run_at"] = self._compute_next_run(next_job["schedule"])
            jobs[index] = next_job
            self._save_cron_jobs(jobs)
            return next_job
        raise HermesAdapterError("cron_job_not_found", t("adapter.cron_job_not_found", job_id=job_id))

    def pause_cron_job(self, job_id: str) -> dict[str, Any]:
        return self.update_cron_job(
            job_id,
            {
                "enabled": False,
                "state": "paused",
                "paused_at": _utc_now_iso(),
                "paused_reason": t("adapter.paused_by_link"),
                "next_run_at": None,
            },
        )

    def resume_cron_job(self, job_id: str) -> dict[str, Any]:
        job = self.get_cron_job(job_id)
        return self.update_cron_job(
            job_id,
            {
                "enabled": True,
                "state": "scheduled",
                "paused_at": None,
                "paused_reason": None,
                "next_run_at": self._compute_next_run(job.get("schedule") or {}),
            },
        )

    def trigger_cron_job(self, job_id: str) -> dict[str, Any]:
        return self.update_cron_job(
            job_id,
            {
                "state": "scheduled",
                "next_run_at": _utc_now_iso(),
            },
        )

    def delete_cron_job(self, job_id: str) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            from cron.jobs import remove_job
            return {"ok": bool(remove_job(str(payload["job_id"])))}
            """,
            {"job_id": job_id},
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            return bridge_value

        jobs = [job for job in self._load_cron_jobs() if job.get("id") != job_id]
        self._save_cron_jobs(jobs)
        return {"ok": True, "job_id": job_id}

    def list_skills(self) -> list[dict[str, Any]]:
        bridge_value = self._run_bridge(
            """
            from hermes_cli.skills_config import get_disabled_skills
            from hermes_cli.config import load_config
            from tools.skills_tool import _find_all_skills

            config = load_config()
            disabled = get_disabled_skills(config)
            skills = _find_all_skills(skip_disabled=True)
            for skill in skills:
                skill["enabled"] = skill.get("name") not in disabled
            return skills
            """,
            timeout=25.0,
        )
        if isinstance(bridge_value, list):
            return bridge_value

        config = self.get_config()
        skills_cfg = config.get("skills") if isinstance(config.get("skills"), dict) else {}
        disabled = set(_normalize_name_list((skills_cfg or {}).get("disabled")))
        external_dirs = []
        for entry in _normalize_name_list((skills_cfg or {}).get("external_dirs")):
            candidate = Path(os.path.expandvars(os.path.expanduser(entry)))
            if candidate.is_dir():
                external_dirs.append(candidate)
        roots = [
            self.hermes_home / "skills",
            *external_dirs,
            self.hermes_home / "hermes-agent" / "skills",
            _reference_hermes_repo() / "skills",
        ]
        skills = []
        seen: set[str] = set()
        for root in roots:
            if not root.exists():
                continue
            for skill_file in root.rglob("SKILL.md"):
                name = skill_file.parent.name
                if name in seen:
                    continue
                seen.add(name)
                category = skill_file.parent.parent.name if skill_file.parent.parent != root else "uncategorized"
                text = skill_file.read_text(encoding="utf-8", errors="ignore")
                skills.append(
                    {
                        "name": name,
                        "category": category,
                        "description": _first_nonempty_line(text) or "",
                        "path": str(skill_file.parent),
                        "enabled": name not in disabled,
                    }
                )
        return sorted(skills, key=lambda item: (item["category"], item["name"]))

    def toggle_skill(self, name: str, *, enabled: bool) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills
            from hermes_cli.config import load_config

            config = load_config()
            disabled = get_disabled_skills(config)
            if payload.get("enabled"):
                disabled.discard(payload["name"])
            else:
                disabled.add(payload["name"])
            save_disabled_skills(config, disabled)
            return {"ok": True, "name": payload["name"], "enabled": bool(payload.get("enabled"))}
            """,
            {"name": name, "enabled": enabled},
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            return bridge_value

        config = self.get_config()
        config.setdefault("skills", {})
        skills_cfg = config["skills"]
        if not isinstance(skills_cfg, dict):
            skills_cfg = {}
            config["skills"] = skills_cfg
        disabled_list = set(_normalize_name_list(skills_cfg.get("disabled")))
        if enabled:
            disabled_list.discard(name)
        else:
            disabled_list.add(name)
        skills_cfg["disabled"] = sorted(disabled_list)
        self.save_config(config)
        return {"ok": True, "name": name, "enabled": enabled}

    def list_toolsets(self) -> list[dict[str, Any]]:
        bridge_value = self._run_bridge(
            """
            from hermes_cli.tools_config import (
                _get_effective_configurable_toolsets,
                _get_platform_tools,
                _toolset_has_keys,
            )
            from hermes_cli.config import load_config
            from toolsets import resolve_toolset

            config = load_config()
            enabled_toolsets = _get_platform_tools(config, "cli", include_default_mcp_servers=False)
            result = []
            for name, label, description in _get_effective_configurable_toolsets():
                try:
                    tools = sorted(set(resolve_toolset(name)))
                except Exception:
                    tools = []
                result.append(
                    {
                        "name": name,
                        "label": label,
                        "description": description,
                        "enabled": name in enabled_toolsets,
                        "available": True,
                        "configured": _toolset_has_keys(name, config),
                        "tools": tools,
                    }
                )
            return result
            """,
            timeout=25.0,
        )
        if isinstance(bridge_value, list):
            return bridge_value

        config = self.get_config()
        platform_toolsets = config.get("platform_toolsets") if isinstance(config.get("platform_toolsets"), dict) else {}
        enabled = set(_normalize_name_list(platform_toolsets.get("cli")) or _normalize_name_list(config.get("toolsets")))
        result = []
        for name, label, description in TOOLSET_CATALOG:
            result.append(
                {
                    "name": name,
                    "label": label,
                    "description": description,
                    "enabled": name in enabled,
                    "available": True,
                    "configured": True,
                    "tools": [],
                }
            )
        return result

    def list_profiles(self) -> dict[str, Any]:
        bridge_value = self._run_bridge(
            """
            from hermes_cli.profiles import get_active_profile, list_profiles

            active_profile = get_active_profile()
            profiles = []
            for profile in list_profiles():
                profiles.append(
                    {
                        "name": profile.name,
                        "path": str(profile.path),
                        "active": profile.name == active_profile,
                        "exists": bool(profile.path.exists()),
                        "is_default": bool(profile.is_default),
                        "gateway_running": bool(profile.gateway_running),
                        "skill_count": int(profile.skill_count),
                        "model": profile.model,
                        "provider": profile.provider,
                        "has_env": bool(profile.has_env),
                        "alias_path": str(profile.alias_path) if profile.alias_path else None,
                    }
                )
            return {"active_profile": active_profile, "profiles": profiles}
            """,
            timeout=25.0,
        )
        if isinstance(bridge_value, dict):
            return bridge_value

        root_home = self._profiles_root_home()
        profiles_root = root_home / "profiles"
        active_path = root_home / "active_profile"
        active_profile = active_path.read_text(encoding="utf-8").strip() if active_path.exists() else "default"
        default_model, default_provider = _parse_profile_model_info(root_home)
        profiles = [
            {
                "name": "default",
                "path": str(root_home),
                "active": active_profile in {"", "default"},
                "exists": True,
                "is_default": True,
                "gateway_running": _gateway_pid_is_running(root_home),
                "skill_count": _count_profile_skills(root_home),
                "model": default_model,
                "provider": default_provider,
                "has_env": (root_home / ".env").exists(),
                "alias_path": None,
            }
        ]
        if profiles_root.exists():
            for entry in sorted(profiles_root.iterdir()):
                if not entry.is_dir():
                    continue
                model, provider = _parse_profile_model_info(entry)
                alias_path = Path.home() / ".local" / "bin" / entry.name
                profiles.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "active": entry.name == active_profile,
                        "exists": True,
                        "is_default": False,
                        "gateway_running": _gateway_pid_is_running(entry),
                        "skill_count": _count_profile_skills(entry),
                        "model": model,
                        "provider": provider,
                        "has_env": (entry / ".env").exists(),
                        "alias_path": str(alias_path) if alias_path.exists() else None,
                    }
                )
        return {"active_profile": active_profile or "default", "profiles": profiles}

    def create_backup(self, output_path: str | None = None) -> dict[str, Any]:
        self._ensure_home()
        source = self.hermes_home
        if output_path:
            destination = Path(output_path).expanduser()
            if destination.is_dir():
                destination = destination / f"hermes-backup-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.zip"
        else:
            destination = Path.home() / f"hermes-backup-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.zip"
        if destination.suffix.lower() != ".zip":
            destination = destination.with_suffix(".zip")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination_resolved = destination.resolve()

        file_count = 0
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for dirpath, dirnames, filenames in os.walk(source):
                dirnames[:] = [item for item in dirnames if item not in {"hermes-agent", "__pycache__", ".git", "node_modules"}]
                base = Path(dirpath)
                for filename in filenames:
                    if filename in {"gateway.pid", "cron.pid"} or filename.endswith((".pyc", ".pyo")):
                        continue
                    absolute = base / filename
                    if absolute.resolve() == destination_resolved:
                        continue
                    relative = absolute.relative_to(source)
                    archive.write(absolute, arcname=str(relative))
                    file_count += 1

        return {
            "ok": True,
            "archive_path": str(destination.resolve()),
            "file_count": file_count,
            "size_bytes": destination.stat().st_size,
        }

    def restore_backup(self, archive_path: str, *, force: bool = False) -> dict[str, Any]:
        source_archive = Path(archive_path).expanduser()
        if not source_archive.exists():
            raise HermesAdapterError("backup_not_found", t("adapter.backup_not_found", archive_path=source_archive))
        root = self._ensure_home()
        if not force and any((root / candidate).exists() for candidate in ("config.yaml", ".env", "state.db")):
            raise HermesAdapterError("backup_restore_conflict", t("adapter.backup_restore_conflict"))
        with zipfile.ZipFile(source_archive, "r") as archive:
            for info in archive.infolist():
                member_name = info.filename
                target_path = _zip_member_path(root, member_name)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise HermesAdapterError(
                        "backup_restore_unsupported_member",
                        t("adapter.backup_restore_unsupported_member", member=member_name),
                    )
                if info.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source_handle, target_path.open("wb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle)
        return {"ok": True, "restored_to": str(root), "archive_path": str(source_archive.resolve())}
