#!/usr/bin/env python3
"""
diff-stat: Intelligent git diff summarizer.

Analyzes what changed between two git refs and produces a structured summary
grouped by file type, module/directory, and author. Includes a risk score
to help prioritize review effort.

Usage:
  diff-stat [options] [<base>] [<head>]
  diff-stat --help

Arguments:
  base    Base ref (commit, tag, branch). Default: HEAD~1
  head    Head ref. Default: HEAD (or working tree if --unstaged)

Options:
  -r, --repo PATH       Path to git repo [default: current dir]
  -f, --format FORMAT   Output format: terminal, json, markdown [default: terminal]
  -o, --output FILE     Write output to file instead of stdout
  --top N               Show only top N files by change size [default: 0 (all)]
  --unstaged            Compare working tree (unstaged changes) vs HEAD
  --staged              Compare staging area vs HEAD
  --no-color            Disable colored terminal output
  --risk-threshold N    Exit 1 if risk score >= N (for CI use) [default: 0]
  -v, --verbose         Show per-file details in terminal output
  -h, --help            Show this help message
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── ANSI colors ──────────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"
BRIGHT_RED = "\033[91m"
BRIGHT_GREEN = "\033[92m"
BRIGHT_YELLOW = "\033[93m"
BRIGHT_BLUE = "\033[94m"

USE_COLOR = True


def c(code: str, text: str) -> str:
    """Wrap text in ANSI escape code if color is enabled."""
    if not USE_COLOR:
        return text
    return f"{code}{text}{RESET}"


# ── File categorization ───────────────────────────────────────────────────────

FILE_CATEGORIES = {
    "source": {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".rb", ".java",
        ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
        ".ex", ".exs", ".hs", ".clj", ".cljs", ".lua", ".php", ".r", ".jl",
        ".sh", ".bash", ".zsh", ".fish", ".ps1",
    },
    "test": {
        # detected by naming patterns below
    },
    "config": {
        ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env",
        ".json", ".xml", ".properties", ".gradle",
    },
    "docs": {
        ".md", ".rst", ".txt", ".adoc", ".org", ".tex",
    },
    "infra": {
        ".tf", ".hcl", ".dockerfile", "",  # Dockerfile handled by name
    },
    "data": {
        ".sql", ".csv", ".tsv", ".parquet", ".ndjson",
    },
    "web": {
        ".html", ".htm", ".css", ".scss", ".sass", ".less", ".svg",
    },
    "build": {
        ".lock", ".sum",  # go.sum, package-lock, etc.
    },
}

LOCK_FILE_NAMES = re.compile(
    r"(package-lock\.json|yarn\.lock|Pipfile\.lock|poetry\.lock|go\.sum|Gemfile\.lock|composer\.lock)",
    re.IGNORECASE,
)

INFRA_NAMES = {"dockerfile", "makefile", "jenkinsfile", "vagrantfile", "procfile"}


def categorize(path: str) -> str:
    """Return category for a file path."""
    name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    norm = path.replace("\\", "/")

    if name in INFRA_NAMES:
        return "infra"
    # Lock files must be checked before generic .json config match
    if LOCK_FILE_NAMES.search(name):
        return "build"
    # Test detection: filename patterns + directory structure
    if re.search(r"(test_|_test\.|[._]test\.|[._]spec\.)", name, re.IGNORECASE):
        return "test"
    if re.search(r"/(tests?|spec)/", norm, re.IGNORECASE):
        return "test"
    if norm.startswith(("test/", "tests/", "spec/")):
        return "test"
    # No extension -> other (infra names already handled above)
    if not suffix:
        return "other"

    for cat, exts in FILE_CATEGORIES.items():
        if cat in ("test", "build", "infra"):
            continue
        if suffix and suffix in exts:
            return cat

    # Fallback infra by extension
    if suffix in FILE_CATEGORIES.get("infra", set()) and suffix:
        return "infra"

    return "other"


def top_module(path: str, depth: int = 2) -> str:
    """Return the top-level module/directory (up to `depth` levels)."""
    parts = Path(path).parts
    # Root file — no directory
    if len(parts) <= 1:
        return "."
    # Drop the filename (last part), keep directory parts up to `depth`
    dir_parts = parts[:-1]
    return str(Path(*dir_parts[:depth]))


# ── Git helpers ───────────────────────────────────────────────────────────────

def git(args: list[str], cwd: str = ".") -> str:
    """Run a git command and return stdout (raises on failure)."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def resolve_ref(ref: str, cwd: str) -> str:
    """Resolve a ref to a full SHA."""
    return git(["rev-parse", "--short", ref], cwd=cwd).strip()


