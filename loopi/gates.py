import re
import shlex
import shutil
import subprocess
from pathlib import Path

from loopi.sandbox import command
from loopi.util import git, matches, tail

SECRET_PATTERNS = [
    r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bghp_[A-Za-z0-9]{36}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{60,}",
    r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    r"\bsk_live_[A-Za-z0-9]{20,}",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    r"\bAIza[0-9A-Za-z_-]{35}\b",
]


ENVIRONMENT_PATTERNS = [
    r"command not found",
    r"No module named",
    r"not found: ",
    r"ENOENT.*spawn",
    r"Cannot find module",
    r"executable file not found",
]


def is_environment(failure):
    if failure.hidden or not failure.output:
        return False
    if failure.reason.endswith("código 127"):
        return True
    return any(re.search(p, tail(failure.output, 20)) for p in ENVIRONMENT_PATTERNS)


class Failure:
    def __init__(self, gate, reason, output="", cmd="", severe=False, hidden=False):
        self.gate = gate
        self.reason = reason
        self.output = output
        self.cmd = cmd
        self.severe = severe
        self.hidden = hidden

    def ticket(self):
        return {
            "gate": self.gate,
            "reason": self.reason,
            "command": None if self.hidden else self.cmd,
            "output": "" if self.hidden else tail(self.output, 60),
        }


def changed_files(worktree, base):
    out = git(worktree, "diff", "--name-status", "--no-renames", f"{base}..HEAD").stdout
    items = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            items.append((parts[0][0], parts[-1]))
    return items


def added_lines(worktree, base):
    current, out = None, {}
    diff = git(worktree, "diff", "-U0", "--no-renames", f"{base}..HEAD").stdout
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("+") and current:
            out.setdefault(current, []).append(line[1:])
    return out


def _file_at(worktree, rev, path):
    result = git(worktree, "show", f"{rev}:{path}", check=False)
    return result.stdout if result.returncode == 0 else ""


def integrity(ctx, issue, worktree, base):
    cfg = ctx.cfg
    test_globs = cfg["tests"]["files"]
    asserts = re.compile(cfg["tests"]["asserts"])
    skips = re.compile(cfg["tests"]["skips"])
    changes = changed_files(worktree, base)
    added = added_lines(worktree, base)
    for status, path in changes:
        if status == "D" and matches(path, test_globs):
            return Failure("integrity", f"teste removido: {path}", severe=True)
    balance = 0
    for status, path in changes:
        if not matches(path, test_globs):
            continue
        before = len(asserts.findall(_file_at(worktree, base, path)))
        after = len(asserts.findall(_file_at(worktree, "HEAD", path))) if status != "D" else 0
        balance += after - before
    if balance < 0:
        return Failure("integrity", f"asserções reduzidas em {-balance}", severe=True)
    for path, lines in added.items():
        if matches(path, test_globs) and any(skips.search(line) for line in lines):
            return Failure("integrity", f"teste pulado ou focado: {path}", severe=True)
    scope = list(issue.get("paths") or [])
    if scope:
        allowed = scope + cfg["free_scope"] + test_globs
        outside = [path for _, path in changes if not matches(path, allowed)]
        if outside:
            return Failure("scope", "arquivos fora do escopo da issue: " + ", ".join(outside[:20]))
    for path, lines in added.items():
        for line in lines:
            if any(re.search(p, line) for p in SECRET_PATTERNS):
                return Failure("secrets", f"possível segredo em {path}")
    if shutil.which("gitleaks"):
        text = "\n".join(line for lines in added.values() for line in lines)
        scan = subprocess.run(
            ["gitleaks", "stdin", "--no-banner", "--redact", "--exit-code", "1"],
            input=text, capture_output=True, text=True,
        )
        if scan.returncode == 1:
            return Failure("secrets", "gitleaks encontrou segredo", scan.stdout + scan.stderr)
    return None


def sensitive_paths(ctx, worktree, base):
    return [p for _, p in changed_files(worktree, base) if matches(p, ctx.cfg["sensitive_paths"])]


