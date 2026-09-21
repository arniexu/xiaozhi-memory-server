---
name: dispatch-dsh-kimicode
description: Dispatch delegated work to the dsh (DeepSeek CLI) and kimicode (Kimi CLI) headless agents - launch commands, permission/sandbox modes, self-contained briefing templates, progress checks, and verifier duties. Use when the user wants to hand a task to dsh or kimi from the shell, run a multi-agent wave, brief a headless worker, or check and collect a headless worker's output.
---

# Dispatch to dsh and kimicode

Delegating a task to `dsh` (DeepSeek CLI) or `kimicode` (`kimi`) means writing a
self-contained briefing, launching a one-shot headless process, watching its log,
and then independently verifying its claims. This skill records the commands and
pitfalls verified on this machine. Anything not verified here is marked
**UNVERIFIED** - do not present it as fact.

## When to use

- The user asks to delegate/派活 to `dsh` or `kimi`, or to run a "wave" of parallel workers.
- You need a headless (non-interactive) run that writes a report to a log.
- You are the verifier collecting and checking another agent's output.

## Ground rules

1. **Self-contained briefing.** A headless run starts fresh; it does not see your
   chat. Put every fact it needs (repo path, decisions, allowed files, report
   path) into the prompt.
2. **Fail-closed is a feature.** Under the default sandbox the worker cannot edit
   outside its workspace; expect a patch instead of an in-place edit.
3. **Never trust a self-report.** Re-run the checks yourself before accepting.
4. **No git from workers.** Workers must not commit/stash/checkout; the verifier owns git.

## Part 1 - dsh (DeepSeek CLI)

### Binary and version (verified)

- `which dsh` -> `/home/xuqj/.nvm/versions/node/v22.22.3/bin/dsh`
- `dsh --version` -> `0.1.5-rc.1`
- Profiles live under `DSH_HOME` (`/home/xuqj/.dsh/profiles`); `headless` exists there.

### Launch one worker (verified pattern)

```bash
cd <repo>
DSH_PERMISSION_MODE=<mode> nohup dsh --profile headless "$(cat context.md brief.md)" \
  > /tmp/<wave>-<role>.log 2>&1 &
echo $!        # record the PID; pgrep is unreliable (see below)
```

- `dsh --profile headless "<prompt>"` = "answer one task, print the result, and exit".
- Convention (not enforced by dsh): put logs under `/tmp/<wave>/` or `/tmp/<wave>-<role>.log`.
- One role = one process; workers do not talk to each other. That isolation is the point.

### Permission / sandbox modes (verified via `--dump-config`)

`dsh --profile headless --dump-config` shows:

```
- id: sandbox-policy
  config:
    mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
    workspaceRoot: !!js process.cwd()
- id: approval
  config:
    policy: !!js >-
      (process.env.DSH_PERMISSION_MODE ?? 'workspace-write') ===
      'danger-full-access' ? 'never' : 'ask'
- id: permission
  config:
    presets:
      read-only:         {sandbox: read-only,          approval: ask}
      workspace-write:   {sandbox: workspace-write,    approval: ask}
      danger-full-access:{sandbox: danger-full-access, approval: never}
```

So `workspaceRoot` is the **current working directory** at launch. `cd` into the
repo (or a scratch dir) before starting.

| Mode | Writes | Approval | Use for |
|---|---|---|---|
| `read-only` | none | ask | review / audit |
| `workspace-write` (default) | session workspace only, plus some platform temp areas | ask | patch-only or in-dir work |
| `danger-full-access` | unrestricted (sandbox off) | never | workers that must edit an external repo/system |

### Live verification of `workspace-write` (this session)

Workspace was `/tmp/skills/dsh-probe2` (cwd), yet the write tool was asked to
create `/home/xuqj/dsh-outside-probe.txt`:

```
Error: [sandbox: file access denied under workspace-write mode]
[sandbox: escalation available — retry this exact operation once with sandbox_permissions (the narrowest wider mode that suffices) + justification; the approval prompt asks the user]
```

