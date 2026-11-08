import copy
import os
import shutil
from pathlib import Path

from loopi.util import LoopiError, git, read_json, short_hash

DEFAULTS = {
    "version": 1,
    "branch": "main",
    "publish": True,
    "isolation": "container",
    "runtime": None,
    "volumes": [],
    "permission_mode": None,
    "claude": "claude",
    "models": {
        "builder": "opus",
        "planner": "opus",
        "judge": "sonnet",
        "improver": "sonnet",
        "founder": "sonnet",
    },
    "budget": {"session_usd": 3.0, "structured_usd": 1.0, "daily_usd": 50.0},
    "limits": {
        "attempts": 4,
        "same_failures": 2,
        "repeated_calls": 4,
        "turns_without_edit": 15,
        "context_tokens": 150000,
        "session_minutes": 45,
        "gates_minutes": 30,
        "deploy_minutes": 20,
    },
    "commands": {
        "setup": "",
        "quick": "",
        "full": "",
        "security": "",
        "visual": "",
        "deploy": "",
        "smoke": "",
        "rollback": "",
        "incidents": "",
    },
    "sensitive_paths": [
        "ui/**",
        "**/auth/**",
        "**/billing/**",
        "**/payments/**",
        "**/migrations/**",
        "infra/**",
        ".github/**",
        ".claude/**",
        "CLAUDE.md",
        "AGENTS.md",
        "docs/**",
    ],
    "free_scope": [
        "**/package-lock.json",
        "**/pnpm-lock.yaml",
        "**/yarn.lock",
        "**/bun.lock",
        "**/bun.lockb",
        "**/poetry.lock",
        "**/uv.lock",
        "**/Cargo.lock",
        "**/go.sum",
        "**/composer.lock",
        "**/pubspec.lock",
        "**/Gemfile.lock",
    ],
    "tests": {
        "files": [
            "**/tests/**",
            "**/test/**",
            "**/__tests__/**",
            "**/e2e/**",
            "**/*.test.*",
            "**/*.spec.*",
            "**/test_*.py",
            "**/*_test.py",
            "**/*_test.go",
            "**/*_test.dart",
            "**/*Test.java",
            "**/*Test.kt",
            "**/*Tests.swift",
            "**/*Test.php",
        ],
        "asserts": r"\b(assert\w*|expect|should|require\.\w+|t\.(Error|Fatal)\w*|XCTAssert\w*)\b",
        "skips": r"(\.skip\(|\bx(it|describe|test)\(|\.only\(|@pytest\.mark\.(skip|xfail)|@unittest\.skip|\bt\.Skip(Now)?\(|@Disabled|@Ignore|\bskip:\s*true|markTestSkipped)",
    },
    "judge": False,
    "stop": {"consecutive_rollbacks": 2, "daily_escalations": 10},
}


def merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


class Context:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.factory_dir = self.root / ".loopi"
        self.config_file = self.factory_dir / "loopi.json"
        self.state_dir = self.factory_dir / "state"
        self.scenarios_dir = self.factory_dir / "scenarios"
        self.twins_dir = self.factory_dir / "twins"
        self.visual_dir = self.factory_dir / "visual"
        self.containerfile = self.factory_dir / "Containerfile"
        cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.cache_dir = cache_home / "loopi" / f"{self.root.name}-{short_hash(str(self.root), 8)}"
        self.cfg = merge(DEFAULTS, read_json(self.config_file, {}))
        if os.environ.get("LOOPI_CLAUDE"):
            self.cfg["claude"] = os.environ["LOOPI_CLAUDE"]

    @property
    def initialized(self):
        return self.config_file.exists()

    def runtime(self):
        if self.cfg.get("runtime"):
            return self.cfg["runtime"]
        for candidate in ("podman", "docker"):
            if shutil.which(candidate):
                return candidate
        raise LoopiError("isolamento por container exige podman ou docker")

    def uses_container(self):
        return self.cfg["isolation"] == "container"

    def permission_mode(self):
        if self.cfg.get("permission_mode"):
            return self.cfg["permission_mode"]
        return "bypassPermissions" if self.uses_container() else "auto"


def locate(cwd=None):
    result = git(cwd or os.getcwd(), "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise LoopiError("execute dentro de um repositório git")
    return Context(result.stdout.strip())
