# diff-stat

Intelligent git diff summarizer with risk scoring.

`diff-stat` analyzes a git diff and answers the useful review questions fast:
- What changed, and how much?
- Which file types and directories absorbed the churn?
- Is this a low-risk cleanup or a high-risk blast radius PR?

Zero dependencies at runtime. Just Python and git.

## Features

- Summarizes diffs by file, file type, and module/directory
- Computes a 0 to 100 risk score with human-readable factors
- Supports commit-to-commit, staged, and unstaged comparisons
- Outputs terminal, Markdown, or JSON
- Ignores generated or vendored paths with repeatable glob filters
- Supports CI gating with `--risk-threshold`
- Works as a single-file script or installable CLI

## Install

### Run directly

```bash
python3 diff_stat.py --help
```

### Install as a CLI

```bash
pip install .
# or for local dev
pip install -e .
```

Then run:

```bash
diff-stat --help
```

## Usage

```bash
# Compare the last commit against its parent
diff-stat

# Compare two refs
diff-stat main HEAD

# Analyze staged changes only
diff-stat --staged

# Ignore generated or vendored paths
diff-stat main HEAD --ignore "dist/*" --ignore "vendor/*"

# Analyze unstaged working tree changes
diff-stat --unstaged

# Emit JSON for automation
diff-stat main HEAD --format json

# Write markdown report to a file
diff-stat main HEAD --format markdown --output report.md

# Fail CI when risk is too high
diff-stat origin/main HEAD --risk-threshold 70
```

## Example terminal output

```text
SUMMARY
  Files changed:          14
  Added:                  3
  Modified:               10
  Deleted:                1
  Lines added:            +482
  Lines removed:          -137
  Net change:             +345

RISK SCORE
  74/100  MEDIUM
  large churn; multiple modules touched; infra/config changes; low test coverage ratio
```

## Output formats

### Terminal
Best for local review. Includes risk bar, file type breakdown, module breakdown, and top files by churn.

### Markdown
Good for PR descriptions, review notes, or attaching to build artifacts.

### JSON
Useful for CI, bots, dashboards, or downstream automation.

## Risk model

The risk score is heuristic, not magic. It considers things like:
- total churn
- number of files changed
- number of directories/modules touched
- deleted files
- infra/config churn
- whether tests changed alongside source changes

Higher score means, “slow down and review this more carefully.”

## Development

```bash
python3 -m pytest -q
```

## License

MIT