def get_commit_info(ref: str, cwd: str) -> dict:
    """Return metadata for a commit ref."""
    fmt = "%H%n%h%n%an%n%ae%n%ci%n%s"
    out = git(["log", "-1", f"--format={fmt}", ref], cwd=cwd).strip().split("\n")
    if len(out) < 6:
        return {"hash": ref, "short": ref, "author": "?", "email": "?", "date": "?", "subject": "?"}
    return {
        "hash": out[0],
        "short": out[1],
        "author": out[2],
        "email": out[3],
        "date": out[4],
        "subject": out[5],
    }


def get_numstat(base: str, head: Optional[str], mode: str, cwd: str) -> list[dict]:
    """Return list of {added, removed, path} dicts from git diff --numstat."""
    if mode == "unstaged":
        args = ["diff", "--numstat"]
    elif mode == "staged":
        args = ["diff", "--staged", "--numstat"]
    elif head:
        args = ["diff", "--numstat", base, head]
    else:
        args = ["diff", "--numstat", f"{base}^", base]

    out = git(args, cwd=cwd).strip()
    if not out:
        return []

    results = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_s, removed_s, path = parts
        # Binary files show "-"
        added = int(added_s) if added_s != "-" else 0
        removed = int(removed_s) if removed_s != "-" else 0
        results.append({"added": added, "removed": removed, "path": path})
    return results


def get_name_status(base: str, head: Optional[str], mode: str, cwd: str) -> dict[str, str]:
    """Return {path: status} where status is A/M/D/R/C."""
    if mode == "unstaged":
        args = ["diff", "--name-status"]
    elif mode == "staged":
        args = ["diff", "--staged", "--name-status"]
    elif head:
        args = ["diff", "--name-status", base, head]
    else:
        args = ["diff", "--name-status", f"{base}^", base]

    out = git(args, cwd=cwd).strip()
    result = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        status = parts[0][0]  # First char: A/M/D/R/C
        path = parts[-1]  # Last field is destination path
        result[path] = status
    return result


def get_author_stats(base: str, head: str, cwd: str) -> dict[str, dict]:
    """Return per-author line counts between two refs."""
    try:
        out = git(["log", "--numstat", f"--format=%an", f"{base}..{head}"], cwd=cwd)
    except RuntimeError:
        return {}

    authors: dict[str, dict] = defaultdict(lambda: {"added": 0, "removed": 0, "commits": 0})
    current_author = None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 3:
            try:
                a = int(parts[0]) if parts[0] != "-" else 0
                r = int(parts[1]) if parts[1] != "-" else 0
                authors[current_author]["added"] += a
                authors[current_author]["removed"] += r
            except ValueError:
                pass
        elif len(parts) == 1 and "\t" not in line:
            # Could be author name
            if not line[0].isdigit():
                current_author = line
                authors[current_author]["commits"] += 1
    return dict(authors)


def count_commits(base: str, head: str, cwd: str) -> int:
    """Count commits between base..head."""
    try:
        out = git(["rev-list", "--count", f"{base}..{head}"], cwd=cwd)
        return int(out.strip())
    except (RuntimeError, ValueError):
        return 0


# ── Risk scoring ──────────────────────────────────────────────────────────────

