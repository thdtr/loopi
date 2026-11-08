import json
import re
import shutil
from pathlib import Path

from loopi import agent, sandbox, state
from loopi.util import LoopiError

ISSUES_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "criteria": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "command": {"type": "string"},
                            },
                            "required": ["description", "command"],
                        },
                    },
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["key", "title", "description", "criteria", "depends_on", "paths"],
            },
        }
    },
    "required": ["issues"],
}

COMMANDS_SCHEMA = {
    "type": "object",
    "properties": {
        "setup": {"type": "string"},
        "quick": {"type": "string"},
        "full": {"type": "string"},
        "security": {"type": "string"},
        "container_packages": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["setup", "quick", "full", "security", "container_packages"],
}

READ_TOOLS = ["Read", "Grep", "Glob"]


def spec_prefix(spec_path):
    stem = Path(spec_path).stem
    return re.sub(r"[^a-z0-9-]+", "-", stem.lower()).strip("-") or "spec"


def _store(st, items, prefix, origin, status, spec=None):
    ids = {}
    for item in items:
        ids[item["key"]] = st.next_id(prefix)
        st.create(ids[item["key"]], spec=spec, title=item["title"])
    created = []
    for item in items:
        issue = st.get(ids[item["key"]])
        issue.update(
            description=item["description"],
            criteria=item["criteria"],
            depends_on=[ids[d] for d in item.get("depends_on", []) if d in ids],
            paths=item.get("paths") or [],
            origin=origin,
            status=status,
        )
        st.save(issue)
        created.append(issue)
    return created


def plan(ctx, spec_path, replace=False):
    spec_file = (ctx.root / spec_path).resolve()
    if not spec_file.exists():
        raise LoopiError(f"spec não encontrada: {spec_path}")
    rel = spec_file.relative_to(ctx.root).as_posix()
    if rel.startswith(".loopi/"):
        raise LoopiError("a spec deve ficar fora de .loopi/")
    st = state.State(ctx)
    st.ensure()
    existing = [i for i in st.issues() if i.get("spec") == rel and i["status"] != state.DISCARDED]
    if existing and not replace:
        raise LoopiError(f"{rel} já tem issues; use --replace para replanejar")
    for issue in existing:
        if issue["status"] in (state.PENDING, state.BLOCKED, state.ABORTED, state.PROPOSED):
            issue["status"] = state.DISCARDED
            st.save(issue)
    workspace = ctx.cache_dir / "workspaces" / f"plan-{spec_prefix(rel)}"
    sandbox.export(ctx, "HEAD", workspace)
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec_file, target)
    done = [
        {k: i.get(k) for k in ("id", "title", "status")}
        for i in existing
        if i["status"] in (state.DONE, state.IN_REVIEW)
    ]
    commands = {k: v for k, v in ctx.cfg["commands"].items() if v and k in ("setup", "quick", "full")}
    stdin_text = f"# Spec: {rel}\n\n{spec_file.read_text()}\n"
    stdin_text += "\n# Comandos configurados da fábrica\n" + json.dumps(commands, ensure_ascii=False, indent=2) + "\n"
    if done:
        stdin_text += "\n# Issues já entregues desta spec\n" + json.dumps(done, ensure_ascii=False, indent=2)
    try:
        session = agent.structured(
            ctx, "planner", agent.load_prompt("planner"), stdin_text, workspace,
            st.session_log(spec_prefix(rel), 1, "planner"), ISSUES_SCHEMA, READ_TOOLS, minutes=30,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    st.log("planned", spec=rel, cost_usd=session.cost)
    return _store(st, session.structured["issues"], spec_prefix(rel), "planner", state.PENDING, rel)


def improve(ctx):
    st = state.State(ctx)
    st.ensure()
    sandbox.require_auth(ctx)
    workspace = ctx.cache_dir / "workspaces" / "improve"
    sandbox.export(ctx, ctx.cfg["branch"], workspace)
    open_issues = [
        {k: i.get(k) for k in ("id", "title", "status")}
        for i in st.issues()
        if i["status"] not in (state.DONE, state.DISCARDED)
    ]
    stdin_text = "# Issues abertas\n" + json.dumps(open_issues, ensure_ascii=False, indent=2) + "\n"
    try:
        session = agent.structured(
            ctx, "improver", agent.load_prompt("improver"), stdin_text, workspace,
            st.session_log("improve", 1, "improver"), ISSUES_SCHEMA, None, isolated=True,
            setup=ctx.cfg["commands"].get("setup", ""), minutes=45,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    st.log("improved", cost_usd=session.cost)
    return _store(st, session.structured["issues"], "improve", "improver", state.PROPOSED)


def detect_commands(ctx):
    workspace = ctx.cache_dir / "workspaces" / "init"
    sandbox.export(ctx, "HEAD", workspace)
    st = state.State(ctx)
    try:
        session = agent.structured(
            ctx, "founder", agent.load_prompt("founder"), "# Repositório no diretório atual\n",
            workspace, st.session_log("init", 1, "founder"), COMMANDS_SCHEMA, READ_TOOLS, minutes=15,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return session.structured
