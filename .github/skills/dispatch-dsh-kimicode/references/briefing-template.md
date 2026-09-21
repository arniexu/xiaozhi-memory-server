# Briefing templates (distilled from real waves)

Source of truth: `/home/xuqj/authentication/.github/team/` and `/tmp/mvp/`.
Two files are concatenated into one prompt by `$(cat context.md brief.md)`.

## 1. context.md (shared, one per wave)

```markdown
# <Wave name> shared context: <one-line goal>

## One sentence
<What the user-visible outcome is.>

## Current state (already reconfirmed; cite file:line, do not re-recon)
- <Subsystem A> at <repo path> (<branch/HEAD>): <facts>; full detail in
  <recon notes path> (read these first).
- <Subsystem B>: <facts>.
- <Subsystem C>: <facts>.

## Non-negotiable decisions (do not deviate)
1. <Decision + why.>
2. <Decision + why.>
3. <Exact contract/keywords/parameters, copied verbatim.>

## Discipline (violations = rework)
- Only edit the files named in your brief; never touch other repos/files.
- No git (no add/commit/stash/checkout/branch); no starting services; no flashing;
  experiments only under /tmp.
- Write your report to /tmp/<wave>/<id>-report.md; every conclusion needs
  file:line evidence; write "UNCONFIRMED" when unsure; fabrication is forbidden.
- Print your completion marker on the final line.
```

## 2. Role brief (one per worker)

```markdown
# <ID> task: <short title>

Repo `<absolute path>` (<fork/branch>). First read `/tmp/<wave>/context.md` and
<contract/docs path>.

**You may only change these files (exclusive ownership):**
1. New file `<path>` - follow the style of `<reference file>`.
2. `<path>` - <what to change>.
3. <...>

## Required behavior
- <Endpoint/signature/contract, with exact types, status codes, error bodies.>
- <Edge cases and concurrency rules.>
- <What must NOT happen.>

## Discipline and self-check
- Do not touch <other repos/dirs>; do not start services.
- Self-check: `<compile/smoke command>`; report raw output.
- Report `/tmp/<wave>/<id>-report.md`: each change with file:line + unverified
  list (especially anything that cannot be exercised locally) + risks.
- Final line: `<ID> DONE <one sentence>`
```

## 3. Report format the verifier expects from every worker

1. **Change list** - file + key line numbers + one-line description.
2. **Verification evidence** - what was run and the **raw output** (never just "passed").
3. **Boundary statement** - what was explicitly not done.
4. **Residual risks** - points needing a human decision.

> No claim without raw evidence. "I did not run it" is acceptable; "it works"
> without output is not.

## 4. Launch examples

```bash
# dsh: patch-only worker under the default sandbox
cd /home/xuqj/xiaozhi-esp32-server
DSH_PERMISSION_MODE=workspace-write nohup dsh --profile headless \
  "$(cat /tmp/mvp/context.md /tmp/mvp/m1-server.md)" \
  > /tmp/mvp/m1.log 2>&1 &
echo $!

# dsh: worker allowed to edit the repo in place
DSH_PERMISSION_MODE=danger-full-access nohup dsh --profile headless \
  "$(cat /tmp/mvp/context.md /tmp/mvp/m1-server.md)" \
  > /tmp/mvp/m1.log 2>&1 &

# kimicode: one-shot, structured events, resumable
/home/xuqj/.kimi-code/bin/kimi -p "$(cat /tmp/mvp/context.md /tmp/mvp/m1-server.md)" \
  --output-format stream-json > /tmp/mvp/m1-kimi.json 2>&1
# continue it later:
/home/xuqj/.kimi-code/bin/kimi -S <session_id> -p "..." 
```
