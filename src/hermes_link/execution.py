from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_link.hermes_adapter import HermesAdapter
from hermes_link.i18n import t
from hermes_link.runtime import generate_id
from hermes_link.models import utc_now_iso

_EVENT_PREFIX = "__HERMES_LINK_EVENT__ "
_COMPLETED_STATUSES = {"completed", "failed", "cancelled"}
_MAX_COMPLETED_RUNS = 50
_COMPLETED_RUN_TTL_SECONDS = 6 * 60 * 60

_WORKER_SCRIPT = r"""
import json
import os
import signal
import sys
import time
import traceback
from typing import Any

from gateway.run import GatewayRunner, _load_gateway_config, _resolve_gateway_model, _resolve_runtime_agent_kwargs
from hermes_cli.tools_config import _get_platform_tools
from hermes_state import SessionDB
from run_agent import AIAgent

payload = json.loads(sys.argv[1])
run_id = str(payload["run_id"])
session_id = str(payload["session_id"])
cancel_state = {"requested": False, "reason": "cancelled"}
agent_ref = {"agent": None}


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write("__HERMES_LINK_EVENT__ " + json.dumps(event, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _normalize_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    if value is None:
        return ""
    return str(value)


def _normalize_history(raw_history: Any) -> list[dict[str, str]]:
    normalized = []
    if not isinstance(raw_history, list):
        return normalized
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        content = _normalize_content(item.get("content"))
        normalized.append({"role": role, "content": content})
    return normalized


def _load_continued_history(db: SessionDB, run_payload: dict[str, Any]) -> list[dict[str, str]]:
    if not run_payload.get("continue_session"):
        return []
    if not run_payload.get("session_id"):
        return []
    try:
        return db.get_messages_as_conversation(str(run_payload["session_id"]))
    except Exception:
        return []


def _build_input(run_payload: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    raw_input = run_payload.get("input")
    if isinstance(raw_input, str):
        return raw_input, []
    if isinstance(raw_input, list):
        messages = []
        for item in raw_input:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if isinstance(item, dict):
                messages.append(
                    {
                        "role": str(item.get("role") or "user"),
                        "content": _normalize_content(item.get("content")),
                    }
                )
        if not messages:
            return "", []
        return messages[-1]["content"], messages[:-1]
    return "", []


def _handle_cancel(signum, frame):
    cancel_state["requested"] = True
    if signum == signal.SIGTERM:
        cancel_state["reason"] = "timeout" if cancel_state.get("reason") == "timeout" else "cancelled"
    agent = agent_ref.get("agent")
    if agent is not None:
        try:
            agent.interrupt("Hermes Link cancelled run")
        except Exception:
            pass


for _sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(_sig, _handle_cancel)


def _text_callback(delta):
    if delta is None:
        return
    _emit(
        {
            "event": "message.delta",
            "run_id": run_id,
            "session_id": session_id,
            "timestamp": time.time(),
            "delta": delta,
        }
    )


def _tool_callback(event_type, name, preview, args, **kwargs):
    ts = time.time()
    if event_type == "tool.started" and name and not str(name).startswith("_"):
        from agent.display import get_tool_emoji

        _emit(
            {
                "event": "tool.started",
                "run_id": run_id,
                "session_id": session_id,
                "timestamp": ts,
                "tool": name,
                "emoji": get_tool_emoji(name),
                "label": preview or name,
            }
        )
    elif event_type == "reasoning.available":
        _emit(
            {
                "event": "reasoning.available",
                "run_id": run_id,
                "session_id": session_id,
                "timestamp": ts,
                "text": preview or "",
            }
        )


def _main() -> int:
    runtime_kwargs = _resolve_runtime_agent_kwargs()
    model = _resolve_gateway_model()
    user_config = _load_gateway_config()
    enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))
    max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
    fallback_model = GatewayRunner._load_fallback_model()
    session_db = SessionDB()

    input_message, inline_history = _build_input(payload)
    explicit_history = _normalize_history(payload.get("conversation_history"))
    continued_history = _load_continued_history(session_db, payload)
    conversation_history = explicit_history or continued_history or inline_history

    if not input_message:
        raise RuntimeError("No user message found in input")

    agent = AIAgent(
        model=model,
        **runtime_kwargs,
        max_iterations=max_iterations,
        quiet_mode=True,
        verbose_logging=False,
        ephemeral_system_prompt=payload.get("instructions") or None,
        enabled_toolsets=enabled_toolsets,
        session_id=session_id,
        platform="api_server",
        stream_delta_callback=_text_callback,
        tool_progress_callback=_tool_callback,
        session_db=session_db,
        fallback_model=fallback_model,
    )
    agent_ref["agent"] = agent

    _emit(
        {
            "event": "run.started",
            "run_id": run_id,
            "session_id": session_id,
            "timestamp": time.time(),
            "continue_session": bool(payload.get("continue_session")),
        }
    )
    result = agent.run_conversation(
        user_message=input_message,
        conversation_history=conversation_history,
        task_id="default",
    )
    usage = {
        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
    }
    final_response = ""
    if isinstance(result, dict):
        final_response = str(result.get("final_response") or result.get("error") or "")

    if cancel_state["requested"]:
        _emit(
            {
                "event": "run.cancelled",
                "run_id": run_id,
                "session_id": session_id,
                "timestamp": time.time(),
                "reason": cancel_state["reason"],
                "output": final_response,
                "usage": usage,
            }
        )
        return 130

    _emit(
        {
            "event": "run.completed",
            "run_id": run_id,
            "session_id": session_id,
            "timestamp": time.time(),
            "output": final_response,
            "usage": usage,
        }
    )
    return 0


try:
    exit_code = _main()
except Exception as exc:
    event_name = "run.cancelled" if cancel_state["requested"] else "run.failed"
    event = {
        "event": event_name,
        "run_id": run_id,
        "session_id": session_id,
        "timestamp": time.time(),
        "error": str(exc),
    }
    if cancel_state["requested"]:
        event["reason"] = cancel_state["reason"]
    else:
        event["traceback"] = traceback.format_exc()
    _emit(event)
    exit_code = 130 if cancel_state["requested"] else 1

raise SystemExit(exit_code)
"""


class ExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ManagedRun:
    run_id: str
    session_id: str
    request_payload: dict[str, Any]
    created_at: str
    updated_at: str
    status: str = "starting"
    final_output: str | None = None
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    event_history: list[dict[str, Any]] = field(default_factory=list)
    output_parts: list[str] = field(default_factory=list)
    subscribers: set[queue.Queue] = field(default_factory=set)
    process: subprocess.Popen[str] | None = None
    condition: threading.Condition = field(default_factory=threading.Condition)
    event_counter: int = 0
    cancel_requested: bool = False
    cancel_reason: str | None = None
    worker_logs: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    stdout_finished: bool = False
    stderr_finished: bool = False
    terminal_emitted: bool = False
    completed_at: float | None = None

    def to_summary(self) -> dict[str, Any]:
        raw_input = self.request_payload.get("input")
        if isinstance(raw_input, str):
            preview = raw_input
        elif isinstance(raw_input, list) and raw_input:
            last = raw_input[-1]
            if isinstance(last, str):
                preview = last
            elif isinstance(last, dict):
                preview = str(last.get("content") or "")
            else:
                preview = ""
        else:
            preview = ""
        preview = preview.strip().replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "input_preview": preview,
            "continue_session": bool(self.request_payload.get("continue_session")),
            "event_count": len(self.event_history),
            "final_output": self.final_output,
            "error": self.error,
            "usage": self.usage,
            "cancel_requested": self.cancel_requested,
            "cancel_reason": self.cancel_reason,
        }