def scenarios(ctx):
    if not ctx.scenarios_dir.exists():
        return []
    found = []
    for path in sorted(ctx.scenarios_dir.rglob("*")):
        if path.is_file() and not path.name.startswith(".") and path.stat().st_mode & 0o111:
            found.append(path.relative_to(ctx.scenarios_dir).as_posix())
    return found


def steps(ctx, issue):
    commands = ctx.cfg["commands"]
    plan = []
    if commands.get("setup"):
        plan.append(("setup", commands["setup"], False))
    if commands.get("quick"):
        plan.append(("quick", commands["quick"], False))
    for n, criterion in enumerate(issue.get("criteria") or [], 1):
        if criterion.get("command"):
            plan.append((f"criterion-{n}", criterion["command"], False))
    for name in ("full", "security", "visual"):
        if commands.get(name):
            plan.append((name, commands[name], False))
    for rel in scenarios(ctx):
        plan.append((f"scenario:{rel}", f'exec "$LOOPI_SCENARIOS"/{shlex.quote(rel)}', True))
    return plan


def run_gates(ctx, issue, worktree, base, out_dir):
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "cmd").mkdir(parents=True)
    plan = steps(ctx, issue)
    if not plan:
        return None, plan
    lines = ["set -u", 'cd "$LOOPI_APP"']
    for i, (_, cmd, _) in enumerate(plan):
        key = f"{i:02d}"
        (out_dir / "cmd" / f"{key}.sh").write_text(cmd + "\n")
        lines.append(
            f'sh "$LOOPI_OUT/cmd/{key}.sh" > "$LOOPI_OUT/{key}.log" 2>&1; '
            f'rc=$?; echo "$rc" > "$LOOPI_OUT/{key}.rc"; [ "$rc" -eq 0 ] || exit 0'
        )
    (out_dir / "gates.sh").write_text("\n".join(lines) + "\n")
    changed = " ".join(p for s, p in changed_files(worktree, base) if s != "D")
    if ctx.uses_container():
        env = {
            "LOOPI_APP": "/work",
            "LOOPI_OUT": "/loopi/out",
            "LOOPI_SCENARIOS": "/loopi/scenarios",
            "LOOPI_TWINS": "/loopi/twins",
            "LOOPI_VISUAL": "/loopi/visual",
            "LOOPI_CHANGED": changed,
        }
        mounts = [(out_dir, "/loopi/out", "rw")]
        for source, target in (
            (ctx.scenarios_dir, "/loopi/scenarios"),
            (ctx.twins_dir, "/loopi/twins"),
            (ctx.visual_dir, "/loopi/visual"),
        ):
            source.mkdir(parents=True, exist_ok=True)
            mounts.append((source, target, "ro"))
        argv, cwd, full_env = command(ctx, 'sh "$LOOPI_OUT/gates.sh"', worktree, mounts, env)
    else:
        env = {
            "LOOPI_APP": str(worktree),
            "LOOPI_OUT": str(out_dir),
            "LOOPI_SCENARIOS": str(ctx.scenarios_dir),
            "LOOPI_TWINS": str(ctx.twins_dir),
            "LOOPI_VISUAL": str(ctx.visual_dir),
            "LOOPI_CHANGED": changed,
        }
        script = f"sh {shlex.quote(str(out_dir / 'gates.sh'))}"
        argv, cwd, full_env = command(ctx, script, worktree, (), env)
    try:
        subprocess.run(argv, cwd=cwd, env=full_env, capture_output=True, text=True,
                       timeout=60 * ctx.cfg["limits"]["gates_minutes"])
    except subprocess.TimeoutExpired:
        return Failure("timeout", "portões excederam o tempo limite"), plan
    for i, (name, cmd, hidden) in enumerate(plan):
        rc_file = out_dir / f"{i:02d}.rc"
        if not rc_file.exists():
            return Failure(name, "passo não executado", cmd=cmd, hidden=hidden), plan
        code = rc_file.read_text().strip()
        if code != "0":
            output = (out_dir / f"{i:02d}.log").read_text(errors="replace")
            return Failure(name, f"saiu com código {code}", output, cmd, hidden=hidden), plan
    return None, plan
