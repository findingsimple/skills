# support-trends — Backlog

Tracked from the April 2026 multi-agent review (ethical-hacker, code-simplifier, test-skeptic, devils-advocate). Tier A + Tier B were addressed in the review session; this file records the deferred items so future sessions don't re-discover them.

## Tier C — Backlog / discuss

### Open

- **#13 — Hoist `_untrusted()` and `_clean()` helpers into `jira_client.py`.** Both helpers are byte-identical across `build_theme_prompt.py`, `build_triage_crossref_prompt.py`, `apply_themes.py`, `apply_triage_crossref.py`, `apply_synthesis.py`. Reviewer: code-simplifier #1, #2. Defer to next consolidation pass.

- **#15 — Sub-agent toolset hardening.** `SKILL.md:208,246` invokes `subagent_type: general-purpose`, which inherits the full Bash/WebFetch/Write toolset and parent-shell env (incl. `JIRA_API_TOKEN`). The "read-only against Jira/GitLab" promise rests entirely on the prompt rules; a successful prompt-injection in an untrusted summary/description could mutate Jira. Reviewer: ethical-hacker #1 (rated HIGH). Mitigation paths: (a) restrict the spawned agent to a tools allow-list (`Bash, Read` only) when the harness supports it, (b) scrub `JIRA_API_TOKEN` / `GITLAB_TOKEN` from the sub-agent env so even a successful jailbreak has no creds. Document in CLAUDE.md "Security Notes" until fixed.

- **#17 — Centralise key/project regexes in `jira_client.py`.** Duplicated across `fetch_triage.py`, `apply_triage_crossref.py`, `apply_themes.py`, `analyze.py`, `fetch.py`. Bundle with #13. Reviewer: code-simplifier #3.

- **#20 — Cross-window charter / triage-coverage consistency. (Jason flagged 2026-04-30 — wants better consistency.)** Charter sub-agent and triage-crossref sub-agent give materially different verdicts when run against overlapping windows for the same team (e.g. April monthly: 5 themes "wrong" charter; same team Feb-Apr quarterly: 1 theme "wrong"; triage `fully:1 → 0` between two consecutive same-window runs). Root cause is sub-agent stochasticity + the longer window having more evidence to push borderline themes from `wrong` → `partial` and `partial` → `fully`. The April 2026-04-30 fix added a per-window caveat note in the charter render so readers see this proactively, but the underlying consistency is still poor. Possible fixes: (a) seed the sub-agent with the previous run's output for the same team as a "prior assessment" hint, similar to themes vocabulary persistence; (b) run the sub-agent N times and majority-vote per theme; (c) post-process to lock a theme verdict once it's been `wrong` for 2+ consecutive runs; (d) cross-reference verdicts when both monthly + quarterly outputs exist for the same team and emit a discrepancy callout. Pick a path and ship it before the next major leadership conversation. Owner: Jason. Priority: medium-high — affects leadership-conversation credibility.

- **#21 — Charter latency / bundle size optimisation.** Charter sub-agent ~100s per run, bundle ~150kB. Pass theme IDs + ticket keys only and have the agent re-read selected tickets on demand instead of pre-bundling enriched ticket records. Defer until charter latency becomes user-felt. Reviewer: performance-profiler 2026-04-30.

- **#22 — Walk-in line confound prefix.** When a strong confound is present (intake-routing shift, reporter concentration, holiday baseline) the volume figure in the walk-in line should carry a `(with confounds — see below)` suffix so a manager skim-reading the top of the report doesn't quote the headline number out of context. Defer; mostly cosmetic. Reviewer: 2026-04-30 self-review.

- **#23 — Synthesis dedupe relaxed predicate.** Current dedupe collapses prompts when evidence Jaccard ≥0.8 AND driver text byte-identical. The 2026-04-30 run added an INFO log for near-misses (Jaccard ≥0.5, distinct text); review the logs across a few runs to decide whether a relaxed predicate (e.g. word-overlap ≥0.7) is worth the false-positive risk of collapsing legitimately-distinct synthesis answers. Defer until the log shows a recurring pattern. Reviewer: 2026-04-30 self-review.

### Closed in follow-up session (April 2026)

- ✅ **#14 — Vocabulary persistence: move to vault + age out stale entries.** Hint file moved from `/tmp/support_trends/themes_vocabulary.json` to `{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Support/Trends/.themes_vocabulary.json`. Each entry carries `last_seen_run` (ISO date) + `count_total_lifetime`. Entries unseen for >90 days are evicted on write. The vault location means the hint survives reboots and is per-team rather than per-machine.

- ✅ **#16 — Refactor `render_themes_triage` into per-theme helpers.** Extracted `_render_triage_coverage_summary()` and `_render_theme_triage_block()` so the section composes from two single-purpose helpers instead of one 60-line function.

- ✅ **#18 — Collapse triage section for leadership readability.** Themes table gained a `Triage` column (🟢 in-flight / 🟡 Backlog / 🟠 adjacent / 🔴 none + headline triage key). The verbose per-theme triage drilldown now lives inside an Obsidian `<details>` block under the section heading — readers get the coverage summary by default, can expand for full reasoning + evidence.

## Tier D — Acknowledged & deliberately skipping

These are recorded so future audits don't re-flag them:

- **Pure style nits** — code-simplifier #4 (unreachable `MAX_TICKETS_TOTAL` belt-and-braces cap), #5 (unused parameter in `_load_window`), #6 (paranoid `or {}` chain), #8 (set-store-mutate pattern in `apply_triage_crossref.py:125-127`), #10 (inconsistent `sys.exit` style), #11 (trivial validator wrappers in `fetch_triage.py:55-60`). No behavioural impact; skip.
- **Code-simplifier #7** — hand-rolled atomic write in `apply_themes.py:175-181`. Functionally identical to the imported `atomic_write_json` helper; cosmetic. Skip until that file is touched for another reason.
- **Code-simplifier #12** — missing `triage.json` silently produces a "no coverage" report. Actually the *correct* behaviour when `TRIAGE_PARENT_ISSUE_KEY` is unset; B7 added the stale-file cleanup to make this safer.
- **Test-skeptic #3, #4, #10, #11** — reviewer self-verified as safe (prior-only theme spike guard, is_new + delta_pct=None interaction, atomic-write race surfacing as #6, theme-coverage prompt zero-count guard).
- **Ethical-hacker #4** — negative/huge counts in `apply_themes.py:110-111`. Sub-agent JSON output is already shape-validated; `max(0,int(...))` is belt-and-braces, not a real risk. Skip.
- **Ethical-hacker #6, #7, #8** — confirmed-safe info findings (JQL injection, path traversal, symlink/TOCTOU). No action.
- **Devils-advocate non-challenges** — 2 extra sub-agent calls (cheap), read-only JQL (correct), adjacent-match noise (self-limited by `MAX_MATCHES_PER_THEME = 6`), keyword-matching alternative to semantic sub-agent (definitions are LLM-generated free text — keyword matcher would be a worse LLM).

## How to read this file

This is a snapshot of review findings, not a live to-do. Items here are deliberately deferred — re-open when their cost-benefit shifts, not just because they're old. New review rounds should add a "Tier ? — <date>" section rather than mutating these entries.