def compute_risk(stats: dict) -> dict:
    """
    Compute a 0–100 risk score based on:
    - Total churn (lines changed)
    - Number of files changed
    - Mix of categories (infra/config changes are riskier)
    - Presence of deletions
    - Number of modules touched
    """
    total_churn = stats["total_added"] + stats["total_removed"]
    n_files = stats["n_files"]
    n_deleted = stats["n_deleted"]
    n_modules = stats["n_modules"]

    by_cat = stats["by_category"]
    infra_files = by_cat.get("infra", {}).get("files", 0)
    config_files = by_cat.get("config", {}).get("files", 0)
    source_files = by_cat.get("source", {}).get("files", 0)
    test_files = by_cat.get("test", {}).get("files", 0)

    # Component scores (each 0-100, then weighted)
    churn_score = min(100, math.log1p(total_churn) / math.log1p(5000) * 100)
    file_score = min(100, math.log1p(n_files) / math.log1p(100) * 100)
    module_score = min(100, math.log1p(n_modules) / math.log1p(20) * 100)
    deletion_score = min(100, math.log1p(n_deleted) / math.log1p(20) * 100)
    risky_files = infra_files + config_files
    risky_score = min(100, math.log1p(risky_files) / math.log1p(10) * 100)
    # Test coverage ratio: penalize if many source changes with few tests
    if source_files > 0:
        test_ratio = test_files / source_files
        test_penalty = max(0, 40 * (1 - test_ratio))
    else:
        test_penalty = 0

    # Weighted composite
    raw = (
        churn_score * 0.30
        + file_score * 0.20
        + module_score * 0.15
        + deletion_score * 0.10
        + risky_score * 0.15
        + test_penalty * 0.10
    )

    score = min(100, round(raw))

    if score >= 75:
        level = "HIGH"
        level_color = BRIGHT_RED
    elif score >= 45:
        level = "MEDIUM"
        level_color = BRIGHT_YELLOW
    elif score >= 20:
        level = "LOW"
        level_color = BRIGHT_GREEN
    else:
        level = "MINIMAL"
        level_color = GREEN

    factors = []
    if churn_score >= 50:
        factors.append(f"high churn ({total_churn:,} lines)")
    if risky_files:
        factors.append(f"{risky_files} infra/config file(s) touched")
    if n_deleted:
        factors.append(f"{n_deleted} file(s) deleted")
    if n_modules >= 5:
        factors.append(f"changes spread across {n_modules} modules")
    if test_penalty >= 20:
        factors.append("low test coverage for source changes")
    if not factors:
        factors.append("routine change")

    return {
        "score": score,
        "level": level,
        "level_color": level_color,
        "factors": factors,
        "components": {
            "churn": round(churn_score),
            "files": round(file_score),
            "modules": round(module_score),
            "deletions": round(deletion_score),
            "risky_files": round(risky_score),
            "test_penalty": round(test_penalty),
        },
    }


# ── Analysis ──────────────────────────────────────────────────────────────────

@dataclass
class FileEntry:
    path: str
    added: int
    removed: int
    status: str  # A/M/D/R/C
    category: str
    module: str

    @property
    def churn(self) -> int:
        return self.added + self.removed


def analyze(
    repo: str,
    base: str,
    head: Optional[str],
    mode: str,
    top: int,
) -> dict:
    """Run full analysis and return structured stats dict."""
    numstat = get_numstat(base, head, mode, repo)
    name_status = get_name_status(base, head, mode, repo)

    entries: list[FileEntry] = []
    for row in numstat:
        path = row["path"]
        entries.append(FileEntry(
            path=path,
            added=row["added"],
            removed=row["removed"],
            status=name_status.get(path, "M"),
            category=categorize(path),
            module=top_module(path),
        ))

    # Sort by churn descending
    entries.sort(key=lambda e: e.churn, reverse=True)
    if top:
        entries = entries[:top]

    total_added = sum(e.added for e in entries)
    total_removed = sum(e.removed for e in entries)
    n_files = len(entries)
    n_added = sum(1 for e in entries if e.status == "A")
    n_deleted = sum(1 for e in entries if e.status == "D")
    n_modified = sum(1 for e in entries if e.status == "M")
    n_renamed = sum(1 for e in entries if e.status in ("R", "C"))

    # Group by category
    by_cat: dict[str, dict] = defaultdict(lambda: {"files": 0, "added": 0, "removed": 0})
    for e in entries:
        by_cat[e.category]["files"] += 1
        by_cat[e.category]["added"] += e.added
        by_cat[e.category]["removed"] += e.removed

    # Group by module
    by_mod: dict[str, dict] = defaultdict(lambda: {"files": 0, "added": 0, "removed": 0})
    for e in entries:
        by_mod[e.module]["files"] += 1
        by_mod[e.module]["added"] += e.added
        by_mod[e.module]["removed"] += e.removed

    n_modules = len(by_mod)

    stats = {
        "total_added": total_added,
        "total_removed": total_removed,
        "total_churn": total_added + total_removed,
        "n_files": n_files,
        "n_added": n_added,
        "n_deleted": n_deleted,
        "n_modified": n_modified,
        "n_renamed": n_renamed,
        "n_modules": n_modules,
        "by_category": dict(by_cat),
        "by_module": dict(by_mod),
        "entries": entries,
    }

    stats["risk"] = compute_risk(stats)
    return stats


# ── Output formatters ─────────────────────────────────────────────────────────

