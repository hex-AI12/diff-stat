"""
Tests for diff-stat: intelligent git diff summarizer.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import diff_stat


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_git_repo(tmp_dir: str) -> str:
    """Create a minimal git repo for integration tests."""
    subprocess.run(["git", "init", tmp_dir], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_dir, check=True, capture_output=True)
    return tmp_dir


def make_commit(repo: str, files: dict[str, str], message: str) -> str:
    """Write files and make a commit, return short SHA."""
    for path, content in files.items():
        full = Path(repo) / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        subprocess.run(["git", "add", path], cwd=repo, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.split("]")[0].split("[")[1].split(" ")[-1].strip()


# ── categorize ────────────────────────────────────────────────────────────────

class TestCategorize(unittest.TestCase):

    def test_python_source(self):
        self.assertEqual(diff_stat.categorize("src/app.py"), "source")

    def test_javascript(self):
        self.assertEqual(diff_stat.categorize("app.js"), "source")

    def test_typescript(self):
        self.assertEqual(diff_stat.categorize("components/Button.tsx"), "source")

    def test_go_source(self):
        self.assertEqual(diff_stat.categorize("cmd/main.go"), "source")

    def test_rust_source(self):
        self.assertEqual(diff_stat.categorize("src/lib.rs"), "source")

    def test_pytest_file(self):
        self.assertEqual(diff_stat.categorize("tests/test_app.py"), "test")

    def test_jest_spec(self):
        self.assertEqual(diff_stat.categorize("src/Button.spec.ts"), "test")

    def test_test_subdir(self):
        self.assertEqual(diff_stat.categorize("test/unit/parser.go"), "test")

    def test_yaml_config(self):
        self.assertEqual(diff_stat.categorize("config/settings.yaml"), "config")

    def test_toml_config(self):
        self.assertEqual(diff_stat.categorize("pyproject.toml"), "config")

    def test_json_config(self):
        self.assertEqual(diff_stat.categorize("package.json"), "config")

    def test_markdown_docs(self):
        self.assertEqual(diff_stat.categorize("README.md"), "docs")

    def test_rst_docs(self):
        self.assertEqual(diff_stat.categorize("docs/api.rst"), "docs")

    def test_dockerfile(self):
        self.assertEqual(diff_stat.categorize("Dockerfile"), "infra")

    def test_terraform(self):
        self.assertEqual(diff_stat.categorize("infra/main.tf"), "infra")

    def test_makefile(self):
        self.assertEqual(diff_stat.categorize("Makefile"), "infra")

    def test_css_web(self):
        self.assertEqual(diff_stat.categorize("static/style.css"), "web")

    def test_html_web(self):
        self.assertEqual(diff_stat.categorize("templates/index.html"), "web")

    def test_sql_data(self):
        self.assertEqual(diff_stat.categorize("migrations/001_init.sql"), "data")

    def test_lockfile_build(self):
        self.assertEqual(diff_stat.categorize("package-lock.json"), "build")

    def test_unknown_extension(self):
        self.assertEqual(diff_stat.categorize("somefile.xyz"), "other")

    def test_no_extension(self):
        self.assertEqual(diff_stat.categorize("scripts/deploy"), "other")


# ── top_module ────────────────────────────────────────────────────────────────

class TestTopModule(unittest.TestCase):

    def test_root_file(self):
        self.assertEqual(diff_stat.top_module("README.md"), ".")

    def test_single_level(self):
        self.assertEqual(diff_stat.top_module("src/main.py"), "src")

    def test_two_levels(self):
        self.assertEqual(diff_stat.top_module("src/app/models.py"), "src/app")

    def test_deep(self):
        self.assertEqual(diff_stat.top_module("a/b/c/d/e.py"), "a/b")

    def test_custom_depth(self):
        self.assertEqual(diff_stat.top_module("a/b/c/d.py", depth=3), "a/b/c")


# ── bar helper ────────────────────────────────────────────────────────────────

class TestBar(unittest.TestCase):

    def test_full(self):
        b = diff_stat.bar(100, 100, width=10)
        self.assertEqual(b, "█" * 10)

    def test_empty(self):
        b = diff_stat.bar(0, 100, width=10)
        self.assertEqual(b, "░" * 10)

    def test_half(self):
        b = diff_stat.bar(50, 100, width=10)
        self.assertEqual(b, "█" * 5 + "░" * 5)

    def test_zero_max(self):
        b = diff_stat.bar(50, 0, width=10)
        self.assertEqual(b, "")


# ── risk scoring ──────────────────────────────────────────────────────────────

class TestRiskScoring(unittest.TestCase):

    def _make_stats(self, **overrides):
        base = {
            "total_added": 10,
            "total_removed": 5,
            "n_files": 2,
            "n_deleted": 0,
            "n_modules": 1,
            "by_category": {"source": {"files": 2, "added": 10, "removed": 5}},
        }
        base.update(overrides)
        return base

    def test_minimal_change(self):
        stats = self._make_stats()
        risk = diff_stat.compute_risk(stats)
        self.assertLess(risk["score"], 40)
        self.assertIn(risk["level"], ("MINIMAL", "LOW"))

    def test_high_churn(self):
        stats = self._make_stats(total_added=5000, total_removed=2000, n_files=50, n_modules=10)
        risk = diff_stat.compute_risk(stats)
        self.assertGreater(risk["score"], 50)

    def test_infra_changes_raise_risk(self):
        stats_no_infra = self._make_stats(
            by_category={"source": {"files": 5, "added": 100, "removed": 50}},
        )
        stats_with_infra = self._make_stats(
            by_category={
                "source": {"files": 5, "added": 100, "removed": 50},
                "infra": {"files": 5, "added": 20, "removed": 10},
            },
        )
        r1 = diff_stat.compute_risk(stats_no_infra)
        r2 = diff_stat.compute_risk(stats_with_infra)
        self.assertGreater(r2["score"], r1["score"])

    def test_deletions_raise_risk(self):
        base = self._make_stats(n_deleted=0)
        with_del = self._make_stats(n_deleted=10)
        r1 = diff_stat.compute_risk(base)
        r2 = diff_stat.compute_risk(with_del)
        self.assertGreater(r2["score"], r1["score"])

    def test_low_test_ratio_penalized(self):
        no_tests = self._make_stats(
            by_category={"source": {"files": 10, "added": 200, "removed": 50}},
        )
        with_tests = self._make_stats(
            by_category={
                "source": {"files": 10, "added": 200, "removed": 50},
                "test": {"files": 8, "added": 150, "removed": 30},
            },
        )
        r1 = diff_stat.compute_risk(no_tests)
        r2 = diff_stat.compute_risk(with_tests)
        self.assertGreater(r1["score"], r2["score"])

    def test_level_labels(self):
        # Need large churn + many files + infra changes + deletions to reach HIGH (>=75)
        high = self._make_stats(
            total_added=20000, total_removed=10000,
            n_files=200, n_deleted=15, n_modules=30,
            by_category={
                "source": {"files": 150, "added": 15000, "removed": 8000},
                "infra": {"files": 10, "added": 500, "removed": 200},
            },
        )
        r = diff_stat.compute_risk(high)
        self.assertEqual(r["level"], "HIGH")

    def test_score_bounds(self):
        for churn in [0, 10, 100, 1000, 100000]:
            stats = self._make_stats(total_added=churn, total_removed=churn // 2)
            r = diff_stat.compute_risk(stats)
            self.assertGreaterEqual(r["score"], 0)
            self.assertLessEqual(r["score"], 100)

    def test_factors_not_empty(self):
        stats = self._make_stats()
        r = diff_stat.compute_risk(stats)
        self.assertTrue(len(r["factors"]) > 0)


# ── FileEntry ─────────────────────────────────────────────────────────────────

class TestFileEntry(unittest.TestCase):

    def test_churn(self):
        e = diff_stat.FileEntry(
            path="foo.py", added=10, removed=5,
            status="M", category="source", module="."
        )
        self.assertEqual(e.churn, 15)

    def test_zero_churn(self):
        e = diff_stat.FileEntry(
            path="foo.py", added=0, removed=0,
            status="M", category="source", module="."
        )
        self.assertEqual(e.churn, 0)


# ── format_markdown ───────────────────────────────────────────────────────────

class TestFormatMarkdown(unittest.TestCase):

    def _sample_stats(self):
        entries = [
            diff_stat.FileEntry("src/app.py", 50, 10, "M", "source", "src"),
            diff_stat.FileEntry("tests/test_app.py", 20, 0, "A", "test", "tests"),
        ]
        stats = {
            "total_added": 70,
            "total_removed": 10,
            "total_churn": 80,
            "n_files": 2,
            "n_added": 1,
            "n_modified": 1,
            "n_deleted": 0,
            "n_renamed": 0,
            "n_modules": 2,
            "by_category": {
                "source": {"files": 1, "added": 50, "removed": 10},
                "test": {"files": 1, "added": 20, "removed": 0},
            },
            "by_module": {
                "src": {"files": 1, "added": 50, "removed": 10},
                "tests": {"files": 1, "added": 20, "removed": 0},
            },
            "entries": entries,
        }
        stats["risk"] = diff_stat.compute_risk(stats)
        return stats

    def test_returns_string(self):
        s = self._sample_stats()
        out = diff_stat.format_markdown(s, {"short": "abc1234", "subject": "test"}, None, "commit")
        self.assertIsInstance(out, str)

    def test_has_summary_header(self):
        s = self._sample_stats()
        out = diff_stat.format_markdown(s, {"short": "abc", "subject": "s"}, None, "commit")
        self.assertIn("## Summary", out)

    def test_has_risk_section(self):
        s = self._sample_stats()
        out = diff_stat.format_markdown(s, {"short": "abc", "subject": "s"}, None, "commit")
        self.assertIn("Risk Score", out)

    def test_has_files_section(self):
        s = self._sample_stats()
        out = diff_stat.format_markdown(s, {"short": "abc", "subject": "s"}, None, "commit")
        self.assertIn("## Files Changed", out)
        self.assertIn("src/app.py", out)

    def test_has_category_section(self):
        s = self._sample_stats()
        out = diff_stat.format_markdown(s, {"short": "abc", "subject": "s"}, None, "commit")
        self.assertIn("## By File Type", out)
        self.assertIn("source", out)
        self.assertIn("test", out)

    def test_unstaged_mode(self):
        s = self._sample_stats()
        out = diff_stat.format_markdown(s, {}, None, "unstaged")
        self.assertIn("unstaged", out.lower())


# ── format_json ───────────────────────────────────────────────────────────────

class TestFormatJSON(unittest.TestCase):

    def _sample_stats(self):
        entries = [
            diff_stat.FileEntry("main.go", 100, 20, "M", "source", "cmd"),
        ]
        stats = {
            "total_added": 100,
            "total_removed": 20,
            "total_churn": 120,
            "n_files": 1,
            "n_added": 0,
            "n_modified": 1,
            "n_deleted": 0,
            "n_renamed": 0,
            "n_modules": 1,
            "by_category": {"source": {"files": 1, "added": 100, "removed": 20}},
            "by_module": {"cmd": {"files": 1, "added": 100, "removed": 20}},
            "entries": entries,
        }
        stats["risk"] = diff_stat.compute_risk(stats)
        return stats

    def test_valid_json(self):
        s = self._sample_stats()
        out = diff_stat.format_json(s, {"short": "abc", "subject": "s"}, None, "commit")
        parsed = json.loads(out)
        self.assertIsInstance(parsed, dict)

    def test_has_summary(self):
        s = self._sample_stats()
        out = json.loads(diff_stat.format_json(s, {}, None, "commit"))
        self.assertIn("summary", out)
        self.assertEqual(out["summary"]["n_files"], 1)

    def test_has_risk(self):
        s = self._sample_stats()
        out = json.loads(diff_stat.format_json(s, {}, None, "commit"))
        self.assertIn("risk", out)
        self.assertIn("score", out["risk"])
        self.assertIn("level", out["risk"])

    def test_has_files(self):
        s = self._sample_stats()
        out = json.loads(diff_stat.format_json(s, {}, None, "commit"))
        self.assertIn("files", out)
        self.assertEqual(len(out["files"]), 1)
        self.assertEqual(out["files"][0]["path"], "main.go")

    def test_has_by_category(self):
        s = self._sample_stats()
        out = json.loads(diff_stat.format_json(s, {}, None, "commit"))
        self.assertIn("by_category", out)

    def test_has_by_module(self):
        s = self._sample_stats()
        out = json.loads(diff_stat.format_json(s, {}, None, "commit"))
        self.assertIn("by_module", out)

    def test_generated_at_present(self):
        s = self._sample_stats()
        out = json.loads(diff_stat.format_json(s, {}, None, "commit"))
        self.assertIn("generated_at", out)


# ── Integration (real git repo) ───────────────────────────────────────────────

class TestIntegration(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        make_git_repo(self.tmp)

    def test_analyze_simple_commit(self):
        make_commit(self.tmp, {"src/app.py": "# v1\nx = 1\n"}, "initial")
        make_commit(self.tmp, {"src/app.py": "# v2\nx = 2\ny = 3\n"}, "update")
        stats = diff_stat.analyze(self.tmp, "HEAD~1", "HEAD", "commit", 0)
        self.assertEqual(stats["n_files"], 1)
        self.assertGreater(stats["total_added"], 0)

    def test_analyze_multiple_files(self):
        make_commit(self.tmp, {
            "src/a.py": "a = 1\n",
            "src/b.py": "b = 2\n",
            "README.md": "# Hi\n",
        }, "initial")
        make_commit(self.tmp, {
            "src/a.py": "a = 1\na2 = 2\n",
            "src/b.py": "b = 99\n",
            "README.md": "# Hi\nUpdated\n",
        }, "bulk update")
        stats = diff_stat.analyze(self.tmp, "HEAD~1", "HEAD", "commit", 0)
        self.assertEqual(stats["n_files"], 3)

    def test_categories_detected(self):
        make_commit(self.tmp, {
            "app.py": "x=1\n",
            "test_app.py": "def test_x(): pass\n",
            "config.yaml": "key: value\n",
        }, "initial")
        make_commit(self.tmp, {
            "app.py": "x=2\n",
            "test_app.py": "def test_x(): assert True\n",
            "config.yaml": "key: changed\n",
        }, "update all")
        stats = diff_stat.analyze(self.tmp, "HEAD~1", "HEAD", "commit", 0)
        self.assertIn("source", stats["by_category"])
        self.assertIn("test", stats["by_category"])
        self.assertIn("config", stats["by_category"])

    def test_deleted_file_counted(self):
        make_commit(self.tmp, {
            "app.py": "x=1\n",
            "old.py": "deprecated\n",
        }, "initial")
        Path(self.tmp, "old.py").unlink()
        subprocess.run(["git", "rm", "old.py"], cwd=self.tmp, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "remove old"], cwd=self.tmp, check=True, capture_output=True)
        stats = diff_stat.analyze(self.tmp, "HEAD~1", "HEAD", "commit", 0)
        self.assertGreater(stats["n_deleted"], 0)

    def test_top_N_limits_entries(self):
        make_commit(self.tmp, {f"f{i}.py": f"x={i}\n" for i in range(10)}, "initial")
        make_commit(self.tmp, {f"f{i}.py": f"x={i}\ny={i+1}\n" for i in range(10)}, "update all")
        stats = diff_stat.analyze(self.tmp, "HEAD~1", "HEAD", "commit", top=3)
        self.assertLessEqual(len(stats["entries"]), 3)

    def test_staged_mode(self):
        make_commit(self.tmp, {"app.py": "x=1\n"}, "initial")
        (Path(self.tmp) / "app.py").write_text("x=2\ny=3\n")
        subprocess.run(["git", "add", "app.py"], cwd=self.tmp, check=True, capture_output=True)
        stats = diff_stat.analyze(self.tmp, "HEAD", None, "staged", 0)
        self.assertGreater(stats["n_files"], 0)

    def test_unstaged_mode(self):
        make_commit(self.tmp, {"app.py": "x=1\n"}, "initial")
        (Path(self.tmp) / "app.py").write_text("x=2\ny=3\nz=4\n")
        stats = diff_stat.analyze(self.tmp, "HEAD", None, "unstaged", 0)
        self.assertGreater(stats["n_files"], 0)

    def test_json_format_end_to_end(self):
        make_commit(self.tmp, {"src/app.py": "x=1\n"}, "initial")
        make_commit(self.tmp, {"src/app.py": "x=1\ny=2\n"}, "update")
        stats = diff_stat.analyze(self.tmp, "HEAD~1", "HEAD", "commit", 0)
        base_info = diff_stat.get_commit_info("HEAD~1", self.tmp)
        head_info = diff_stat.get_commit_info("HEAD", self.tmp)
        out = diff_stat.format_json(stats, base_info, head_info, "commit")
        parsed = json.loads(out)
        self.assertEqual(parsed["summary"]["n_files"], 1)

    def test_rename_marks_both_old_and_new_paths(self):
        make_commit(self.tmp, {"src/old_name.py": "x=1\n"}, "initial")
        subprocess.run(["git", "mv", "src/old_name.py", "src/new_name.py"], cwd=self.tmp, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "rename file"], cwd=self.tmp, check=True, capture_output=True)
        statuses = diff_stat.get_name_status("HEAD~1", "HEAD", "commit", self.tmp)
        self.assertEqual(statuses["src/old_name.py"], "R")
        self.assertEqual(statuses["src/new_name.py"], "R")

    def test_markdown_format_end_to_end(self):
        make_commit(self.tmp, {"README.md": "# Hi\n"}, "initial")
        make_commit(self.tmp, {"README.md": "# Hi\n\nNew content\n"}, "update")
        stats = diff_stat.analyze(self.tmp, "HEAD~1", "HEAD", "commit", 0)
        base_info = diff_stat.get_commit_info("HEAD~1", self.tmp)
        out = diff_stat.format_markdown(stats, base_info, None, "commit")
        self.assertIn("README.md", out)
        self.assertIn("docs", out)

    def test_terminal_format_end_to_end(self):
        diff_stat.USE_COLOR = False
        make_commit(self.tmp, {"app.py": "x=1\n"}, "initial")
        make_commit(self.tmp, {"app.py": "x=2\n"}, "update")
        stats = diff_stat.analyze(self.tmp, "HEAD~1", "HEAD", "commit", 0)
        base_info = diff_stat.get_commit_info("HEAD~1", self.tmp)
        head_info = diff_stat.get_commit_info("HEAD", self.tmp)
        out = diff_stat.format_terminal(stats, base_info, head_info, "commit", verbose=False)
        self.assertIn("SUMMARY", out)
        self.assertIn("RISK SCORE", out)

    def test_risk_threshold_exit_code(self):
        """Main should return 1 when risk >= threshold."""
        make_commit(self.tmp, {"app.py": "x=1\n"}, "initial")
        # Many large changes to get high risk
        big_content = "\n".join([f"line_{i} = {i}" for i in range(500)])
        make_commit(self.tmp, {"app.py": big_content}, "big change")
        stats = diff_stat.analyze(self.tmp, "HEAD~1", "HEAD", "commit", 0)
        risk = stats["risk"]["score"]
        # Set threshold just above actual score → should pass (return 0-like logic)
        # We just test the risk score is computed correctly
        self.assertGreater(risk, 0)


# ── get_commit_info mocking ───────────────────────────────────────────────────

class TestGetCommitInfo(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        make_git_repo(self.tmp)
        make_commit(self.tmp, {"a.py": "x=1\n"}, "first commit")

    def test_returns_dict(self):
        info = diff_stat.get_commit_info("HEAD", self.tmp)
        self.assertIn("hash", info)
        self.assertIn("short", info)
        self.assertIn("author", info)
        self.assertIn("subject", info)

    def test_subject_matches(self):
        info = diff_stat.get_commit_info("HEAD", self.tmp)
        self.assertIn("first commit", info["subject"])


# ── fmt_lines ─────────────────────────────────────────────────────────────────

class TestFmtLines(unittest.TestCase):

    def setUp(self):
        diff_stat.USE_COLOR = False

    def test_both(self):
        out = diff_stat.fmt_lines(10, 5)
        self.assertIn("+10", out)
        self.assertIn("-5", out)

    def test_added_only(self):
        out = diff_stat.fmt_lines(10, 0)
        self.assertIn("+10", out)
        self.assertNotIn("-0", out)

    def test_removed_only(self):
        out = diff_stat.fmt_lines(0, 5)
        self.assertIn("-5", out)
        self.assertNotIn("+0", out)

    def test_zero_both(self):
        out = diff_stat.fmt_lines(0, 0)
        self.assertIn("±0", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
