# diff-stat

Intelligent git diff summarizer with risk scoring.

`diff-stat` analyzes what changed between two git refs and produces a structured, human-readable summary grouped by file type, module/directory, and author — plus a **0–100 risk score** that flags high-impact changes before review.

## Why?

`git diff --stat` gives you a raw file list. `diff-stat` gives you:

- **Grouped view** by file type (source, test, config, infra, docs, …) and by module/directory
- **Risk score** based on churn volume, affected modules, infra changes, deletions, and test coverage ratio
- **Three output formats**: colorized terminal, Markdown (for PRs/wikis), JSON (for CI pipelines)
- **CI integration**: fail the build if risk score exceeds a threshold

## Install

No dependencies beyond Python 3.8+. Just clone and run:

```bash
git clone https://github.com/hex-AI12/diff-stat
cd diff-stat
chmod +x diff_stat.py
```

Optional: symlink to your PATH:
```bash
ln -s "$PWD/diff_stat.py" /usr/local/bin/diff-stat
```

## Usage

```
diff-stat [options] [<base>] [<head>]
```

### Examples

```bash
# Summarize last commit (HEAD~1..HEAD)
diff-stat

# Compare two branches
diff-stat main feature/new-auth

# Summarize staged changes
diff-stat --staged

# Summarize unstaged working-tree changes
diff-stat --unstaged

# Output as Markdown (great for PR descriptions)
diff-stat main HEAD --format markdown

# Output as JSON for CI/scripting
diff-stat v1.2.0 v1.3.0 --format json

# Show all files (not just top 10)
diff-stat -v

# Only top 5 files by churn
diff-stat --top 5

# Fail CI if risk score >= 70
diff-stat --format json --risk-threshold 70

# Write Markdown to file
diff-stat --format markdown --output CHANGES.md
```

## Risk Score

The risk score is a 0–100 composite based on:

| Factor | Weight | What it measures |
|--------|--------|-----------------|
| Churn | 30% | Total lines added + removed |
| Files | 20% | Number of files changed |
| Modules | 15% | How many directories/modules touched |
| Deletions | 10% | Files deleted (high-risk regressions) |
| Infra/Config | 15% | Infrastructure and config file changes |
| Test gap | 10% | Source changes not covered by test changes |

| Score | Level | Recommended action |
|-------|-------|--------------------|
| 0–19 | MINIMAL | Quick scan sufficient |
| 20–44 | LOW | Standard review |
| 45–74 | MEDIUM | Careful review, check regressions |
| 75–100 | HIGH | Deep review, consider staging |

## Output Formats

### Terminal (default)
Colorized, human-readable with progress bars and grouped breakdowns.

### Markdown (`--format markdown`)
Clean table-based output suitable for PR descriptions or wikis.

### JSON (`--format json`)
Full machine-readable output for CI pipelines or further analysis:
```json
{
  "summary": { "n_files": 12, "total_added": 450, ... },
  "risk": { "score": 42, "level": "MEDIUM", "factors": [...] },
  "by_category": { "source": {...}, "test": {...} },
  "by_module": { "src/api": {...} },
  "files": [...]
}
```

## Options

```
positional:
  base                    Base ref (default: HEAD~1)
  head                    Head ref (default: HEAD)

optional:
  -r, --repo PATH         Path to git repo [default: .]
  -f, --format FORMAT     terminal | json | markdown [default: terminal]
  -o, --output FILE       Write output to file
  --top N                 Show only top N files by churn
  --unstaged              Compare working tree vs HEAD
  --staged                Compare staging area vs HEAD
  --no-color              Disable colored output
  --risk-threshold N      Exit 1 if risk score >= N (CI gate)
  -v, --verbose           Show all files, not just top 10
  -h, --help              Show help
```

## File Type Categories

| Category | Detected by |
|----------|-------------|
| source | `.py .js .ts .go .rs .rb .java .c .cpp` + more |
| test | `test_*`, `*_test.*`, `*.spec.*`, paths containing `/test/` or `/spec/` |
| config | `.yml .yaml .toml .json .ini .conf` |
| infra | `Dockerfile`, `Makefile`, `.tf`, `.hcl` |
| docs | `.md .rst .txt .adoc` |
| web | `.html .css .scss .svg` |
| data | `.sql .csv .tsv` |
| build | `package-lock.json`, `yarn.lock`, `go.sum`, etc. |

## Tests

```bash
python3 -m pytest tests/ -v
```

71 tests covering categorization, risk scoring, all output formats, and real git integration.

## License

MIT
