import json
import os
import signal
import subprocess
import threading
from pathlib import Path

from loopi.sandbox import claude_script, command
from loopi.util import LoopiError

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
TASK = "Execute a tarefa descrita na entrada padrão."


def load_prompt(name):
    return (PROMPTS_DIR / f"{name}.md").read_text()


class Watchdog:
    def __init__(self, limits):
        self.limits = limits
        self.last_call = None
        self.streak = 0
        self.turns_without_edit = 0
        self.result = None

    def observe(self, event):
        kind = event.get("type")
        if kind == "result":
            self.result = event
            return None
        if kind != "assistant" or event.get("parent_tool_use_id"):
            return None
        message = event.get("message") or {}
        usage = message.get("usage") or {}
        context = sum(
            usage.get(k) or 0
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        if context >= self.limits["context_tokens"]:
            return "context"
        calls = [b for b in message.get("content") or [] if b.get("type") == "tool_use"]
        if not calls:
            return None
        if any(b.get("name") in EDIT_TOOLS for b in calls):
            self.turns_without_edit = 0
        else:
            self.turns_without_edit += 1
            if self.turns_without_edit >= self.limits["turns_without_edit"]:
                return "no_progress"
        for call in calls:
            signature = json.dumps([call.get("name"), call.get("input")], sort_keys=True)
            self.streak = self.streak + 1 if signature == self.last_call else 1
            self.last_call = signature
            if self.streak >= self.limits["repeated_calls"]:
                return "repetition"
        return None


class Session:
    def __init__(self):
        self.stop_reason = None
        self.cost = 0.0
        self.exit_code = None
        self.result = None
        self.structured = None
        self.stderr = ""


def claude_argv(ctx, role, system_prompt, budget, tools=None, schema=None):
    argv = [
        ctx.cfg["claude"],
        "-p",
        TASK,
        "--setting-sources",
        "project",
        "--permission-prompts",
        "none",
        "--append-system-prompt",
        system_prompt,
        "--max-budget-usd",
        str(budget),
    ]
    model = ctx.cfg["models"].get(role)
    if model:
        argv += ["--model", model]
    if tools is not None:
        argv += ["--tools", ",".join(tools), "--permission-mode", "dontAsk"]
        if tools:
            argv += ["--allowedTools", ",".join(tools)]
    else:
        argv += ["--permission-mode", ctx.permission_mode()]
    if schema:
        argv += ["--json-schema", json.dumps(schema)]
    if not ctx.uses_container():
        deny = [f"Read(/{ctx.root}/**)", f"Edit(/{ctx.root}/**)"]
        argv += ["--settings", json.dumps({"permissions": {"deny": deny}})]
    argv += ["--output-format", "stream-json", "--verbose"]
    return argv


def _stop(proc):
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _flag(session, reason, proc):
    if not session.stop_reason:
        session.stop_reason = reason
    _stop(proc)


def _json_in_text(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def run(ctx, role, system_prompt, stdin_text, workspace, log_path, budget, isolated=True,
        tools=None, schema=None, setup="", minutes=None, mounts=()):
    argv = claude_argv(ctx, role, system_prompt, budget, tools, schema)
    if isolated:
        cmd, cwd, env = command(ctx, claude_script(argv, setup), workspace, mounts, interactive=True)
    else:
        cmd, cwd, env = argv, str(workspace), dict(os.environ)
    watchdog = Watchdog(ctx.cfg["limits"])
    session = Session()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    stderr_lines = []
    stderr_reader = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
    stderr_reader.start()
    try:
        proc.stdin.write(stdin_text)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    timer = threading.Timer(
        60 * (minutes or ctx.cfg["limits"]["session_minutes"]),
        lambda: _flag(session, "timeout", proc),
    )
    timer.daemon = True
    timer.start()
    with log_path.open("w") as log:
        for line in proc.stdout:
            log.write(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            reason = watchdog.observe(event)
            if reason and not session.stop_reason:
                session.stop_reason = reason
                _stop(proc)
    proc.wait()
    timer.cancel()
    stderr_reader.join(timeout=5)
    session.exit_code = proc.returncode
    session.stderr = "".join(stderr_lines)[-4000:]
    if watchdog.result:
        session.result = watchdog.result
        session.cost = float(watchdog.result.get("total_cost_usd") or 0)
        session.structured = watchdog.result.get("structured_output")
        if session.structured is None and schema:
            session.structured = _json_in_text(watchdog.result.get("result") or "")
        if watchdog.result.get("is_error") and not session.stop_reason:
            session.stop_reason = "agent_error:" + str(watchdog.result.get("subtype"))
    elif not session.stop_reason and proc.returncode != 0:
        session.stop_reason = "process_failure"
    return session


def structured(ctx, role, system_prompt, stdin_text, workspace, log_path, schema, tools,
               isolated=False, setup="", minutes=20):
    session = run(
        ctx, role, system_prompt, stdin_text, workspace, log_path, ctx.cfg["budget"]["structured_usd"],
        isolated=isolated, tools=tools, schema=schema, setup=setup, minutes=minutes,
    )
    if session.structured is None:
        detail = session.stop_reason or "sem resultado"
        raise LoopiError(f"{role}: sem saída estruturada ({detail}) {session.stderr[-500:]}")
    return session
