---
name: pull-request
description: >-
  Frame every pull request: title, body shape, and the evidence that goes in it.
  Use whenever creating or editing a PR (gh pr create / gh pr edit, "open a PR",
  "push this up", "write the PR description") or when asked to review a draft PR
  body. Bodies are demonstrative, not descriptive: lead with the artifact
  (screenshot, GIF, repro output, numbers table), a few lines of prose at most,
  no section templates, no AI footers. Not for: commit messages or reviewing
  someone else's code.
---

# pull-request

**Show, don't tell.** A reviewer should judge the PR from the diff plus one
artifact. Prose covers only what the artifact can't.

## Title

`area: imperative summary`. Lowercase, under ~50 chars. `area` is the
subsystem or directory touched (`ui: fix button overflow`,
`ci: persist ccache across builds`). No `feat(x):` / `fix:` prefixes.

## Body

| Change | Lead with | Then |
|---|---|---|
| UI / visual | screenshot, GIF, or video; before/after table when both exist | 0–2 lines |
| Bug fix / behavior | the repro: command + output, or failing → passing test | 1–3 lines on the cause |
| Perf / size / CI time | numbers table, before → after | one line on what moved them |
| Refactor / mechanical | nothing, or one line proving output is unchanged | — |
| Trivial | one line, or empty | — |

- Context links first, bare: issue URL, `split from #N`, `depends on #N`.
- About five lines of prose total. Needing more means split the PR or pick a
  better artifact.
- No `## What / Why / Testing` headers. Headers only label media in a long PR.
- Test evidence is an artifact trimmed to the lines that prove the point, never
  "tests pass". Skip anything CI already proves (builds, lint, unit tests); show
  what CI can't: device behavior, a repro, numbers, a visual.
- More than ~4 artifacts: keep 1–2 visible, fold the rest in `<details>`.
- Never: AI attribution footers, emoji headers, bold on every noun, bullets that
  restate the diff.
- Author's voice. Lowercase and casual is fine. Short sentences.

## Artifacts

- Visual: capture the real thing. Before and after from the same scenario, labeled.
- Behavior: run the repro on master and on the branch, paste both.
- Numbers: same machine, same scenario, name the baseline commit.
- Upload with `--attach` (gh ≥ 2.100). Reference the local path in the body and
  gh rewrites it to the uploaded URL; unreferenced files are appended. Alt text
  follows `#`. Videos get a bare line, no alt text.

```bash
cat > /tmp/body.md <<'MD'
| Before | After |
|---|---|
| ![Before](./before.png) | ![After](./after.png) |
MD
gh pr create --draft --title "area: summary" --body-file /tmp/body.md \
  --attach ./before.png --attach ./after.png
gh pr edit NUMBER --attach './demo.gif#What the reviewer should notice'
gh pr create --draft --title "area: summary" --body ''      # trivial change
```

If some uploads fail, gh exits non-zero but still creates the PR; re-run
`gh pr edit --attach` for the missing files.

## Before opening

- One concern per PR; split cleanup from behavior.
- Always `--draft` unless the user says it's ready for review.
- Re-read as the reviewer: can they see the change without the diff?