class HermesExecutionManager:
    def __init__(self, adapter: HermesAdapter):
        self.adapter = adapter
        self._lock = threading.RLock()
        self._runs: dict[str, ManagedRun] = {}

    def list_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            runs = sorted(self._runs.values(), key=lambda item: item.created_at, reverse=True)
            return [run.to_summary() for run in runs[:limit]]

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        return run.to_summary()

    def start_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._prune_runs()
        run_id = generate_id("run_")
        session_id = str(payload.get("session_id") or generate_id("sess_"))
        request_payload = dict(payload)
        request_payload["session_id"] = session_id

        run = ManagedRun(
            run_id=run_id,
            session_id=session_id,
            request_payload=request_payload,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        process = self._spawn_worker(run)
        run.process = process

        with self._lock:
            self._runs[run_id] = run

        threading.Thread(target=self._consume_stdout, args=(run,), daemon=True).start()
        threading.Thread(target=self._consume_stderr, args=(run,), daemon=True).start()
        threading.Thread(target=self._watch_process, args=(run,), daemon=True).start()

        timeout_seconds = request_payload.get("timeout_seconds")
        if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0:
            threading.Thread(target=self._watch_timeout, args=(run, float(timeout_seconds)), daemon=True).start()

        return run.to_summary()

    def wait_for_terminal(self, run_id: str, *, timeout_seconds: float | None = None) -> dict[str, Any] | None:
        run = self._require_run(run_id)
        deadline = time.time() + timeout_seconds if timeout_seconds else None
        with run.condition:
            while run.status not in _COMPLETED_STATUSES:
                if deadline is None:
                    run.condition.wait(timeout=0.5)
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                run.condition.wait(timeout=min(0.5, remaining))
        return run.to_summary()

    def retry_run(self, run_id: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        run = self._require_run(run_id)
        payload = dict(run.request_payload)
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        return self.start_run(payload)

    def cancel_run(self, run_id: str, *, reason: str = "cancelled") -> dict[str, Any]:
        run = self._require_run(run_id)
        if run.status in _COMPLETED_STATUSES:
            raise ExecutionError("run_not_active", t("execution.run_not_active"))

        run.cancel_requested = True
        run.cancel_reason = reason
        process = run.process
        if process is None:
            raise ExecutionError("run_not_active", t("execution.run_not_active"))

        try:
            if sys.platform == "win32":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except Exception:
            try:
                process.terminate()
            except Exception as exc:
                raise ExecutionError("run_cancel_failed", t("execution.run_cancel_failed")) from exc
        return run.to_summary()

    def subscribe(self, run_id: str) -> queue.Queue:
        run = self._require_run(run_id)
        subscription: queue.Queue = queue.Queue()
        with self._lock:
            for event in run.event_history:
                subscription.put(event)
            if run.status in _COMPLETED_STATUSES:
                subscription.put(None)
            else:
                run.subscribers.add(subscription)
        return subscription

    def unsubscribe(self, run_id: str, subscription: queue.Queue) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.subscribers.discard(subscription)

    def _require_run(self, run_id: str) -> ManagedRun:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise ExecutionError("run_not_found", t("execution.run_not_found", run_id=run_id))
        return run

    def _spawn_worker(self, run: ManagedRun) -> subprocess.Popen[str]:
        python_exec = self.adapter.resolve_bridge_python()
        if not python_exec:
            raise ExecutionError("execution_unavailable", t("execution.execution_unavailable"))

        env = self.adapter.build_bridge_env()
        env["PYTHONUNBUFFERED"] = "1"
        payload = {
            "run_id": run.run_id,
            "session_id": run.session_id,
            **run.request_payload,
        }
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "bufsize": 1,
            "env": env,
        }
        if self.adapter.discovery.hermes_home:
            kwargs["cwd"] = str(Path(self.adapter.discovery.hermes_home).expanduser())
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(
            [python_exec, "-u", "-c", _WORKER_SCRIPT, json.dumps(payload, ensure_ascii=False)],
            **kwargs,
        )

    def _consume_stdout(self, run: ManagedRun) -> None:
        assert run.process is not None
        stream = run.process.stdout
        if stream is None:
            run.stdout_finished = True
            return
        for raw_line in stream:
            line = raw_line.rstrip("\r\n")
            if not line:
                continue
            if not line.startswith(_EVENT_PREFIX):
                run.worker_logs.append(line)
                continue
            payload = line[len(_EVENT_PREFIX) :]
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                run.worker_logs.append(line)
                continue
            self._record_event(run, event)
        run.stdout_finished = True

    def _consume_stderr(self, run: ManagedRun) -> None:
        assert run.process is not None
        stream = run.process.stderr
        if stream is None:
            run.stderr_finished = True
            return
        for raw_line in stream:
            line = raw_line.rstrip("\r\n")
            if line:
                run.worker_logs.append(line)
        run.stderr_finished = True

    def _watch_timeout(self, run: ManagedRun, timeout_seconds: float) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if run.status in _COMPLETED_STATUSES:
                return
            time.sleep(0.2)
        if run.status in _COMPLETED_STATUSES:
            return
        run.cancel_reason = "timeout"
        run.cancel_requested = True
        try:
            self.cancel_run(run.run_id, reason="timeout")
        except ExecutionError:
            pass

    def _watch_process(self, run: ManagedRun) -> None:
        assert run.process is not None
        return_code = run.process.wait()
        time.sleep(0.1)
        if run.status not in _COMPLETED_STATUSES:
            if run.cancel_requested or return_code == 130:
                event = {
                    "event": "run.cancelled",
                    "run_id": run.run_id,
                    "session_id": run.session_id,
                    "timestamp": time.time(),
                    "reason": run.cancel_reason or "cancelled",
                    "output": "".join(run.output_parts) or run.final_output or "",
                    "usage": run.usage,
                }
            else:
                error = "\n".join(run.worker_logs).strip() or t("execution.run_failed_without_detail")
                event = {
                    "event": "run.failed",
                    "run_id": run.run_id,
                    "session_id": run.session_id,
                    "timestamp": time.time(),
                    "error": error,
                }
            self._record_event(run, event)
        with self._lock:
            subscribers = list(run.subscribers)
            run.subscribers.clear()
        for subscriber in subscribers:
            subscriber.put(None)

    def _record_event(self, run: ManagedRun, event: dict[str, Any]) -> None:
        with self._lock:
            run.event_counter += 1
            enriched = {
                **event,
                "sequence": run.event_counter,
                "run_id": event.get("run_id") or run.run_id,
                "session_id": event.get("session_id") or run.session_id,
            }
            run.event_history.append(enriched)
            run.updated_at = utc_now_iso()

            event_name = str(enriched.get("event") or "")
            if event_name == "run.started":
                run.status = "running"
            elif event_name == "message.delta":
                delta = str(enriched.get("delta") or "")
                if delta:
                    run.output_parts.append(delta)
            elif event_name == "run.completed":
                run.status = "completed"
                run.final_output = str(enriched.get("output") or "".join(run.output_parts))
                run.usage = dict(enriched.get("usage") or {})
                run.terminal_emitted = True
                run.completed_at = time.time()
            elif event_name == "run.failed":
                run.status = "failed"
                run.error = str(enriched.get("error") or t("execution.run_failed_without_detail"))
                run.terminal_emitted = True
                run.completed_at = time.time()
            elif event_name == "run.cancelled":
                run.status = "cancelled"
                run.cancel_requested = True
                reason = str(enriched.get("reason") or "")
                if run.cancel_reason and (not reason or reason == "cancelled"):
                    reason = run.cancel_reason
                run.cancel_reason = reason or "cancelled"
                run.final_output = str(enriched.get("output") or "".join(run.output_parts))
                run.usage = dict(enriched.get("usage") or run.usage)
                run.terminal_emitted = True
                run.completed_at = time.time()
            subscribers = list(run.subscribers)

        with run.condition:
            run.condition.notify_all()

        for subscriber in subscribers:
            subscriber.put(enriched)

    def _prune_runs(self) -> None:
        with self._lock:
            completed = [
                run
                for run in self._runs.values()
                if run.status in _COMPLETED_STATUSES and run.completed_at is not None
            ]
            now = time.time()
            expired = [
                run.run_id
                for run in completed
                if run.completed_at is not None and now - run.completed_at > _COMPLETED_RUN_TTL_SECONDS
            ]
            for run_id in expired:
                self._runs.pop(run_id, None)

            completed = sorted(
                (
                    run
                    for run in self._runs.values()
                    if run.status in _COMPLETED_STATUSES and run.completed_at is not None
                ),
                key=lambda item: item.completed_at or 0,
                reverse=True,
            )
            for run in completed[_MAX_COMPLETED_RUNS:]:
                self._runs.pop(run.run_id, None)
