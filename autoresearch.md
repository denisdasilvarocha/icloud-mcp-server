# Autoresearch: icloud-mcp onboarding

## Objective
icloud-mcp onboarding

## Metrics
- Primary: seconds (s, lower is better)
- Secondary: none yet

## How to Run
`uv run python -m unittest discover -s tests/unit` prints `METRIC name=value` lines.

## Files in Scope
- pyproject.toml
- pytest.ini
- tests
- README.md
- AGENTS.md

## Off Limits
- TBD: add off-limits files or behaviors if needed

## Constraints
- - Decision contract: seconds is the primary metric; secondary evidence explains tradeoffs but should not silently override it.
- Keep public MCP contracts stable; verify unit tests then ruff check then ruff format after code changes.

## Decision Rules
- Keep when the primary metric improves or a baseline is needed and checks pass.
- Discard when the metric is equal or worse, unless the run only establishes the baseline.
- Log crashes and failed checks with a concrete rollback reason.
- Put next-step guidance in ASI so another Codex session can continue.

## Stop Conditions
- Stop when the target metric reaches the agreed threshold.
- For qualitative loops, stop when `quality_gap=0`, checks pass, and no high-impact open finding remains.
- Stop when maxIterations is reached or the user interrupts.

## Research Notes
- Source-backed facts, contradictions, and open questions go here or in linked scratchpad files.
- For deep research loops, link the scratchpad folder and summarize the current synthesis.

## What's Been Tried
- Baseline: pending

## Resume This Session

Use these commands to pick the loop back up without rediscovering state:

```bash
node "/Users/denis/.codex/plugins/cache/thegreencedar-autoresearch/codex-autoresearch/1.1.11/scripts/autoresearch.mjs" state --cwd "/Users/denis/Documents/Git/icloud-mcp-server"
node "/Users/denis/.codex/plugins/cache/thegreencedar-autoresearch/codex-autoresearch/1.1.11/scripts/autoresearch.mjs" doctor --cwd "/Users/denis/Documents/Git/icloud-mcp-server" --check-benchmark
node "/Users/denis/.codex/plugins/cache/thegreencedar-autoresearch/codex-autoresearch/1.1.11/scripts/autoresearch.mjs" next --cwd "/Users/denis/Documents/Git/icloud-mcp-server"
node "/Users/denis/.codex/plugins/cache/thegreencedar-autoresearch/codex-autoresearch/1.1.11/scripts/autoresearch.mjs" log --cwd "/Users/denis/Documents/Git/icloud-mcp-server" --from-last --status keep --description "Describe the kept change"
node "/Users/denis/.codex/plugins/cache/thegreencedar-autoresearch/codex-autoresearch/1.1.11/scripts/autoresearch.mjs" export --cwd "/Users/denis/Documents/Git/icloud-mcp-server"
```

- Run 1 keep: Baseline for icloud-mcp onboarding autoresearch setup; metric=2.772; best=2.772; commit=d0fb466; Git: committed d0fb466..