def bar(value: int, max_val: int, width: int = 20, char: str = "█") -> str:
    if max_val == 0:
        return ""
    filled = round(value / max_val * width)
    return char * filled + "░" * (width - filled)


def fmt_lines(added: int, removed: int) -> str:
    parts = []
    if added:
        parts.append(c(BRIGHT_GREEN, f"+{added:,}"))
    if removed:
        parts.append(c(RED, f"-{removed:,}"))
    return " ".join(parts) if parts else c(DIM, "±0")


def format_terminal(
    stats: dict,
    base_info: dict,
    head_info: Optional[dict],
    mode: str,
    verbose: bool,
) -> str:
    lines = []
    W = 70

    # Header
    lines.append(c(BOLD + CYAN, "━" * W))
    lines.append(c(BOLD + CYAN, "  diff-stat  —  Git Change Summary"))
    lines.append(c(BOLD + CYAN, "━" * W))
    lines.append("")

    # Refs
    if mode == "unstaged":
        lines.append(f"  {c(BOLD, 'Scope:')} Working tree (unstaged)")
    elif mode == "staged":
        lines.append(f"  {c(BOLD, 'Scope:')} Staging area (staged)")
    else:
        base_ref = f"{base_info['short']}  {c(DIM, base_info['subject'][:50])}"
        lines.append(f"  {c(BOLD, 'Base:')} {base_ref}")
        if head_info:
            head_ref = f"{head_info['short']}  {c(DIM, head_info['subject'][:50])}"
            lines.append(f"  {c(BOLD, 'Head:')} {head_ref}")
    lines.append("")

    # Summary bar
    lines.append(c(BOLD, "  SUMMARY"))
    lines.append(f"  {'Files changed:':<22} {c(BOLD, str(stats['n_files']))}")
    lines.append(f"  {'  Added:':<22} {c(BRIGHT_GREEN, str(stats['n_added']))}")
    lines.append(f"  {'  Modified:':<22} {c(YELLOW, str(stats['n_modified']))}")
    lines.append(f"  {'  Deleted:':<22} {c(RED, str(stats['n_deleted']))}")
    if stats["n_renamed"]:
        lines.append(f"  {'  Renamed/Copied:':<22} {c(BLUE, str(stats['n_renamed']))}")
    added_str = f"+{stats['total_added']:,}"
    removed_str = f"-{stats['total_removed']:,}"
    net = stats['total_added'] - stats['total_removed']
    net_str = f"{net:+,}"
    lines.append(f"  {'Lines added:':<22} {c(BRIGHT_GREEN, added_str)}")
    lines.append(f"  {'Lines removed:':<22} {c(RED, removed_str)}")
    lines.append(f"  {'Net change:':<22} {c(CYAN, net_str)}")
    lines.append("")

    # Risk
    risk = stats["risk"]
    score = risk["score"]
    level = risk["level"]
    lc = risk["level_color"]
    risk_bar = bar(score, 100, width=30)
    lines.append(c(BOLD, "  RISK SCORE"))
    lines.append(f"  {c(lc, f'{score}/100')}  {c(lc, level)}")
    lines.append(f"  {c(lc, risk_bar)}")
    lines.append(f"  {c(DIM, 'Factors: ' + '; '.join(risk['factors']))}")
    lines.append("")

    # By category
    lines.append(c(BOLD, "  BY FILE TYPE"))
    cat_order = ["source", "test", "config", "infra", "docs", "web", "data", "build", "other"]
    cat_colors = {
        "source": BRIGHT_BLUE,
        "test": BRIGHT_GREEN,
        "config": YELLOW,
        "infra": MAGENTA,
        "docs": CYAN,
        "web": BLUE,
        "data": WHITE,
        "build": DIM,
        "other": DIM,
    }
    max_cat_churn = max(
        (v["added"] + v["removed"] for v in stats["by_category"].values()),
        default=1,
    )
    for cat in cat_order:
        if cat not in stats["by_category"]:
            continue
        d = stats["by_category"][cat]
        churn = d["added"] + d["removed"]
        b = bar(churn, max_cat_churn, width=15)
        cc = cat_colors.get(cat, WHITE)
        lines.append(
            f"  {c(cc, f'{cat:<10}')}  {b}  "
            f"{d['files']:>3} file(s)  {fmt_lines(d['added'], d['removed'])}"
        )
    lines.append("")

    # By module
    if stats["by_module"]:
        lines.append(c(BOLD, "  BY MODULE / DIRECTORY"))
        sorted_mods = sorted(
            stats["by_module"].items(),
            key=lambda kv: kv[1]["added"] + kv[1]["removed"],
            reverse=True,
        )[:10]  # top 10 modules
        max_mod_churn = max(v["added"] + v["removed"] for _, v in sorted_mods) or 1
        for mod, d in sorted_mods:
            churn = d["added"] + d["removed"]
            b = bar(churn, max_mod_churn, width=15)
            lines.append(
                f"  {c(BOLD, f'{mod:<30}')}  {b}  "
                f"{d['files']:>3} file(s)  {fmt_lines(d['added'], d['removed'])}"
            )
        if len(stats["by_module"]) > 10:
            extra_mods = len(stats['by_module']) - 10
            lines.append(f"  {c(DIM, f'... and {extra_mods} more modules')}")
        lines.append("")

    # Top files (verbose or always show top 10)
    if stats["entries"]:
        limit = None if verbose else 10
        shown = stats["entries"][:limit]
        lines.append(c(BOLD, f"  TOP FILES BY CHURN" + ("" if verbose else " (top 10)")))
        status_sym = {"A": c(BRIGHT_GREEN, "A"), "D": c(RED, "D"), "M": c(YELLOW, "M"),
                      "R": c(BLUE, "R"), "C": c(BLUE, "C")}
        max_file_churn = shown[0].churn if shown else 1
        for e in shown:
            sym = status_sym.get(e.status, e.status)
            b = bar(e.churn, max_file_churn, width=12, char="▪")
            cat_label = c(DIM, f"[{e.category}]")
            lines.append(
                f"  {sym}  {b}  {e.path:<50}  {fmt_lines(e.added, e.removed)}  {cat_label}"
            )
        if not verbose and len(stats["entries"]) > 10:
            extra_files = len(stats['entries']) - 10
            lines.append(f"  {c(DIM, f'... and {extra_files} more files (-v to show all)')}")
        lines.append("")

    lines.append(c(DIM, "  " + "─" * (W - 2)))
    lines.append(c(DIM, f"  Generated by diff-stat at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"))
    lines.append("")

    return "\n".join(lines)


