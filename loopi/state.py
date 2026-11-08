import json
import re
import time
from datetime import datetime, timezone

from loopi.util import read_json, write_json

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
BLOCKED = "blocked"
ABORTED = "aborted"
IN_REVIEW = "in_review"
PROPOSED = "proposed"
DISCARDED = "discarded"

ESCALATED = (BLOCKED, ABORTED, IN_REVIEW, PROPOSED)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today():
    return datetime.now(timezone.utc).date().isoformat()


def _sort_key(issue_id):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", issue_id)]


class State:
    def __init__(self, ctx):
        self.ctx = ctx
        self.dir = ctx.state_dir
        self.issues_dir = self.dir / "issues"
        self.sessions_dir = self.dir / "sessions"
        self.journal = self.dir / "journal.ndjson"
        self.stop_file = self.dir / "STOP"

    def ensure(self):
        self.issues_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def issues(self):
        if not self.issues_dir.exists():
            return []
        items = [read_json(p) for p in self.issues_dir.glob("*.json")]
        return sorted([i for i in items if i], key=lambda i: _sort_key(i["id"]))

    def get(self, issue_id):
        return read_json(self.issues_dir / f"{issue_id}.json")

    def save(self, issue):
        issue["updated_at"] = now()
        write_json(self.issues_dir / f"{issue['id']}.json", issue)

    def create(self, issue_id, **fields):
        issue = {
            "id": issue_id,
            "spec": None,
            "title": "",
            "description": "",
            "criteria": [],
            "depends_on": [],
            "paths": [],
            "origin": "planner",
            "status": PENDING,
            "attempts": 0,
            "tickets": [],
            "cost_usd": 0.0,
            "reason": None,
            "review": None,
            "created_at": now(),
        }
        issue.update(fields)
        self.save(issue)
        return issue

    def next_id(self, prefix):
        highest = 0
        for issue in self.issues():
            match = re.fullmatch(re.escape(prefix) + r"\.(\d+)", issue["id"])
            if match:
                highest = max(highest, int(match.group(1)))
        return f"{prefix}.{highest + 1:02d}"

    def log(self, event, **data):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a") as f:
            f.write(json.dumps({"ts": now(), "event": event, **data}, ensure_ascii=False) + "\n")

    def events(self):
        if not self.journal.exists():
            return
        with self.journal.open() as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def cost_today(self):
        day = today()
        return sum(e.get("cost_usd") or 0 for e in self.events() if e["ts"].startswith(day))

    def count_today(self, event):
        day = today()
        return sum(1 for e in self.events() if e["event"] == event and e["ts"].startswith(day))

    def consecutive_rollbacks(self):
        count = 0
        for e in self.events():
            if e["event"] == "rollback":
                count += 1
            elif e["event"] == "deploy_ok":
                count = 0
        return count

    def session_log(self, issue_id, attempt, role="builder"):
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        return self.sessions_dir / f"{issue_id}-{role}-{attempt}-{int(time.time())}.ndjson"

    def next_ready(self):
        by_id = {i["id"]: i for i in self.issues()}
        for issue in by_id.values():
            if issue["status"] != PENDING:
                continue
            if all(by_id.get(dep, {}).get("status") == DONE for dep in issue.get("depends_on", [])):
                return issue
        return None
