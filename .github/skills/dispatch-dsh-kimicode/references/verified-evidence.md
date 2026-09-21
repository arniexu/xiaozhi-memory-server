# Verified evidence log

Raw commands and output snippets backing the claims in `SKILL.md`. Verified
2026-09-13 on this machine. Anything absent here is **UNVERIFIED**.

## dsh

```
$ dsh --version
0.1.5-rc.1

$ dsh --profile headless --dump-config | sed -n '102,142p'
- id: sandbox
  name: '@deepseek-ai/dsh-sandbox-local'
- id: sandbox-policy
  name: '@deepseek-ai/dsh-sandbox-policy'
  config:
    mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
    workspaceRoot: !!js process.cwd()
- id: approval
  name: '@deepseek-ai/dsh-user-approval'
  config:
    policy: !!js >-
      (process.env.DSH_PERMISSION_MODE ?? 'workspace-write') ===
      'danger-full-access' ? 'never' : 'ask'
- id: permission
  name: '@deepseek-ai/dsh-permission-presets'
  config:
    presets:
      read-only:        {sandbox: read-only,          approval: ask}
      workspace-write:  {sandbox: workspace-write,    approval: ask}
      danger-full-access:{sandbox: danger-full-access, approval: never}
```

`dsh --help` documents headless as: `dsh --profile headless "run the tests"` —
"answer one task, print the result, and exit".

### workspace-write live probe A - write outside cwd under /home

Workspace/cwd = `/tmp/skills/dsh-probe2`; target `/home/xuqj/dsh-outside-probe.txt`:

```
Error: [sandbox: file access denied under workspace-write mode]
[sandbox: escalation available — retry this exact operation once with
 sandbox_permissions (the narrowest wider mode that suffices) + justification;
 the approval prompt asks the user]
```

`ls -l /home/xuqj/dsh-outside-probe.txt` afterwards -> `No such file or directory`.
Not created.

### workspace-write live probe B - write outside cwd under /tmp

Workspace/cwd = `/tmp/skills/dsh-probe`; target `/tmp/skills/dsh-outside.txt`.
Worker write tool returned `Created file`; worker `bash ls` returned
`No such file or directory`; after dsh exited the file existed with content
`probe`. Conclusion: bash view and write-tool persistence can diverge; `/tmp` may
be writable outside cwd.

### Escalation with no approver (prior practice, not re-run this session)

```
/tmp/mvp/m1.log:1377  - 沙箱升级写权限返回 `requires approval, but no approval channel is available`（fail-closed）
/tmp/mvp/m2.log:1529  The escalation requires approval but no approval channel is available.
```

Precedent artifacts: `/tmp/mvp/m1.patch` (24418 bytes), `/tmp/mvp/m2.patch`
(27967 bytes) - workers under workspace-write produced patches instead of edits.

### pgrep pitfall

With no worker running, `pgrep -fc 'dsh --profile headless'` returned `2`:
it matched this session's own dsh process and the pgrep shell line. Use the PID
captured with `$!` (`kill -0 <pid>`) instead.

## kimicode

```
$ /home/xuqj/.kimi-code/bin/kimi --version      # (or first line of -p output)
kimi version 0.42.0

$ /home/xuqj/.kimi-code/bin/kimi -p "Create the file /tmp/skills/kimi-probe.txt
  containing exactly the single line: hello-from-kimi. Then read it back ..."
kimi version 0.42.0
• Done. Created `/tmp/skills/kimi-probe.txt` and read it back with `cat` — contents:
  hello-from-kimi
To resume this session: kimi -r session_dbd5206d-3293-4c29-84c2-76be56ebc24c
# exit 0

$ cat /tmp/skills/kimi-probe.txt
hello-from-kimi
```

Flag conflicts (exit 1 each):

```
$ kimi -p "..." --auto      -> error: Cannot combine --prompt with --auto.
$ kimi -p "..." -y          -> error: Cannot combine --prompt with --yolo.
```

Structured output:

```
$ kimi -p "Write /tmp/skills/kimi-probe3.txt ..." --output-format stream-json
{"role":"meta","type":"system.version","version":"0.42.0"}
{"role":"assistant","tool_calls":[{"type":"function","id":"...","function":{"name":"Write","arguments":"{\"path\":\"/tmp/skills/kimi-probe3.txt\",\"content\":\"probe3-ok\\n\"}"}}]}
{"role":"tool","tool_call_id":"...","content":"Wrote 10 bytes to /tmp/skills/kimi-probe3.txt"}
{"role":"meta","type":"session.resume_hint","session_id":"session_8c5ba25b-...","command":"kimi -r session_8c5ba25b-..."}
```

Resume:

```
$ kimi -S session_8c5ba25b-ff7d-4559-bfaf-59b5373beb24c -p \
  "Which file did you write in the previous turn, and what content?"
• I wrote `/tmp/skills/kimi-probe3.txt` with the content `probe3-ok` (followed by a newline).
# exit 0
```

`kimi doctor` -> `OK config.toml`, `OK tui.toml`, `All checked config files are valid.`
