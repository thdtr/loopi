import os
import shlex
import shutil
import subprocess
from pathlib import Path

from loopi.util import LoopiError, git, read_json, run, short_hash

RESULT_FILE = ".loopi-result.json"
AUTH_VARS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
)
IDENTITY = ["-c", "user.name=loopi", "-c", "user.email=loopi@localhost"]
ARTIFACTS = [
    "node_modules/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".venv/",
    ".next/",
    ".nuxt/",
    ".turbo/",
    ".dart_tool/",
    ".gradle/",
    "coverage/",
    ".nyc_output/",
    "playwright-report/",
    "test-results/",
    ".DS_Store",
    "*.log",
]


def export(ctx, ref, dest):
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref, "--", ".", ":(exclude).loopi"],
        cwd=ctx.root,
        capture_output=True,
    )
    if archive.returncode != 0:
        raise LoopiError(archive.stderr.decode(errors="replace"))
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    git(dest, "init", "-q", "-b", "loopi")
    (dest / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (dest / ".git" / "info" / "exclude").write_text("\n".join(ARTIFACTS) + "\n")
    git(dest, "add", "-A")
    git(dest, *IDENTITY, "commit", "-q", "--allow-empty", "--no-verify", "-m", "base")
    return git(dest, "rev-parse", "HEAD").stdout.strip()


def read_result(workspace):
    data = read_json(Path(workspace) / RESULT_FILE)
    return data if isinstance(data, dict) else {}


def workspace_diff(workspace, base):
    workspace = Path(workspace)
    (workspace / RESULT_FILE).unlink(missing_ok=True)
    git(workspace, "add", "-A")
    result = subprocess.run(
        ["git", "diff", "--cached", "--binary", base], cwd=workspace, capture_output=True
    )
    if result.returncode != 0:
        raise LoopiError(result.stderr.decode(errors="replace"))
    return result.stdout


def image(ctx):
    if not ctx.containerfile.exists():
        raise LoopiError(f"{ctx.containerfile} não encontrado")
    tag = f"loopi-{_slug(ctx.root.name)}:{short_hash(ctx.containerfile.read_text())}"
    runtime = ctx.runtime()
    if run([runtime, "image", "inspect", tag], check=False).returncode != 0:
        built = subprocess.run(
            [runtime, "build", "-t", tag, "-f", str(ctx.containerfile), str(ctx.factory_dir)]
        )
        if built.returncode != 0:
            raise LoopiError(f"falha ao construir a imagem {tag}")
    return tag


def auth_env():
    return {k: os.environ[k] for k in AUTH_VARS if os.environ.get(k)}


def require_auth(ctx):
    if ctx.uses_container() and not auth_env():
        raise LoopiError(
            "o container precisa de CLAUDE_CODE_OAUTH_TOKEN (gere com: claude setup-token) "
            "ou ANTHROPIC_API_KEY"
        )


def command(ctx, script, workspace, mounts=(), env=None, interactive=False):
    env = dict(env or {})
    if not ctx.uses_container():
        full_env = dict(os.environ)
        full_env.update(env)
        return ["sh", "-c", script], str(workspace), full_env
    runtime = ctx.runtime()
    argv = [runtime, "run", "--rm", "--init"]
    if interactive:
        argv.append("-i")
    if Path(runtime).name == "podman":
        argv.append("--userns=keep-id")
    else:
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    argv += ["-v", f"{workspace}:/work:z", "-w", "/work"]
    for source, target, mode in mounts:
        argv += ["-v", f"{source}:{target}:{mode},z"]
    for volume in ctx.cfg.get("volumes", []):
        argv += ["-v", os.path.expanduser(volume)]
    env.setdefault("HOME", "/tmp/home")
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    env.update(auth_env())
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    argv += [image(ctx), "sh", "-c", f'mkdir -p "$HOME"; {script}']
    return argv, None, None


def claude_script(claude_argv, setup=""):
    line = shlex.join(claude_argv)
    if setup:
        return f"( {setup} ) > /tmp/loopi-setup.log 2>&1; exec {line}"
    return f"exec {line}"


def _slug(name):
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-") or "project"
