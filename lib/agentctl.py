"""Shared utilities for agentctl CLI tools."""

import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime

AGENT_ID_PATTERN = re.compile(r'^[0-9a-f]{6}$')

AGENTCTL_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
CONFIG_DIR = os.path.join(AGENTCTL_ROOT, 'config')
SESSIONS_DIR = os.path.join(AGENTCTL_ROOT, 'sessions')

CLUSTERS = {'bp', 'is', 'mn'}
COPY_FLAGS = {'bp': '-by', 'is': '-iy', 'mn': '-my'}

RESERVATION_TIMEOUT_SECONDS = 600  # 10-minute hard ceiling for stale reservations


def _process_alive(pid):
    """Check if a process is still running (via os.kill signal 0)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _reap_stale_reservations(jobs, cluster=None):
    """Remove stale pending reservations from jobs list (mutates in place).

    A reservation is stale if:
    - Its owning process (pid) is confirmed dead, OR
    - It is older than RESERVATION_TIMEOUT_SECONDS (regardless of pid)

    Called inside locked_jobs() so mutations are saved automatically.
    """
    now = datetime.now()
    to_keep = []
    for j in jobs:
        if j.get('status') != 'pending':
            to_keep.append(j)
            continue
        if cluster is not None and j.get('cluster') != cluster:
            to_keep.append(j)
            continue
        pid = j.get('pid')
        try:
            age = (now - datetime.fromisoformat(j['submitted_at'])).total_seconds()
        except (ValueError, KeyError):
            continue  # malformed → reap
        # Hard ceiling: always reap after timeout, even if PID is alive
        if age > RESERVATION_TIMEOUT_SECONDS:
            continue
        # Within timeout: reap only if owning process is confirmed dead
        if pid and not _process_alive(pid):
            continue
        to_keep.append(j)
    jobs.clear()
    jobs.extend(to_keep)


def generate_token():
    """Generate a cryptographically random token."""
    return secrets.token_hex(32)


def hash_token(token):
    """Hash a token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_agent_token(agent_id, token):
    """Verify that the provided token matches the stored hash for this agent."""
    identity_file = os.path.join(get_session_dir(agent_id), 'identity.json')
    if not os.path.exists(identity_file):
        return False
    with open(identity_file) as f:
        identity = json.load(f)
    stored_hash = identity.get('token_hash')
    if not stored_hash:
        return False
    return secrets.compare_digest(
        hashlib.sha256(token.encode()).hexdigest(), stored_hash
    )


def parse_agent_flags(argv):
    """Extract --agent-id and --token from argv, returning (agent_id, token, remaining_argv).

    Flags can appear anywhere in argv. They are consumed and not passed through.
    Falls back to environment variables if flags are not provided.
    """
    agent_id = None
    token = None
    remaining = []
    i = 0
    while i < len(argv):
        if argv[i] == '--agent-id' and i + 1 < len(argv):
            agent_id = argv[i + 1]
            i += 2
        elif argv[i] == '--token' and i + 1 < len(argv):
            token = argv[i + 1]
            i += 2
        else:
            remaining.append(argv[i])
            i += 1
    if not agent_id:
        agent_id = os.environ.get('AGENTCTL_AGENT_ID')
    if not token:
        token = os.environ.get('AGENTCTL_TOKEN')
    return agent_id, token, remaining


