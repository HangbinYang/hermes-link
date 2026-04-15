import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

from hermes_link.execution import HermesExecutionManager

EVENT_PREFIX = "__HERMES_LINK_EVENT__ "


class StubAdapter:
    def __init__(self):
        self.discovery = SimpleNamespace(hermes_home=None)

    def resolve_bridge_python(self):
        return sys.executable

    def build_bridge_env(self):
        return os.environ.copy()


class ScriptedExecutionManager(HermesExecutionManager):
    def __init__(self, script: str):
        super().__init__(StubAdapter())
        self.script = script

    def _spawn_worker(self, run):
        payload = {
            "run_id": run.run_id,
            "session_id": run.session_id,
            **run.request_payload,
        }
        return subprocess.Popen(
            [sys.executable, "-u", "-c", self.script, json.dumps(payload)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )


def test_execution_manager_completes_and_replays_history():
    script = f"""
import json, sys, time
payload = json.loads(sys.argv[1])
prefix = {EVENT_PREFIX!r}
def emit(event):
    print(prefix + json.dumps(event), flush=True)
emit({{"event":"run.started","run_id":payload["run_id"],"session_id":payload["session_id"],"timestamp":time.time()}})
emit({{"event":"message.delta","run_id":payload["run_id"],"session_id":payload["session_id"],"timestamp":time.time(),"delta":"Hello "}})
emit({{"event":"message.delta","run_id":payload["run_id"],"session_id":payload["session_id"],"timestamp":time.time(),"delta":"world"}})
emit({{"event":"run.completed","run_id":payload["run_id"],"session_id":payload["session_id"],"timestamp":time.time(),"output":"Hello world","usage":{{"total_tokens":2}}}})
"""
    manager = ScriptedExecutionManager(script)

    summary = manager.start_run({"input": "hello"})
    completed = manager.wait_for_terminal(summary["run_id"], timeout_seconds=5)

    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["final_output"] == "Hello world"
    assert completed["usage"]["total_tokens"] == 2

    subscription = manager.subscribe(summary["run_id"])
    events = []
    while True:
        event = subscription.get(timeout=1)
        if event is None:
            break
        events.append(event["event"])

    assert events == ["run.started", "message.delta", "message.delta", "run.completed"]


def test_execution_manager_can_cancel_a_running_worker():
    script = f"""
import json, signal, sys, time
payload = json.loads(sys.argv[1])
prefix = {EVENT_PREFIX!r}
def emit(event):
    print(prefix + json.dumps(event), flush=True)
def handle(signum, frame):
    emit({{"event":"run.cancelled","run_id":payload["run_id"],"session_id":payload["session_id"],"timestamp":time.time(),"reason":"cancelled"}})
    raise SystemExit(130)
signal.signal(signal.SIGTERM, handle)
signal.signal(signal.SIGINT, handle)
emit({{"event":"run.started","run_id":payload["run_id"],"session_id":payload["session_id"],"timestamp":time.time()}})
while True:
    time.sleep(0.1)
"""
    manager = ScriptedExecutionManager(script)

    summary = manager.start_run({"input": "hello"})
    deadline = time.time() + 2
    while time.time() < deadline:
        current = manager.get_run(summary["run_id"])
        if current["status"] == "running":
            break
        time.sleep(0.05)

    cancelled = manager.cancel_run(summary["run_id"])
    finished = manager.wait_for_terminal(summary["run_id"], timeout_seconds=5)

    assert cancelled["cancel_requested"] is True
    assert finished is not None
    assert finished["status"] == "cancelled"
    assert finished["cancel_reason"] == "cancelled"
