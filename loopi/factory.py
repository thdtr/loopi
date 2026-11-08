import json
import shutil
import subprocess
import time
from pathlib import Path

from loopi import agent, gates, sandbox, state
from loopi.util import LoopiError, failure_signature, git, run, tail

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approved", "reasons"],
}


class Factory:
    def __init__(self, ctx, out=print):
        self.ctx = ctx
        self.state = state.State(ctx)
        self.out = out

    def check_repo(self):
        branch = self.ctx.cfg["branch"]
        current = git(self.ctx.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if current != branch:
            raise LoopiError(f"checkout atual é '{current}', esperado '{branch}'")
        if git(self.ctx.root, "status", "--porcelain").stdout.strip():
            raise LoopiError("há alterações não commitadas no repositório")

    def stop_reason(self):
        limits = self.ctx.cfg["stop"]
        if self.state.stop_file.exists():
            return "parada manual"
        if self.state.cost_today() >= self.ctx.cfg["budget"]["daily_usd"]:
            return "orçamento diário esgotado"
        if self.state.consecutive_rollbacks() >= limits["consecutive_rollbacks"]:
            self.state.stop_file.parent.mkdir(parents=True, exist_ok=True)
            self.state.stop_file.write_text("consecutive_rollbacks\n")
            return "rollbacks seguidos"
        if self.state.count_today("escalated") >= limits["daily_escalations"]:
            return "escalações demais hoje"
        return None

    def run(self, continuous=False, once=False, interval=300):
        self.state.ensure()
        self.check_repo()
        sandbox.require_auth(self.ctx)
        if self.ctx.uses_container():
            sandbox.image(self.ctx)
        for issue in self.state.issues():
            if issue["status"] == state.IN_PROGRESS:
                issue["status"] = state.PENDING
                self.state.save(issue)
        while True:
            reason = self.stop_reason()
            if reason:
                self.out(f"parada: {reason}")
                return 2
            self.sync_reviews()
            self.ingest_incidents()
            issue = self.state.next_ready()
            if not issue:
                if not continuous:
                    self.out("fila vazia")
                    return 0
                time.sleep(interval)
                continue
            try:
                self.process(issue)
            except LoopiError as e:
                self.escalate(self.state.get(issue["id"]), state.BLOCKED, f"erro interno: {e}")
            except Exception as e:
                self.escalate(self.state.get(issue["id"]), state.BLOCKED,
                              f"erro interno: {type(e).__name__}: {e}")
            if once:
                return 0

    def ingest_incidents(self):
        cmd = self.ctx.cfg["commands"].get("incidents")
        if not cmd:
            return
        result = subprocess.run(["sh", "-c", cmd], cwd=self.ctx.root, capture_output=True,
                                text=True, timeout=300)
        if result.returncode != 0:
            self.state.log("incidents_error", output=tail(result.stderr, 20))
            return
        try:
            items = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            self.state.log("incidents_error", output="invalid json")
            return
        known = {i.get("incident_key") for i in self.state.issues()}
        for item in items if isinstance(items, list) else []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            key = failure_signature(title)
            if key in known:
                continue
            issue = self.state.create(
                self.state.next_id("prod"), title=title,
                description=str(item.get("description") or ""), origin="production",
                incident_key=key,
            )
            known.add(key)
            self.state.log("incident", issue=issue["id"])

    def sync_reviews(self):
        for issue in self.state.issues():
            if issue["status"] != state.IN_REVIEW or not issue.get("review"):
                continue
            review = issue["review"]
            merged = False
            if review.get("pr") and shutil.which("gh"):
                result = run(["gh", "pr", "view", review["pr"], "--json", "state", "-q", ".state"],
                             cwd=self.ctx.root, check=False)
                pr_state = result.stdout.strip()
                if pr_state == "CLOSED":
                    issue["status"] = state.BLOCKED
                    issue["reason"] = "pr fechado sem merge"
                    self.state.save(issue)
                    continue
                merged = pr_state == "MERGED"
            elif review.get("branch"):
                result = git(self.ctx.root, "merge-base", "--is-ancestor", review["branch"],
                             self.ctx.cfg["branch"], check=False)
                merged = result.returncode == 0
            if merged:
                issue["status"] = state.DONE
                self.state.save(issue)
                self.state.log("review_merged", issue=issue["id"])

    def builder_input(self, issue):
        fields = ("id", "spec", "title", "description", "criteria", "paths")
        parts = ["# Issue", json.dumps({k: issue.get(k) for k in fields}, ensure_ascii=False, indent=2)]
        if issue.get("tickets"):
            parts += [
                "# Tentativas anteriores reprovadas",
                json.dumps(issue["tickets"][-3:], ensure_ascii=False, indent=2),
                "Registre em hypothesis uma hipótese diferente das anteriores.",
            ]
        return "\n\n".join(parts) + "\n"

    def process(self, issue):
        ctx = self.ctx
        branch = ctx.cfg["branch"]
        attempt = issue["attempts"] + 1
        issue["status"] = state.IN_PROGRESS
        issue["attempts"] = attempt
        self.state.save(issue)
        self.out(f"{issue['id']} tentativa {attempt}: {issue['title']}")
        workspace = ctx.cache_dir / "workspaces" / f"{issue['id']}-{attempt}"
        workspace_base = sandbox.export(ctx, branch, workspace)
        session = agent.run(
            ctx, "builder", agent.load_prompt("builder"), self.builder_input(issue), workspace,
            self.state.session_log(issue["id"], attempt), ctx.cfg["budget"]["session_usd"],
            setup=ctx.cfg["commands"].get("setup", ""),
        )
        issue["cost_usd"] = round(issue.get("cost_usd", 0) + session.cost, 4)
        self.state.log("session", issue=issue["id"], attempt=attempt, cost_usd=session.cost,
                       stop_reason=session.stop_reason)
        result = sandbox.read_result(workspace)
        worktree = ctx.cache_dir / "worktrees" / issue["id"]
        issue_branch = f"loopi/{issue['id']}"
        try:
            if result.get("status") == "blocked" and not session.stop_reason:
                evidence = result.get("evidence") or result.get("summary") or "sem evidência"
                return self.escalate(issue, state.BLOCKED, f"construtor: {evidence}")
            patch = sandbox.workspace_diff(workspace, workspace_base)
            if not patch.strip():
                reason = session.stop_reason or "nenhuma alteração produzida"
                return self.reject(issue, gates.Failure("session", reason, session.stderr), result)
            self.remove_worktree(worktree, issue_branch)
            git(ctx.root, "worktree", "add", "-q", "-b", issue_branch, str(worktree), branch)
            base = git(worktree, "rev-parse", "HEAD").stdout.strip()
            applied = subprocess.run(["git", "apply", "--index", "--binary", "-"], cwd=worktree,
                                     input=patch, capture_output=True)
            if applied.returncode != 0:
                output = applied.stderr.decode(errors="replace")
                return self.reject(issue, gates.Failure("apply", "patch não aplicou", output), result)
            git(worktree, *sandbox.IDENTITY, "commit", "-q", "--no-verify", "-m",
                f"loopi({issue['id']}): {issue['title']}")
            if session.stop_reason:
                return self.reject(issue, gates.Failure("watchdog", session.stop_reason), result)
            failure = gates.integrity(ctx, issue, worktree, base)
            if failure and failure.severe:
                issue["tickets"].append(failure.ticket())
                return self.escalate(issue, state.ABORTED, failure.reason)
            if failure:
                return self.reject(issue, failure, result)
            failure, plan = gates.run_gates(ctx, issue, worktree, base,
                                            ctx.cache_dir / "gates" / issue["id"])
            if failure:
                if failure.gate.startswith("scenario:"):
                    self.state.log("gap", issue=issue["id"], scenario=failure.gate)
                return self.reject(issue, failure, result)
            if ctx.cfg.get("judge"):
                failure = self.judge(issue, worktree, base)
                if failure:
                    return self.reject(issue, failure, result)
            self.state.log("approved", issue=issue["id"], steps=[s[0] for s in plan])
            touched = gates.sensitive_paths(ctx, worktree, base)
            if touched:
                return self.request_review(issue, issue_branch, touched)
            self.integrate(issue, issue_branch)
        finally:
            keep = issue["status"] == state.IN_REVIEW
            self.remove_worktree(worktree, issue_branch, keep_branch=keep)
            shutil.rmtree(workspace, ignore_errors=True)

    def judge(self, issue, worktree, base):
        diff = git(worktree, "diff", f"{base}..HEAD").stdout
        parts = ["# Issue", json.dumps(issue, ensure_ascii=False, indent=2)]
        for rel in (issue.get("spec"), "docs/DESIGN.md", "docs/PROJECT.md"):
            if rel and (worktree / rel).exists():
                parts += [f"# {rel}", (worktree / rel).read_text(errors="replace")[:60000]]
        parts += ["# Diff", diff[:150000]]
        session = agent.structured(
            self.ctx, "judge", agent.load_prompt("judge"), "\n\n".join(parts) + "\n", worktree,
            self.state.session_log(issue["id"], issue["attempts"], "judge"), JUDGE_SCHEMA, [],
        )
        verdict = session.structured
        self.state.log("judge", issue=issue["id"], cost_usd=session.cost,
                       approved=verdict.get("approved"))
        if not verdict.get("approved"):
            return gates.Failure("judge", "; ".join(verdict.get("reasons") or []))
        return None

    def reject(self, issue, failure, result):
        limits = self.ctx.cfg["limits"]
        ticket = failure.ticket()
        ticket["attempt"] = issue["attempts"]
        ticket["hypothesis"] = result.get("hypothesis") if result else None
        ticket["signature"] = failure.gate + ":" + failure_signature(failure.output or failure.reason)
        issue["tickets"].append(ticket)
        same = sum(1 for t in issue["tickets"] if t.get("signature") == ticket["signature"])
        self.state.log("rejected", issue=issue["id"], gate=failure.gate, reason=failure.reason)
        self.out(f"{issue['id']} reprovada em {failure.gate}: {failure.reason}")
        if gates.is_environment(failure):
            return self.escalate(issue, state.BLOCKED, f"ambiente: {failure.gate} sem ferramenta necessária")
        if same >= limits["same_failures"]:
            return self.escalate(issue, state.BLOCKED, f"mesma falha {same}x em {failure.gate}")
        if issue["attempts"] >= limits["attempts"]:
            return self.escalate(issue, state.BLOCKED, "tentativas esgotadas")
        issue["status"] = state.PENDING
        self.state.save(issue)

    def escalate(self, issue, status, reason):
        issue["status"] = status
        issue["reason"] = reason
        self.state.save(issue)
        self.state.log("escalated", issue=issue["id"], status=status, reason=reason)
        self.out(f"{issue['id']} {status}: {reason}")

    def request_review(self, issue, issue_branch, touched):
        ctx = self.ctx
        review = {"branch": issue_branch, "pr": None, "paths": touched}
        if ctx.cfg.get("publish") and self.has_remote():
            git(ctx.root, "push", "-q", "-u", "origin", issue_branch)
            if shutil.which("gh"):
                fields = {k: issue.get(k) for k in ("id", "spec", "criteria", "paths")}
                fields["sensitive"] = touched
                body = "```json\n" + json.dumps(fields, ensure_ascii=False, indent=2) + "\n```"
                created = run(["gh", "pr", "create", "--base", ctx.cfg["branch"], "--head",
                               issue_branch, "--title", f"loopi({issue['id']}): {issue['title']}",
                               "--body", body], cwd=ctx.root, check=False)
                if created.returncode == 0:
                    review["pr"] = created.stdout.strip().splitlines()[-1]
        issue["review"] = review
        self.escalate(issue, state.IN_REVIEW, "caminhos sensíveis: " + ", ".join(touched[:10]))

    def integrate(self, issue, issue_branch):
        ctx = self.ctx
        branch = ctx.cfg["branch"]
        before = git(ctx.root, "rev-parse", branch).stdout.strip()
        git(ctx.root, "merge", "-q", "--ff-only", issue_branch)
        after = git(ctx.root, "rev-parse", branch).stdout.strip()
        if ctx.cfg.get("publish") and self.has_remote():
            git(ctx.root, "push", "-q", "origin", branch)
        issue["status"] = state.DONE
        issue["reason"] = None
        self.state.save(issue)
        self.state.log("merged", issue=issue["id"], commit=after)
        self.out(f"{issue['id']} integrada")
        self.deploy(issue, before, after)

    def deploy(self, issue, before, after):
        commands = self.ctx.cfg["commands"]
        if not commands.get("deploy"):
            return
        timeout = 60 * self.ctx.cfg["limits"]["deploy_minutes"]
        ok, output = self.host_command(commands["deploy"], timeout)
        if ok and commands.get("smoke"):
            ok, output = self.host_command(commands["smoke"], timeout)
        if ok:
            self.state.log("deploy_ok", issue=issue["id"])
            return
        self.state.log("rollback", issue=issue["id"])
        self.out(f"{issue['id']} falhou após o deploy, revertendo")
        if commands.get("rollback"):
            self.host_command(commands["rollback"], timeout)
        git(self.ctx.root, *sandbox.IDENTITY, "revert", "--no-edit", f"{before}..{after}")
        if self.ctx.cfg.get("publish") and self.has_remote():
            git(self.ctx.root, "push", "-q", "origin", self.ctx.cfg["branch"])
        issue["status"] = state.PENDING
        issue["tickets"].append({"gate": "smoke", "reason": "falhou após o deploy",
                                 "output": tail(output, 60), "attempt": issue["attempts"]})
        self.state.save(issue)

    def host_command(self, cmd, timeout):
        try:
            result = subprocess.run(["sh", "-c", cmd], cwd=self.ctx.root, capture_output=True,
                                    text=True, timeout=timeout)
            return result.returncode == 0, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return False, "timeout"

    def has_remote(self):
        return "origin" in git(self.ctx.root, "remote").stdout.split()

    def remove_worktree(self, worktree, issue_branch, keep_branch=False):
        if Path(worktree).exists():
            git(self.ctx.root, "worktree", "remove", "--force", str(worktree), check=False)
            shutil.rmtree(worktree, ignore_errors=True)
        git(self.ctx.root, "worktree", "prune", check=False)
        if not keep_branch:
            git(self.ctx.root, "branch", "-D", issue_branch, check=False)
