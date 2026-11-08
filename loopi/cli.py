import argparse
import json
import shutil
import sys

from loopi import PACKAGE_DIR, VERSION, planner, state
from loopi.config import DEFAULTS, locate
from loopi.factory import Factory
from loopi.util import LoopiError, git, write_json

GITIGNORE = "state/\n"


def cmd_init(args):
    ctx = locate()
    if ctx.initialized and not args.force:
        raise LoopiError(".loopi/loopi.json já existe (use --force para recriar)")
    for directory in (ctx.scenarios_dir, ctx.twins_dir, ctx.visual_dir):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").touch()
    (ctx.factory_dir / ".gitignore").write_text(GITIGNORE)
    cfg = {
        "version": 1,
        "branch": git(ctx.root, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() or "main",
        "isolation": "container" if (shutil.which("podman") or shutil.which("docker")) else "host",
        "commands": dict(DEFAULTS["commands"]),
    }
    if cfg["branch"] == "HEAD":
        cfg["branch"] = "main"
    packages = []
    has_commit = git(ctx.root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0
    if has_commit and not args.no_agent:
        print("analisando o repositório...")
        detected = planner.detect_commands(ctx)
        for key in ("setup", "quick", "full", "security"):
            cfg["commands"][key] = detected.get(key, "")
        packages = detected.get("container_packages") or []
    write_json(ctx.config_file, cfg)
    template = (PACKAGE_DIR / "templates" / "Containerfile").read_text()
    if packages:
        template = template.replace('ARG PACKAGES=""', f'ARG PACKAGES="{" ".join(packages)}"')
    if not ctx.containerfile.exists() or args.force:
        ctx.containerfile.write_text(template)
    print(f"fábrica criada em {ctx.factory_dir.relative_to(ctx.root)}/")
    print(json.dumps(cfg["commands"], ensure_ascii=False, indent=2))
    return 0


def require(ctx):
    if not ctx.initialized:
        raise LoopiError("rode 'loopi init' primeiro")
    return ctx


def cmd_plan(args):
    ctx = require(locate())
    issues = planner.plan(ctx, args.spec, replace=args.replace)
    for issue in issues:
        deps = f" (depois de {', '.join(issue['depends_on'])})" if issue["depends_on"] else ""
        print(f"{issue['id']}  {issue['title']}{deps}")
    return 0


def cmd_run(args):
    ctx = require(locate())
    return Factory(ctx).run(continuous=args.continuous, once=args.once, interval=args.interval)


def cmd_status(args):
    ctx = require(locate())
    factory = Factory(ctx, out=lambda *_: None)
    factory.sync_reviews()
    st = factory.state
    issues = st.issues()
    if args.json:
        print(json.dumps(issues, ensure_ascii=False, indent=2))
        return 0
    counts = {}
    for issue in issues:
        counts[issue["status"]] = counts.get(issue["status"], 0) + 1
    events = list(st.events())
    merged = sum(1 for e in events if e["event"] == "merged")
    reviewed = sum(1 for e in events if e["event"] == "review_merged")
    rollbacks = sum(1 for e in events if e["event"] == "rollback")
    gaps = sum(1 for e in events if e["event"] == "gap")
    approvals = sum(1 for e in events if e["event"] == "approved")
    autonomous = f"{100 * merged / (merged + reviewed):.0f}%" if merged + reviewed else "-"
    print("issues: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nenhuma"))
    print(f"custo hoje: US$ {st.cost_today():.2f}")
    print(f"integradas sem humano: {autonomous} | rollbacks: {rollbacks} | "
          f"lacunas testes×cenários: {gaps}/{gaps + approvals}")
    if st.stop_file.exists():
        print(f"PARADA: {st.stop_file.read_text().strip() or 'manual'}")
    pending_human = [i for i in issues if i["status"] in state.ESCALATED]
    if pending_human:
        print("\nprecisam de você:")
        for issue in pending_human:
            extra = (issue.get("review") or {}).get("pr") or ""
            print(f"  {issue['id']} [{issue['status']}] {issue['title']}")
            if issue.get("reason"):
                print(f"      {issue['reason']}")
            if extra:
                print(f"      {extra}")
    return 0


def _change_status(issue_id, allowed, new_status, reset=False):
    ctx = require(locate())
    st = state.State(ctx)
    issue = st.get(issue_id)
    if not issue:
        raise LoopiError(f"issue {issue_id} não existe")
    if issue["status"] not in allowed:
        raise LoopiError(f"issue {issue_id} está {issue['status']}")
    issue["status"] = new_status
    issue["reason"] = None
    if reset:
        issue["attempts"] = 0
        issue["tickets"] = []
    st.save(issue)
    st.log("status_changed", issue=issue_id, status=new_status)
    print(f"{issue_id} → {new_status}")
    return 0


def cmd_retry(args):
    allowed = (state.BLOCKED, state.ABORTED, state.PROPOSED, state.DISCARDED, state.IN_REVIEW)
    return _change_status(args.id, allowed, state.PENDING, reset=True)


def cmd_discard(args):
    allowed = tuple(s for s in (state.PENDING, *state.ESCALATED))
    return _change_status(args.id, allowed, state.DISCARDED)


def cmd_stop(args):
    ctx = require(locate())
    st = state.State(ctx)
    st.stop_file.parent.mkdir(parents=True, exist_ok=True)
    st.stop_file.write_text("manual\n")
    print("parada registrada")
    return 0


def cmd_resume(args):
    ctx = require(locate())
    state.State(ctx).stop_file.unlink(missing_ok=True)
    print("parada removida")
    return 0


def cmd_improve(args):
    ctx = require(locate())
    for issue in planner.improve(ctx):
        print(f"{issue['id']}  {issue['title']}")
    return 0


def cmd_version(args):
    print(VERSION)
    return 0


def parser():
    p = argparse.ArgumentParser(prog="loopi", description="Fábrica de software autônoma com Claude Code.")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="cria a fábrica (.loopi/) no repositório atual")
    s.add_argument("--force", action="store_true", help="recria a configuração")
    s.add_argument("--no-agent", action="store_true", help="não usa o Claude para detectar comandos")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("plan", help="quebra uma spec em issues")
    s.add_argument("spec", help="caminho da spec, ex.: docs/specs/001-login.md")
    s.add_argument("--replace", action="store_true", help="descarta issues abertas da spec e replaneja")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("run", help="executa a fila de issues")
    s.add_argument("--continuous", action="store_true", help="não para quando a fila esvazia")
    s.add_argument("--once", action="store_true", help="processa uma issue e sai")
    s.add_argument("--interval", type=int, default=300, help="segundos entre ciclos no modo contínuo")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="mostra o painel da fábrica")
    s.add_argument("--json", action="store_true", help="saída em JSON")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("retry", help="devolve uma issue para a fila")
    s.add_argument("id")
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("discard", help="descarta uma issue")
    s.add_argument("id")
    s.set_defaults(func=cmd_discard)

    sub.add_parser("stop", help="pausa a fábrica").set_defaults(func=cmd_stop)
    sub.add_parser("resume", help="retoma a fábrica").set_defaults(func=cmd_resume)
    sub.add_parser("improve", help="inspeciona o app e propõe issues").set_defaults(func=cmd_improve)
    sub.add_parser("version", help="mostra a versão").set_defaults(func=cmd_version)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except LoopiError as e:
        print(f"loopi: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
