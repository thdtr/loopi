import hashlib
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path


class LoopiError(Exception):
    pass


@lru_cache(maxsize=512)
def _glob_regex(pattern):
    i, out = 0, ""
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def matches(path, patterns):
    return any(_glob_regex(p).match(path) for p in patterns)


def run(argv, cwd=None, input=None, check=True, env=None, timeout=None):
    result = subprocess.run(
        argv, cwd=cwd, input=input, capture_output=True, text=True, errors="replace", env=env,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise LoopiError(f"{' '.join(map(str, argv))}: {detail}")
    return result


def git(cwd, *args, check=True, input=None):
    return run(["git", *args], cwd=cwd, check=check, input=input)


def short_hash(text, size=12):
    return hashlib.sha256(text.encode()).hexdigest()[:size]


def tail(text, lines=80):
    return "\n".join((text or "").splitlines()[-lines:])


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def failure_signature(text):
    normalized = re.sub(r"\d+(\.\d+)?(ms|s|m)?", "N", text or "")
    normalized = re.sub(r"0x[0-9a-fA-F]+", "H", normalized)
    normalized = re.sub(r"/[^\s:]*/", "/P/", normalized)
    return short_hash(tail(normalized, 30))