The target file was **not** created. When a worker actually retries the
escalation with no approver attached, prior practice logs report:

```
requires approval, but no approval channel is available      # /tmp/mvp/m1.log:1377, m2.log:1529
```

i.e. **fail-closed**: no channel -> no escalation -> the worker cannot edit that
path and must emit a patch. Real precedent: `/tmp/mvp/m1.patch`, `/tmp/mvp/m2.patch`.

Nuance verified the same session: a write tool call to `/tmp/skills/dsh-outside.txt`
(also outside cwd, but under `/tmp`) reported `Created file`, while a `bash ls`
inside the *same* worker reported `No such file or directory`; after the process
exited the file did exist. Lesson: `/tmp` is a platform temp area that may be
writable even outside cwd, and **the worker's bash view can diverge from what the
write tool actually persisted**. Do not trust an in-worker `ls` to confirm a
write-tool effect, and do not assume `/tmp` contains a worker.

### Progress checks (verified, with a caveat)

```bash
wc -c <log>          # is the log growing?
tail -c 400 <log>    # tail bytes, not lines (headless streams progress)
kill -0 <pid> && echo alive   # use the PID captured at launch
```

**`pgrep -fc 'dsh --profile headless'` is deliberately avoided.** Even with no
worker running it returned `2` on this machine: it matched the *current dsh
session* (you may yourself be a dsh worker) and the `pgrep` command's own shell
line. `pgrep -f ... | wc -l` (the playbook's suggestion) has the same flaw. Check
the PID you recorded instead.

### dsh pitfalls (verified / marked)

- **No resume.** `headless` is one-shot; there is no session to resume. Briefing
  must be self-contained. (The launcher has `--resume` for other apps, but the
  headless profile is "answer one task and exit".)
- **Logs in `/tmp` are conventionally where reports land**, but that is a team
  convention, not a dsh enforcement; confirm the path was actually written.
- **Quota exhaustion (HTTP 402) interrupts a run** - **UNVERIFIED in this
  session** (reported by prior practice; no 402 observed in `/tmp/mvp/*.log`).
  If a run dies early with no report, check the log for an API/quota error.
- The UNDICI `EnvHttpProxyAgent is experimental` warning on stderr is noise.

## Part 2 - kimicode (Kimi CLI)

### Binary and version (verified)

- Full path: `/home/xuqj/.kimi-code/bin/kimi` - **NOT on `PATH`**; always call by
  full path (or add it to `PATH`).
- `~/.kimi-code/bin/kimi --version` (or first line of `-p` output) -> `0.42.0`.

### Non-interactive one-shot (verified end to end)

`-p, --prompt <prompt>` = "Run one prompt non-interactively and print the response."

```bash
/home/xuqj/.kimi-code/bin/kimi -p "<self-contained task>"
```

Real probe run this session:

```bash
/home/xuqj/.kimi-code/bin/kimi -p \
  "Create the file /tmp/skills/kimi-probe.txt containing exactly the single line: \
hello-from-kimi. Then read it back with cat and show its contents."
```

Output (abridged, exit code 0):

```
kimi version 0.42.0
• Simple task: write file, cat it.hello-from-kimi
• Done. Created `/tmp/skills/kimi-probe.txt` and read it back with `cat` — contents:
  hello-from-kimi
To resume this session: kimi -r session_dbd5206d-3293-4c29-84c2-76be56ebc24c
```

Independently confirmed: `cat /tmp/skills/kimi-probe.txt` -> `hello-from-kimi`.

### Permission flags do NOT combine with `-p` (verified)

Both of these fail immediately:

```
kimi -p "..." --auto     -> error: Cannot combine --prompt with --auto.
kimi -p "..." -y/--yolo  -> error: Cannot combine --prompt with --yolo.
```

So in prompt mode you cannot pick a permission mode; the probe's routine file
write ran without an interactive prompt. `--auto`/`--yolo` exist only for the
interactive TUI. **UNVERIFIED:** what sandbox (if any) `-p` mode applies, and how
it behaves on a denied action or an exhausted quota.

### Structured progress output (verified)

```bash
/home/xuqj/.kimi-code/bin/kimi -p "<task>" --output-format stream-json
```

Emits JSON lines, e.g.:

```
{"role":"meta","type":"system.version","version":"0.42.0"}
{"role":"assistant","tool_calls":[{"type":"function","id":"...","function":{"name":"Write","arguments":"{...}"}}]}
{"role":"tool","tool_call_id":"...","content":"Wrote 10 bytes to /tmp/skills/kimi-probe3.txt"}
{"role":"meta","type":"session.resume_hint","session_id":"session_8c5ba25b-...","command":"kimi -r session_8c5ba25b-..."}
```

This is the best way to watch what a `-p` run is doing (parse tool-call lines).

### Resume works (verified) - unlike dsh headless

```bash
kimi -S session_8c5ba25b-ff7d-4559-bfaf-59b5373beb12 -p "What file did you write last turn?"
# -> recalled /tmp/skills/kimi-probe3.txt with content probe3-ok; exit 0
```

The output advertises `kimi -r <id>` as a shortcut; `-r` is **not listed in
`--help`** (the documented flag is `-S, --session [id]`). Use `-S`.

### Other kimi notes (verified from `--help`)

- No `-p` -> interactive TUI; not usable as a background worker.
- `--agent` / `--agent-file` cannot combine with `--session`/`--continue`.
- `--skills-dir <dir>` can point at a skills directory.
- `--add-dir <dir>` adds another workspace directory.
- Commands `acp`, `web`, `server`, `rc` run servers/TUI, not one-shot workers.
- `kimi doctor` validates `config.toml` / `tui.toml` (verified: both OK).

### Choosing dsh vs kimicode

| Need | Use |
|---|---|
| Many isolated parallel workers, in-repo or patch-only | `dsh --profile headless` |
| Continue/steer a specific thread, structured event stream | `kimi -p` + `--output-format stream-json`, then `-S` |
| Full write access to an external repo from a worker | `DSH_PERMISSION_MODE=danger-full-access` (or do the edit yourself) |

## Part 3 - Briefing template

Two files per wave: a shared `context.md` plus one brief per role. Full templates
are in `references/briefing-template.md`; the shape is:

- **context.md** - one-line goal; current state of each subsystem with `file:line`
  pointers to recon notes; the non-negotiable decisions; the discipline list
  (violations = rework).
- **role brief** - role name; repo and read-first files; **allowed file list**
  (exclusive ownership, name each file); forbidden list (no `git`, no services, no
  flashing, experiments only in `/tmp`); deliverable = report path + `file:line`
  evidence + a final marker line such as `M1 DONE <one line>`; environment facts
  (repo path, permission mode).

## Part 4 - Verifier (main controller) duties

From the real playbook at `/home/xuqj/authentication/.github/team/README.md`:

1. **Freeze the baseline before work**: `HEAD`, `git status`, `md5` of files about
   to change -> `/tmp/<wave>/baseline.txt`.
2. **Range check after work**: walk `git status` against the isolation map; catch
   out-of-scope edits. Real incident: a "read-only" reviewer wrote to the real repo.
3. **Never trust self-reports; reproduce every claim.** A claimed fix whose audit
   file was never wired into production was caught only by independent review.
4. **Full regression**, not a sample.
5. **Commit by theme**, with the evidence source (who implemented, who reproduced)
   in the message. Workers do not run git.
6. Label anything asserted without evidence as **inference**, not fact.

## Quick checklist before accepting a worker's result

1. Log exists and ends with the required `DONE` marker.
2. Every claimed `file:line` actually exists and says what was claimed.
3. Re-run the worker's own tests/commands yourself and capture raw output.
4. `git status` shows only the files the brief allowed.
5. For `workspace-write` runs, confirm a patch file was produced (the worker
   could not edit the repo) rather than believing an "edited" claim.