def format_markdown(
    stats: dict,
    base_info: dict,
    head_info: Optional[dict],
    mode: str,
) -> str:
    lines = []
    lines.append("# diff-stat: Git Change Summary\n")

    if mode == "unstaged":
        lines.append("**Scope:** Working tree (unstaged)\n")
    elif mode == "staged":
        lines.append("**Scope:** Staging area (staged)\n")
    else:
        lines.append(f"**Base:** `{base_info['short']}` — {base_info['subject']}")
        if head_info:
            lines.append(f"**Head:** `{head_info['short']}` — {head_info['subject']}")
        lines.append("")

    # Summary table
    lines.append("## Summary\n")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Files changed | {stats['n_files']} |")
    lines.append(f"| Added | {stats['n_added']} |")
    lines.append(f"| Modified | {stats['n_modified']} |")
    lines.append(f"| Deleted | {stats['n_deleted']} |")
    lines.append(f"| Lines added | +{stats['total_added']:,} |")
    lines.append(f"| Lines removed | -{stats['total_removed']:,} |")
    lines.append(f"| Net change | {stats['total_added'] - stats['total_removed']:+,} |")
    lines.append("")

    # Risk
    risk = stats["risk"]
    lines.append(f"## Risk Score: {risk['score']}/100 ({risk['level']})\n")
    lines.append("**Factors:**")
    for f in risk["factors"]:
        lines.append(f"- {f}")
    lines.append("")

    # By category
    lines.append("## By File Type\n")
    lines.append("| Category | Files | Added | Removed |")
    lines.append("|----------|-------|-------|---------|")
    for cat, d in sorted(stats["by_category"].items(), key=lambda kv: kv[1]["added"] + kv[1]["removed"], reverse=True):
        lines.append(f"| {cat} | {d['files']} | +{d['added']:,} | -{d['removed']:,} |")
    lines.append("")

    # By module
    if stats["by_module"]:
        lines.append("## By Module / Directory\n")
        lines.append("| Module | Files | Added | Removed |")
        lines.append("|--------|-------|-------|---------|")
        sorted_mods = sorted(stats["by_module"].items(), key=lambda kv: kv[1]["added"] + kv[1]["removed"], reverse=True)[:15]
        for mod, d in sorted_mods:
            lines.append(f"| `{mod}` | {d['files']} | +{d['added']:,} | -{d['removed']:,} |")
        lines.append("")

    # Files
    lines.append("## Files Changed\n")
    lines.append("| Status | File | Added | Removed | Category |")
    lines.append("|--------|------|-------|---------|----------|")
    for e in stats["entries"]:
        lines.append(f"| {e.status} | `{e.path}` | +{e.added:,} | -{e.removed:,} | {e.category} |")
    lines.append("")

    lines.append(f"*Generated by diff-stat at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    return "\n".join(lines)


def format_json(
    stats: dict,
    base_info: dict,
    head_info: Optional[dict],
    mode: str,
) -> str:
    output = {
        "generated_at": datetime.now().isoformat(),
        "mode": mode,
        "refs": {
            "base": base_info,
            "head": head_info,
        },
        "summary": {
            "n_files": stats["n_files"],
            "n_added": stats["n_added"],
            "n_modified": stats["n_modified"],
            "n_deleted": stats["n_deleted"],
            "n_renamed": stats["n_renamed"],
            "total_added": stats["total_added"],
            "total_removed": stats["total_removed"],
            "total_churn": stats["total_churn"],
            "n_modules": stats["n_modules"],
        },
        "risk": {
            "score": stats["risk"]["score"],
            "level": stats["risk"]["level"],
            "factors": stats["risk"]["factors"],
            "components": stats["risk"]["components"],
        },
        "by_category": stats["by_category"],
        "by_module": stats["by_module"],
        "files": [
            {
                "path": e.path,
                "status": e.status,
                "added": e.added,
                "removed": e.removed,
                "churn": e.churn,
                "category": e.category,
                "module": e.module,
            }
            for e in stats["entries"]
        ],
    }
    return json.dumps(output, indent=2)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="diff-stat",
        description="Intelligent git diff summarizer with risk scoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else "",
    )
    p.add_argument("base", nargs="?", default=None, help="Base ref (default: HEAD~1)")
    p.add_argument("head", nargs="?", default=None, help="Head ref (default: HEAD)")
    p.add_argument("-r", "--repo", default=".", help="Path to git repo")
    p.add_argument("-f", "--format", choices=["terminal", "json", "markdown"], default="terminal")
    p.add_argument("-o", "--output", help="Write output to file")
    p.add_argument("--top", type=int, default=0, help="Show only top N files by churn")
    p.add_argument("--unstaged", action="store_true")
    p.add_argument("--staged", action="store_true")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--risk-threshold", type=int, default=0)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    global USE_COLOR

    args = parse_args()

    if args.no_color or not sys.stdout.isatty():
        USE_COLOR = False

    # Output format to file: also disable color unless markdown/json
    if args.output and args.format == "terminal":
        USE_COLOR = False

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(os.path.join(repo, ".git")):
        print(f"Error: {repo} is not a git repository", file=sys.stderr)
        return 1

    # Determine mode
    mode = "commit"
    if args.unstaged:
        mode = "unstaged"
    elif args.staged:
        mode = "staged"

    # Resolve refs
    base = args.base or "HEAD"
    head = args.head

    base_info = {}
    head_info = None

    if mode == "commit":
        try:
            base_info = get_commit_info(base, repo)
            if head:
                head_info = get_commit_info(head, repo)
            else:
                head_info = get_commit_info("HEAD", repo)
                # If base is HEAD and no explicit head, compare HEAD~1..HEAD
                if base == "HEAD" and not args.base:
                    base = "HEAD~1"
                    base_info = get_commit_info(base, repo)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    else:
        try:
            base_info = get_commit_info("HEAD", repo)
        except RuntimeError:
            pass

    try:
        stats = analyze(repo, base, head, mode, args.top)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if stats["n_files"] == 0:
        print("No changes found.", file=sys.stderr)
        return 0

    # Format output
    if args.format == "terminal":
        output = format_terminal(stats, base_info, head_info, mode, args.verbose)
    elif args.format == "markdown":
        output = format_markdown(stats, base_info, head_info, mode)
    elif args.format == "json":
        output = format_json(stats, base_info, head_info, mode)
    else:
        output = format_terminal(stats, base_info, head_info, mode, args.verbose)

    if args.output:
        Path(args.output).write_text(output)
        print(f"Output written to {args.output}", file=sys.stderr)
    else:
        print(output)

    # CI exit code
    if args.risk_threshold and stats["risk"]["score"] >= args.risk_threshold:
        print(
            f"Risk score {stats['risk']['score']} >= threshold {args.risk_threshold} — failing.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
