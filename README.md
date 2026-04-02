# agentctl

A lightweight permissions and quota system for letting AI coding agents submit and manage jobs on HPC clusters via [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

## What this does

Agents get read-only access to cluster state (queues, logs, node status) with no setup. For submitting jobs, they must authenticate with a token and stay within per-agent GPU quotas.

**Read-only tools** (no auth required): `lq`, `ljob`, `lnodes`, `llogs`, `lhist`, `lcat`, `lls`, `lcp`, `lcplog`, `ltransfers`, `lqa`, `qfdata`

**Write tools** (token auth + quota enforcement): `qf-submit`, `qf-cancel`, `qf-sync`

**Admin tools** (requires manual approval): `qf-admin`

## Security model

- **Token authentication** — each agent session gets a cryptographic token (SHA-256 hashed at rest). Agents must pass `--agent-id` and `--token` on every write operation.
- **Quota enforcement** — per-agent, per-cluster GPU limits with atomic reservation-based tracking and file locking.
- **Guard hooks** — Claude Code PreToolUse hooks block direct access to `sessions/` and `config/` directories via Read, Edit, Write, Glob, Grep, NotebookEdit, and Bash tools.
- **Input validation** — all user inputs to SSH commands are validated or quoted to prevent command injection.
- **Job ownership** — agents can only cancel jobs they submitted.

## Setup

1. Add `bin/` to your PATH:
   ```bash
   export PATH="$HOME/CLIs/agentctl/bin:$PATH"
   ```

2. Configure Claude Code hooks by adding to `~/.claude/settings.json`:
   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Read|Edit|Write|Glob|Grep|NotebookEdit",
           "hooks": [{"type": "command", "command": "~/CLIs/agentctl/lib/guard-hook"}]
         },
         {
           "matcher": "Bash",
           "hooks": [
             {"type": "command", "command": "~/CLIs/agentctl/lib/guard-hook-bash"},
             {"type": "command", "command": "~/CLIs/agentctl/lib/enforce-hpc-rules"}
           ]
         }
       ]
     }
   }
   ```

3. Configure SSH aliases (`bp`, `mn`, `is`) in your `~/.ssh/config` to point to your clusters.

## Agent workflow

```bash
# 1. Initialize a session (prints agent-id and token)
qf-init

# 2. Request a quota (requires human approval)
export YES_I_KNOW_THIS_WILL_BE_SLOW=1 && qf-admin quota set <agent-id> <cluster> <gpus>

# 3. Submit jobs
qf-submit --agent-id <id> --token <tok> <cluster> <runner> [args...]

# 4. Check status
lq bp

# 5. Cancel a job
qf-cancel --agent-id <id> --token <tok> <cluster> <job_id>
```

Agents can run `qf-help` to see the full agent guide.

## Project structure

```
bin/            CLI tools (on PATH)
  lq, ljob, lnodes, llogs, ...   read-only cluster tools
  qf-init, qf-submit, qf-cancel  authenticated job management
  qf-admin                        admin tool (quota management)
  qf-help                         prints the agent guide
lib/
  agentctl.py                     shared library (auth, quotas, locking)
  guard-hook                      PreToolUse hook for Read/Edit/Write/Glob/Grep
  guard-hook-bash                 PreToolUse hook for Bash commands
  enforce-hpc-rules               HPC workflow policy enforcement
config/
  quotas.json                     per-agent GPU quotas (gitignored)
  .hpc_projects                   project alias configuration
sessions/
  {agent_id}/                     per-agent session data (gitignored)
    identity.json                 agent ID + token hash
    jobs.json                     job records + pending reservations
tests/
  test_agentctl.py                pytest suite
AGENT_README.md                   guide for agents (shown by qf-help)
```

## How quota enforcement works

Job submission uses a three-phase reservation pattern:

1. **Reserve** — atomically check quota and create a pending entry (under file lock)
2. **Submit** — sync code and submit via SSH (outside lock)
3. **Finalize** — replace pending entry with real job ID (under file lock)

If submission fails, the reservation is cleaned up in a `finally` block. If the process crashes, a PID-based reaper automatically cleans up stale reservations.

## Running tests

```bash
python -m pytest tests/ -v
```
