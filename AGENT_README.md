# agentctl — Agent Guide

You have access to HPC clusters via CLI tools on your PATH. This document
explains what you can do, what you cannot do, and the rules you must follow.

## Clusters

| Alias | Cluster       |
|-------|---------------|
| `bp`  | BluePebble    |
| `mn`  | MareNostrum   |
| `is`  | Isambard      |

## Quick start: reading cluster state (no setup required)

These tools are read-only and available immediately:

```bash
lq                          # Job queues on all clusters
lq bp                       # Queue on BluePebble only
ljob bp                     # Detailed job info
ljob bp 12345               # Specific job
ljob bp -n gpu038           # Jobs on a specific node
lnodes bp                   # GPU node status
lnodes is gpu001            # Specific node
llogs bp 12345              # Fetch stdout log for a job
llogs bp 12345 -e           # Fetch stderr log
llogs bp -f                 # Follow latest job log
lhist bp                    # Recent job history (sacct)
lhist bp 12345              # History for specific job
lcat bp path/to/file        # Display a remote file (relative to work dir)
lls bp                      # List remote files
lls bp logs/ -g pattern     # List with grep filter
lcp bp path/to/file         # Copy remote file locally (to /tmp/)
lcplog bp 12345             # Copy log file locally
ltransfers bp               # Check active rsync/transfers
```

### DPO pair analysis

```bash
lqa bp 12345 --list                # List available epoch files
lqa bp 12345 --summary             # Stats: answer rates, divergence
lqa bp 12345 --show 3              # Show 3 random pairs
lqa bp 12345 --filter no_answer    # Filter pairs
lqa bp 12345 --full 5              # Full transcript of pair at index 5
qfdata summary file.jsonl          # Local JSONL analysis
qfdata sample file.jsonl -n 5      # Sample pairs from local file
```

## Submitting and managing jobs (requires setup)

### Step 1: Initialize your session

Before submitting jobs, you must initialize an agent session:

```bash
qf-init
```

This prints your credentials:
```
agent-id: ab12cd
token: 071a2b01c33fd7c9cdb1ce440f7dfbb777a0dd377218333a343011e6fc3e51a1
```

**Save both values.** You'll pass them as flags to every `qf-submit` and
`qf-cancel` command. Run `qf-init` once — don't keep creating new sessions.

### Step 2: Check your quota

Your quota is set per-agent, per-cluster. If your quota is 0 (the default),
you cannot submit jobs. To request a quota, run:

```bash
export YES_I_KNOW_THIS_WILL_BE_SLOW=1 && qf-admin quota set <your-agent-id> <cluster> <number>
```

This requires manual approval from the user. It will block until they return
to the terminal and approve it, which could take hours. Plan accordingly —
request your quota early, before you need it.

### Step 3: Submit a job

```bash
qf-submit --agent-id <id> --token <token> <cluster> <runner> [args...]
```

Example:
```bash
qf-submit --agent-id ab12cd --token 071a2b...51a1 bp runners/main.py --80g
```

This will:
1. Reserve a quota slot (atomic, prevents over-submission)
2. Sync your code to the cluster via `hpc_utils/copy`
3. Submit the job via `hpc_utils/run`
4. Record the job under your agent ID

You must run this from within the project directory (the one containing
`hpc_utils/copy`).

### Step 4: Cancel a job

```bash
qf-cancel --agent-id <id> --token <token> <cluster> <job_id>
```

You can only cancel jobs you submitted. The job ID must be numeric.

### Sync code without submitting

```bash
qf-sync <cluster>
```

Syncs code to the cluster without submitting a job. Warning: this overwrites
the shared remote directory — if other agents have synced different code, yours
will replace it.

## Quota management

Your quota controls how many concurrent jobs you can have on each cluster.
Quotas are set per-agent, per-cluster.

### Checking your quota

Your current quota and active job count are shown every time you run
`qf-submit`. You can also check the queue directly:

```bash
lq bp    # See all your active jobs on BluePebble
```

### Requesting a quota change

To change your quota, run:

```bash
export YES_I_KNOW_THIS_WILL_BE_SLOW=1 && qf-admin quota set <your_agent_id> <cluster> <number>
```

For example: `export YES_I_KNOW_THIS_WILL_BE_SLOW=1 && qf-admin quota set 158c62 bp 6`

This requires manual approval from the user, which may take hours.

### If your quota is 0

A quota of 0 means you have no permission to submit jobs on that cluster.
This is the default for all new agents. Ask the user to grant you a quota
before attempting to submit.

### If your quota is exceeded

You've hit your concurrent job limit. Options:
1. Wait for a running job to finish (check with `lq <cluster>`)
2. Cancel a job you no longer need with `qf-cancel --agent-id <id> --token <token> <cluster> <job_id>`
3. Request a higher quota with `export YES_I_KNOW_THIS_WILL_BE_SLOW=1 && qf-admin quota set <id> <cluster> <number>`

## Rules and constraints

1. **Quota enforcement**: You cannot submit more jobs than your quota allows
   per cluster. Check your active count in the output of `qf-submit`.

2. **Job ownership**: You can only cancel your own jobs. Attempting to cancel
   another agent's job will fail.

3. **Protected directories**: You cannot directly read or write files in
   `sessions/` or `config/`. Use the `qf-*` tools instead.

4. **Authentication**: All `qf-submit` and `qf-cancel` operations require
   `--agent-id` and `--token` flags. Run `qf-init` once to get your
   credentials, then pass them on every command.

5. **Shared remote directory**: Code syncs overwrite a shared directory on each
   cluster. Your synced code may be overwritten by another agent, and vice
   versa. This is expected — it means all agents benefit from each other's
   code improvements.

6. **No chaining read-only CLIs**: Don't chain multiple read-only tools in one
   command (e.g., `lq && llogs bp`). Run them separately.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Agent ID not provided" | Add `--agent-id <id>` flag, or run `qf-init` to create a session |
| "Token not provided" | Add `--token <token>` flag |
| "Invalid token for agent" | Your token doesn't match. Run `qf-init` for a new session |
| "No quota on cluster (quota=0)" | Ask the user to set a quota in `config/quotas.json` |
| "Quota exceeded" | Wait for a job to finish or cancel one with `qf-cancel` |
| "Could not find project root" | Run `qf-submit` from the project directory (must contain `hpc_utils/copy`) |
| "SSH timed out" | Job may have been submitted. Check with `lq <cluster>` |

## Summary of all tools

| Tool | Purpose | Auth required |
|------|---------|---------------|
| `lq` | Check job queues | No |
| `ljob` | Detailed job info | No |
| `lnodes` | Node status | No |
| `llogs` | Fetch job logs | No |
| `lhist` | Job history (sacct) | No |
| `lcat` | Display remote file | No |
| `lls` | List remote files | No |
| `lcp` | Copy remote file locally | No |
| `lcplog` | Copy log file locally | No |
| `ltransfers` | Check active transfers | No |
| `lqa` | DPO pair analysis (remote) | No |
| `qfdata` | DPO pair analysis (local) | No |
| `qf-help` | Show this guide | No |
| `qf-init` | Initialize agent session | No |
| `qf-submit` | Submit a job | Yes |
| `qf-cancel` | Cancel your job | Yes |
| `qf-sync` | Sync code to cluster | No |