def get_agent_id(agent_id=None):
    """Validate an agent ID. Exits if invalid or session doesn't exist.

    If agent_id is None, reads from AGENTCTL_AGENT_ID env var.
    """
    if not agent_id:
        agent_id = os.environ.get('AGENTCTL_AGENT_ID')
    if not agent_id:
        print("Error: Agent ID not provided.", file=sys.stderr)
        print("Use --agent-id <id> or set AGENTCTL_AGENT_ID.", file=sys.stderr)
        sys.exit(1)
    if not AGENT_ID_PATTERN.match(agent_id):
        print(f"Error: Invalid agent ID format '{agent_id}'. Must be 6 hex chars.", file=sys.stderr)
        sys.exit(1)
    session_dir = os.path.join(SESSIONS_DIR, agent_id)
    # Verify resolved path stays within sessions dir (defense-in-depth)
    real_session = os.path.realpath(session_dir)
    real_sessions_root = os.path.realpath(SESSIONS_DIR)
    if not real_session.startswith(real_sessions_root + os.sep):
        print(f"Error: Invalid session path for agent '{agent_id}'.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(real_session):
        print(f"Error: No session found for agent '{agent_id}'.", file=sys.stderr)
        sys.exit(1)
    return agent_id


def get_verified_agent(agent_id=None, token=None):
    """Validate agent ID and verify token. Exits on auth failure.

    If agent_id/token are None, reads from env vars.
    """
    agent_id = get_agent_id(agent_id)
    if not token:
        token = os.environ.get('AGENTCTL_TOKEN')
    if not token:
        print("Error: Token not provided.", file=sys.stderr)
        print("Use --token <token> or set AGENTCTL_TOKEN.", file=sys.stderr)
        sys.exit(1)
    if not verify_agent_token(agent_id, token):
        print(f"Error: Invalid token for agent '{agent_id}'.", file=sys.stderr)
        sys.exit(1)
    return agent_id


def get_session_dir(agent_id):
    return os.path.join(SESSIONS_DIR, agent_id)


def load_jobs(agent_id):
    jobs_file = os.path.join(get_session_dir(agent_id), 'jobs.json')
    if not os.path.exists(jobs_file):
        return []
    with open(jobs_file) as f:
        return json.load(f)


def save_jobs(agent_id, jobs):
    jobs_file = os.path.join(get_session_dir(agent_id), 'jobs.json')
    with open(jobs_file, 'w') as f:
        json.dump(jobs, f, indent=2)


@contextmanager
def locked_jobs(agent_id):
    """Context manager that holds an exclusive lock while reading/writing jobs.json.

    Yields the current jobs list. Any mutations to the list will be saved back
    to jobs.json when the context exits (before the lock is released).

    Usage:
        with locked_jobs(agent_id) as jobs:
            jobs.append({...})
    """
    lock_path = os.path.join(get_session_dir(agent_id), 'jobs.lock')
    fd = open(lock_path, 'w')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        jobs = load_jobs(agent_id)
        yield jobs
        save_jobs(agent_id, jobs)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def get_active_job_ids_on_cluster(cluster):
    """Get set of active (PENDING/RUNNING) job IDs on a cluster."""
    try:
        result = subprocess.run(
            ['ssh', cluster, 'squeue -u $USER -h -o "%i"'],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return set()
        return {line.strip().strip('"') for line in result.stdout.strip().split('\n') if line.strip()}
    except (subprocess.TimeoutExpired, Exception):
        return set()


def count_active_agent_jobs(agent_id, cluster):
    """Count active jobs including non-stale pending reservations."""
    jobs = load_jobs(agent_id)
    now = datetime.now()

    pending_count = 0
    real_job_ids = set()
    for j in jobs:
        if j.get('cluster') != cluster:
            continue
        if j.get('status') == 'pending':
            try:
                age = (now - datetime.fromisoformat(j['submitted_at'])).total_seconds()
            except (ValueError, KeyError):
                continue  # malformed, don't count
            if age > RESERVATION_TIMEOUT_SECONDS:
                continue  # past hard ceiling, don't count
            pid = j.get('pid')
            if pid and not _process_alive(pid):
                continue  # dead process, don't count
            pending_count += 1
        else:
            real_job_ids.add(j['job_id'])

    if not real_job_ids and pending_count == 0:
        return 0
    active_ids = get_active_job_ids_on_cluster(cluster) if real_job_ids else set()
    return len(real_job_ids & active_ids) + pending_count


def get_quota(agent_id, cluster):
    """Get the quota for this agent on this cluster. Returns 0 if not configured."""
    quotas_file = os.path.join(CONFIG_DIR, 'quotas.json')
    if not os.path.exists(quotas_file):
        return 0
    with open(quotas_file) as f:
        quotas = json.load(f)
    # Agent-specific override takes priority
    agent_quotas = quotas.get('agents', {}).get(agent_id, {})
    if cluster in agent_quotas:
        return agent_quotas[cluster]
    return quotas.get('default', {}).get(cluster, 0)


def reserve_quota_slot(agent_id, cluster, args_str):
    """Phase 1: Atomically check quota and create a pending reservation.

    Returns (ok, reservation_id, active_count, quota).
    """
    quota = get_quota(agent_id, cluster)
    if quota == 0:
        return (False, None, 0, 0)

    # squeue call outside lock (slow network I/O)
    active_ids = get_active_job_ids_on_cluster(cluster)

    reservation_id = secrets.token_hex(6)  # 12 hex chars

    with locked_jobs(agent_id) as jobs:
        _reap_stale_reservations(jobs, cluster)

        # Count real active jobs (intersection with squeue)
        real_ids = {j['job_id'] for j in jobs
                    if j['cluster'] == cluster and j.get('status') != 'pending'}
        real_active = len(real_ids & active_ids)

        # Count live pending reservations
        pending_count = sum(1 for j in jobs
                          if j['cluster'] == cluster and j.get('status') == 'pending')

        active_count = real_active + pending_count
        if active_count >= quota:
            return (False, None, active_count, quota)

        jobs.append({
            'job_id': f'pending:{reservation_id}',
            'cluster': cluster,
            'submitted_at': datetime.now().isoformat(),
            'args': args_str,
            'status': 'pending',
            'reservation_id': reservation_id,
            'pid': os.getpid(),
        })

    return (True, reservation_id, active_count, quota)


def finalize_reservation(agent_id, reservation_id, real_job_id, cluster, args_str):
    """Phase 3: Replace a pending reservation with the real job ID.

    If the reservation was reaped (e.g., by another agent's stale-reaper),
    falls back to creating a fresh entry so the job is always tracked.
    """
    with locked_jobs(agent_id) as jobs:
        for j in jobs:
            if j.get('reservation_id') == reservation_id and j.get('status') == 'pending':
                j['job_id'] = str(real_job_id)
                j['submitted_at'] = datetime.now().isoformat()
                j.pop('status', None)
                j.pop('reservation_id', None)
                j.pop('pid', None)
                return True
        # Reservation was reaped — create entry directly
        jobs.append({
            'job_id': str(real_job_id),
            'cluster': cluster,
            'submitted_at': datetime.now().isoformat(),
            'args': args_str,
        })
        return True


def cancel_reservation(agent_id, reservation_id):
    """Remove a pending reservation (cleanup on failure)."""
    try:
        with locked_jobs(agent_id) as jobs:
            jobs[:] = [j for j in jobs if not
                       (j.get('reservation_id') == reservation_id and j.get('status') == 'pending')]
    except Exception:
        pass  # best effort; stale reaper is the backup


def is_agent_job(agent_id, job_id, cluster):
    """Check if a job belongs to this agent."""
    jobs = load_jobs(agent_id)
    return any(j['job_id'] == str(job_id) and j['cluster'] == cluster for j in jobs)


def find_project_root():
    """Walk up from cwd to find directory containing hpc_utils/copy."""
    path = os.getcwd()
    for _ in range(10):
        if os.path.exists(os.path.join(path, 'hpc_utils', 'copy')):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None
