import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loopi import state  # noqa: E402
from loopi.agent import Watchdog  # noqa: E402
from loopi.cli import main  # noqa: E402
from loopi.config import DEFAULTS  # noqa: E402
from loopi.util import matches  # noqa: E402

FAKE = ROOT / "tests" / "fakes" / "claude"


def sh(cwd, *argv):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


class GlobTest(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(matches("ui/button/index.tsx", ["ui/**"]))
        self.assertTrue(matches("app/db/migrations/001.sql", ["**/migrations/**"]))
        self.assertTrue(matches("pnpm-lock.yaml", ["**/pnpm-lock.yaml"]))
        self.assertTrue(matches("a/b/x.test.ts", ["**/*.test.*"]))
        self.assertFalse(matches("app/ui.ts", ["ui/**"]))
        self.assertFalse(matches("src/a.ts", ["*.ts"]))


class WatchdogTest(unittest.TestCase):
    def call(self, name, payload, tokens=10):
        return {"type": "assistant", "message": {
            "usage": {"input_tokens": tokens},
            "content": [{"type": "tool_use", "name": name, "input": payload}]}}

    def test_repetition_only_when_consecutive(self):
        w = Watchdog(DEFAULTS["limits"])
        for _ in range(3):
            self.assertIsNone(w.observe(self.call("Bash", {"command": "npm test"})))
            self.assertIsNone(w.observe(self.call("Edit", {"file_path": "a"})))
        for _ in range(3):
            self.assertIsNone(w.observe(self.call("Bash", {"command": "ls"})))
        self.assertEqual(w.observe(self.call("Bash", {"command": "ls"})), "repetition")

    def test_no_progress(self):
        limits = dict(DEFAULTS["limits"], turns_without_edit=3, repeated_calls=99)
        w = Watchdog(limits)
        self.assertIsNone(w.observe(self.call("Read", {"p": 1})))
        self.assertIsNone(w.observe(self.call("Read", {"p": 2})))
        self.assertEqual(w.observe(self.call("Read", {"p": 3})), "no_progress")

    def test_context(self):
        w = Watchdog(DEFAULTS["limits"])
        self.assertEqual(w.observe(self.call("Read", {}, tokens=10**6)), "context")

    def test_subagent_ignored(self):
        w = Watchdog(dict(DEFAULTS["limits"], repeated_calls=2))
        event = dict(self.call("Bash", {"command": "ls"}), parent_tool_use_id="x")
        self.assertIsNone(w.observe(event))
        self.assertIsNone(w.observe(event))


class FactoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "app"
        self.repo.mkdir()
        self.env = {
            "XDG_CACHE_HOME": str(base / "cache"),
            "LOOPI_CLAUDE": str(FAKE),
            "FAKE_CLAUDE_LOG": str(base / "claude.log"),
        }
        self.saved = {k: os.environ.get(k) for k in [*self.env, "FAKE_BUILDER", "FAKE_JUDGE"]}
        os.environ.update(self.env)
        self.cwd = os.getcwd()
        os.chdir(self.repo)
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.name", "t")
        sh(self.repo, "git", "config", "user.email", "t@t")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text("def test_a():\n    assert 1 == 1\n")
        (self.repo / "docs" / "specs").mkdir(parents=True)
        (self.repo / "docs" / "specs" / "001-feature.md").write_text("# Feature\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "init")
        self.assertEqual(main(["init", "--no-agent"]), 0)
        cfg = json.loads((self.repo / ".loopi" / "loopi.json").read_text())
        cfg.update(isolation="host", permission_mode="dontAsk", judge=False)
        cfg["commands"]["quick"] = "test -f feature.txt"
        cfg["models"] = {"builder": "fake-builder", "planner": "fake-planner",
                         "judge": "fake-judge", "improver": "fake-improver", "founder": "fake-founder"}
        (self.repo / ".loopi" / "loopi.json").write_text(json.dumps(cfg))
        scenario = self.repo / ".loopi" / "scenarios" / "001-feature" / "conteudo.sh"
        scenario.parent.mkdir(parents=True)
        scenario.write_text('#!/bin/sh\ngrep -q pronto "$LOOPI_APP/feature.txt"\n')
        scenario.chmod(0o755)
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "loopi")

    def tearDown(self):
        os.chdir(self.cwd)
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def issues(self):
        out = subprocess.run([sys.executable, "-m", "loopi", "status", "--json"], cwd=self.repo,
                             capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(ROOT)))
        return {i["id"]: i for i in json.loads(out.stdout)}

    def plan(self):
        self.assertEqual(main(["plan", "docs/specs/001-feature.md"]), 0)

    def test_init_files(self):
        for rel in ("loopi.json", "Containerfile", ".gitignore", "scenarios", "twins", "visual"):
            self.assertTrue((self.repo / ".loopi" / rel).exists(), rel)

    def test_plan_creates_dependent_issues(self):
        self.plan()
        issues = self.issues()
        self.assertEqual(issues["001-feature.02"]["depends_on"], ["001-feature.01"])
        self.assertEqual(main(["plan", "docs/specs/001-feature.md"]), 1)

    def test_happy_path_merges(self):
        self.plan()
        os.environ["FAKE_BUILDER"] = "ok"
        self.assertEqual(main(["run", "--once"]), 0)
        issue = self.issues()["001-feature.01"]
        self.assertEqual(issue["status"], state.DONE)
        self.assertEqual((self.repo / "feature.txt").read_text(), "pronto\n")
        log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=self.repo, capture_output=True,
                             text=True).stdout
        self.assertIn("loopi(001-feature.01)", log)

    def test_binary_files_and_artifacts(self):
        self.plan()
        os.environ["FAKE_BUILDER"] = "binary"
        main(["run", "--once"])
        self.assertEqual(self.issues()["001-feature.01"]["status"], state.DONE)
        self.assertTrue((self.repo / "tests" / "dado.bin").exists())
        self.assertFalse((self.repo / "tests" / "__pycache__").exists())

    def test_builder_never_sees_factory(self):
        self.plan()
        os.environ["FAKE_BUILDER"] = "ok"
        main(["run", "--once"])
        calls = [json.loads(l) for l in Path(self.env["FAKE_CLAUDE_LOG"]).read_text().splitlines()]
        builder = [c for c in calls if c["model"] == "fake-builder"][0]
        self.assertNotEqual(Path(builder["cwd"]).resolve(), self.repo.resolve())
        self.assertFalse((Path(self.tmp.name) / "cache").joinpath(".loopi").exists())

    def test_hidden_scenario_failure_blocks_after_same_failures(self):
        self.plan()
        os.environ["FAKE_BUILDER"] = "wrong"
        main(["run", "--once"])
        issue = self.issues()["001-feature.01"]
        self.assertEqual(issue["status"], state.PENDING)
        self.assertEqual(issue["tickets"][0]["output"], "")
        self.assertIsNone(issue["tickets"][0]["command"])
        main(["run", "--once"])
        self.assertEqual(self.issues()["001-feature.01"]["status"], state.BLOCKED)
        self.assertFalse((self.repo / "feature.txt").exists())

    def test_builder_block_escalates(self):
        self.plan()
        os.environ["FAKE_BUILDER"] = "blocked"
        main(["run", "--once"])
        issue = self.issues()["001-feature.01"]
        self.assertEqual(issue["status"], state.BLOCKED)
        self.assertIn("spec contraditória", issue["reason"])

    def test_deleted_test_aborts(self):
        self.plan()
        os.environ["FAKE_BUILDER"] = "delete_test"
        main(["run", "--once"])
        self.assertEqual(self.issues()["001-feature.01"]["status"], state.ABORTED)

    def test_sensitive_path_goes_to_review(self):
        self.plan()
        cfg_file = self.repo / ".loopi" / "loopi.json"
        cfg = json.loads(cfg_file.read_text())
        cfg["publish"] = False
        cfg_file.write_text(json.dumps(cfg))
        sh(self.repo, "git", "commit", "-qam", "cfg")
        os.environ["FAKE_BUILDER"] = "sensitive"
        issues_dir = self.repo / ".loopi" / "state" / "issues"
        issue = json.loads((issues_dir / "001-feature.01.json").read_text())
        issue["paths"] = ["feature.txt", "docs/**"]
        (issues_dir / "001-feature.01.json").write_text(json.dumps(issue))
        main(["run", "--once"])
        issue = self.issues()["001-feature.01"]
        self.assertEqual(issue["status"], state.IN_REVIEW)
        sh(self.repo, "git", "merge", "-q", "--ff-only", "loopi/001-feature.01")
        self.assertEqual(self.issues()["001-feature.01"]["status"], state.DONE)

    def test_watchdog_kills_repeating_session(self):
        self.plan()
        os.environ["FAKE_BUILDER"] = "loop"
        main(["run", "--once"])
        issue = self.issues()["001-feature.01"]
        self.assertEqual(issue["tickets"][0]["reason"], "repetition")

    def test_judge_rejects(self):
        self.plan()
        cfg_file = self.repo / ".loopi" / "loopi.json"
        cfg = json.loads(cfg_file.read_text())
        cfg["judge"] = True
        cfg_file.write_text(json.dumps(cfg))
        sh(self.repo, "git", "commit", "-qam", "cfg")
        os.environ["FAKE_BUILDER"] = "ok"
        os.environ["FAKE_JUDGE"] = "no"
        main(["run", "--once"])
        self.assertEqual(self.issues()["001-feature.01"]["tickets"][0]["gate"], "judge")

    def test_stop_and_resume(self):
        self.plan()
        main(["stop"])
        self.assertEqual(main(["run", "--once"]), 2)
        main(["resume"])
        os.environ["FAKE_BUILDER"] = "ok"
        self.assertEqual(main(["run", "--once"]), 0)

    def test_dirty_tree_refused(self):
        (self.repo / "sujo.txt").write_text("x")
        self.assertEqual(main(["run"]), 1)


if __name__ == "__main__":
    unittest.main()
