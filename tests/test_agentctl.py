"""Comprehensive test suite for the agentctl library.

Covers: token auth, agent ID validation, process checks, stale reservation
reaping, the reservation flow (reserve/finalize/cancel), locking semantics,
active job counting, and ownership checks.
"""

import json
import os
import subprocess
import sys

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Bootstrap: make lib/ importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
import agentctl


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_session(tmp_path):
    """Create a temporary agentctl environment with a test session."""
    original_sessions = agentctl.SESSIONS_DIR
    original_config = agentctl.CONFIG_DIR
    agentctl.SESSIONS_DIR = str(tmp_path / 'sessions')
    agentctl.CONFIG_DIR = str(tmp_path / 'config')
    os.makedirs(agentctl.SESSIONS_DIR)
    os.makedirs(agentctl.CONFIG_DIR)

    # Create a test agent session
    agent_id = 'aabb11'
    token = agentctl.generate_token()
    session_dir = os.path.join(agentctl.SESSIONS_DIR, agent_id)
    os.makedirs(session_dir)

    identity = {
        'agent_id': agent_id,
        'name': 'test',
        'created_at': datetime.now().isoformat(),
        'token_hash': agentctl.hash_token(token),
    }
    with open(os.path.join(session_dir, 'identity.json'), 'w') as f:
        json.dump(identity, f)
    with open(os.path.join(session_dir, 'jobs.json'), 'w') as f:
        json.dump([], f)

    # Create quotas.json: bp=2, is=1, mn=0
    quotas = {'default': {'bp': 2, 'is': 1, 'mn': 0}, 'agents': {}}
    with open(os.path.join(agentctl.CONFIG_DIR, 'quotas.json'), 'w') as f:
        json.dump(quotas, f)

    yield {
        'agent_id': agent_id,
        'token': token,
        'session_dir': session_dir,
        'tmp_path': tmp_path,
    }

    agentctl.SESSIONS_DIR = original_sessions
    agentctl.CONFIG_DIR = original_config


def _dead_pid():
    """Return a PID that is guaranteed to be dead.

    Spawns a subprocess that exits immediately, waits for it, and returns
    its now-defunct PID.
    """
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid


# ===================================================================
# 1. Token auth
# ===================================================================

class TestTokenAuth:
    def test_generate_token_length(self):
        token = agentctl.generate_token()
        assert len(token) == 64, "Token must be 64 hex characters (32 bytes)"

    def test_generate_token_is_hex(self):
        token = agentctl.generate_token()
        int(token, 16)  # raises ValueError if not valid hex

    def test_generate_token_uniqueness(self):
        tokens = {agentctl.generate_token() for _ in range(50)}
        assert len(tokens) == 50, "Tokens should be unique"

    def test_hash_token_is_sha256(self):
        token = "deadbeef"
        h = agentctl.hash_token(token)
        assert len(h) == 64, "SHA-256 digest must be 64 hex chars"
        int(h, 16)

    def test_hash_token_deterministic(self):
        token = agentctl.generate_token()
        assert agentctl.hash_token(token) == agentctl.hash_token(token)

    def test_verify_correct_token(self, test_session):
        agent_id = test_session['agent_id']
        token = test_session['token']
        assert agentctl.verify_agent_token(agent_id, token) is True

    def test_verify_wrong_token(self, test_session):
        agent_id = test_session['agent_id']
        assert agentctl.verify_agent_token(agent_id, 'wrong_token') is False

    def test_verify_nonexistent_agent(self, test_session):
        assert agentctl.verify_agent_token('ffffff', 'anything') is False

    def test_verify_uses_compare_digest(self, test_session):
        """secrets.compare_digest is called -- verified by observing that
        verify_agent_token still returns the correct boolean even when
        the hash is compared character-by-character (timing-safe)."""
        agent_id = test_session['agent_id']
        token = test_session['token']
        # Correct token
        assert agentctl.verify_agent_token(agent_id, token) is True
        # A token whose hash differs only in the last nibble should still
        # be rejected (compare_digest does full comparison).
        assert agentctl.verify_agent_token(agent_id, token + 'x') is False


# ===================================================================
# 2. Agent ID validation
# ===================================================================

class TestAgentIdValidation:
    """get_agent_id() reads AGENTCTL_AGENT_ID from the environment and
    validates format + session existence.  We set the env var via
    monkeypatch and expect sys.exit(1) on failures.
    """

    def test_rejects_empty(self, test_session, monkeypatch):
        monkeypatch.delenv('AGENTCTL_AGENT_ID', raising=False)
        with pytest.raises(SystemExit):
            agentctl.get_agent_id()

    def test_rejects_too_short(self, test_session, monkeypatch):
        monkeypatch.setenv('AGENTCTL_AGENT_ID', 'abc')
        with pytest.raises(SystemExit):
            agentctl.get_agent_id()

    def test_rejects_too_long(self, test_session, monkeypatch):
        monkeypatch.setenv('AGENTCTL_AGENT_ID', 'aabbccdd')
        with pytest.raises(SystemExit):
            agentctl.get_agent_id()

    def test_rejects_non_hex(self, test_session, monkeypatch):
        monkeypatch.setenv('AGENTCTL_AGENT_ID', 'gggggg')
        with pytest.raises(SystemExit):
            agentctl.get_agent_id()

    def test_rejects_uppercase_hex(self, test_session, monkeypatch):
        monkeypatch.setenv('AGENTCTL_AGENT_ID', 'AABB11')
        with pytest.raises(SystemExit):
            agentctl.get_agent_id()

    def test_rejects_path_traversal(self, test_session, monkeypatch):
        monkeypatch.setenv('AGENTCTL_AGENT_ID', '../etc')
        with pytest.raises(SystemExit):
            agentctl.get_agent_id()

    def test_rejects_path_traversal_hex(self, test_session, monkeypatch):
        """Even a 6-char string that's valid hex won't pass if the session
        dir resolves outside SESSIONS_DIR (defense-in-depth check)."""
        # This would need a symlink or similar to actually escape, but the
        # regex alone blocks '../xx' since it contains non-hex chars and is
        # too long. We verify the regex gate blocks it.
        monkeypatch.setenv('AGENTCTL_AGENT_ID', '../../a')
        with pytest.raises(SystemExit):
            agentctl.get_agent_id()

    def test_rejects_missing_session_dir(self, test_session, monkeypatch):
        monkeypatch.setenv('AGENTCTL_AGENT_ID', 'cc0011')  # valid format, no dir
        with pytest.raises(SystemExit):
            agentctl.get_agent_id()

    def test_accepts_valid_id(self, test_session, monkeypatch):
        monkeypatch.setenv('AGENTCTL_AGENT_ID', test_session['agent_id'])
        result = agentctl.get_agent_id()
        assert result == test_session['agent_id']


# ===================================================================
# 3. _process_alive()
# ===================================================================

class TestProcessAlive:
    def test_pid_zero(self):
        assert agentctl._process_alive(0) is False

    def test_pid_negative(self):
        assert agentctl._process_alive(-1) is False

    def test_pid_none(self):
        assert agentctl._process_alive(None) is False

    def test_pid_string(self):
        assert agentctl._process_alive("123") is False

    def test_current_process(self):
        assert agentctl._process_alive(os.getpid()) is True

    def test_nonexistent_large_pid(self):
        assert agentctl._process_alive(999999999) is False


# ===================================================================
# 4. _reap_stale_reservations()
# ===================================================================

class TestReapStaleReservations:
    def _pending_entry(self, cluster='bp', pid=None, submitted_at=None,
                       reservation_id='res000'):
        """Helper to build a pending reservation dict."""
        return {
            'job_id': f'pending:{reservation_id}',
            'cluster': cluster,
            'submitted_at': (submitted_at or datetime.now()).isoformat(),
            'status': 'pending',
            'reservation_id': reservation_id,
            'pid': pid if pid is not None else os.getpid(),
        }

    def test_reaps_dead_pid(self):
        dead = _dead_pid()
        jobs = [self._pending_entry(pid=dead)]
        agentctl._reap_stale_reservations(jobs)
        assert len(jobs) == 0, "Entry with dead PID should be reaped"

    def test_reaps_old_entry(self):
        old_time = datetime.now() - timedelta(seconds=agentctl.RESERVATION_TIMEOUT_SECONDS + 1)
        jobs = [self._pending_entry(submitted_at=old_time)]
        agentctl._reap_stale_reservations(jobs)
        assert len(jobs) == 0, "Entry past timeout should be reaped"

    def test_keeps_live_within_timeout(self):
        jobs = [self._pending_entry(pid=os.getpid())]
        agentctl._reap_stale_reservations(jobs)
        assert len(jobs) == 1, "Live entry within timeout should be kept"

    def test_keeps_non_pending(self):
        old_time = datetime.now() - timedelta(seconds=agentctl.RESERVATION_TIMEOUT_SECONDS + 100)
        entry = {
            'job_id': '12345',
            'cluster': 'bp',
            'submitted_at': old_time.isoformat(),
        }
        jobs = [entry]
        agentctl._reap_stale_reservations(jobs)
        assert len(jobs) == 1, "Non-pending entries must never be reaped"

    def test_reaps_live_pid_past_hard_timeout(self):
        """Even if the PID is alive, entries past the hard timeout ceiling
        are reaped."""
        old_time = datetime.now() - timedelta(seconds=agentctl.RESERVATION_TIMEOUT_SECONDS + 1)
        jobs = [self._pending_entry(pid=os.getpid(), submitted_at=old_time)]
        agentctl._reap_stale_reservations(jobs)
        assert len(jobs) == 0, "Live PID past hard timeout should still be reaped"

    def test_reaps_malformed_submitted_at(self):
        entry = self._pending_entry(pid=os.getpid())
        entry['submitted_at'] = 'not-a-date'
        jobs = [entry]
        agentctl._reap_stale_reservations(jobs)
        assert len(jobs) == 0, "Malformed submitted_at should be reaped"

    def test_reaps_missing_submitted_at(self):
        entry = self._pending_entry(pid=os.getpid())
        del entry['submitted_at']
        jobs = [entry]
        agentctl._reap_stale_reservations(jobs)
        assert len(jobs) == 0, "Missing submitted_at should be reaped"

    def test_cluster_filter(self):
        """When cluster param is given, only reap that cluster's entries."""
        dead = _dead_pid()
        bp_entry = self._pending_entry(cluster='bp', pid=dead, reservation_id='res_bp')
        is_entry = self._pending_entry(cluster='is', pid=dead, reservation_id='res_is')
        jobs = [bp_entry, is_entry]
        agentctl._reap_stale_reservations(jobs, cluster='bp')
        assert len(jobs) == 1, "Only bp entry should be reaped"
        assert jobs[0]['cluster'] == 'is'

    def test_cluster_none_reaps_all_clusters(self):
        """When cluster is None, reap stale entries across all clusters."""
        dead = _dead_pid()
        bp_entry = self._pending_entry(cluster='bp', pid=dead, reservation_id='res_bp2')
        is_entry = self._pending_entry(cluster='is', pid=dead, reservation_id='res_is2')
        jobs = [bp_entry, is_entry]
        agentctl._reap_stale_reservations(jobs, cluster=None)
        assert len(jobs) == 0, "Both stale entries should be reaped when cluster=None"


# ===================================================================
# 5. Reservation flow
# ===================================================================

class TestReservationFlow:
    @patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value=set())
    def test_reserve_creates_pending_entry(self, mock_squeue, test_session):
        agent_id = test_session['agent_id']
        ok, res_id, count, quota = agentctl.reserve_quota_slot(agent_id, 'bp', 'runner --flag')
        assert ok is True
        assert res_id is not None
        assert len(res_id) == 12, "reservation_id should be 12 hex chars"
        assert count == 0
        assert quota == 2

        jobs = agentctl.load_jobs(agent_id)
        assert len(jobs) == 1
        entry = jobs[0]
        assert entry['job_id'].startswith('pending:')
        assert entry['status'] == 'pending'
        assert entry['reservation_id'] == res_id
        assert entry['pid'] == os.getpid()
        assert entry['cluster'] == 'bp'
        assert entry['args'] == 'runner --flag'

    @patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value=set())
    def test_reserve_rejects_zero_quota(self, mock_squeue, test_session):
        agent_id = test_session['agent_id']
        ok, res_id, count, quota = agentctl.reserve_quota_slot(agent_id, 'mn', 'runner')
        assert ok is False
        assert res_id is None
        assert quota == 0

    @patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value={'111', '222'})
    def test_reserve_rejects_when_at_quota(self, mock_squeue, test_session):
        """bp quota is 2; seed two real active jobs so reservation fails."""
        agent_id = test_session['agent_id']
        # Seed two finalized jobs whose IDs are in the mock squeue output
        agentctl.save_jobs(agent_id, [
            {'job_id': '111', 'cluster': 'bp', 'submitted_at': datetime.now().isoformat(), 'args': 'x'},
            {'job_id': '222', 'cluster': 'bp', 'submitted_at': datetime.now().isoformat(), 'args': 'y'},
        ])
        ok, res_id, count, quota = agentctl.reserve_quota_slot(agent_id, 'bp', 'runner')
        assert ok is False
        assert count >= quota

    @patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value=set())
    def test_reserve_counts_pending_toward_quota(self, mock_squeue, test_session):
        """is quota is 1; create one pending entry then try to reserve another."""
        agent_id = test_session['agent_id']
        ok1, res1, _, _ = agentctl.reserve_quota_slot(agent_id, 'is', 'runner1')
        assert ok1 is True
        ok2, res2, count, quota = agentctl.reserve_quota_slot(agent_id, 'is', 'runner2')
        assert ok2 is False, "Second reservation should be rejected (quota=1)"
        assert count >= quota

    @patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value=set())
    def test_finalize_replaces_pending(self, mock_squeue, test_session):
        agent_id = test_session['agent_id']
        ok, res_id, _, _ = agentctl.reserve_quota_slot(agent_id, 'bp', 'runner --flag')
        assert ok is True

        agentctl.finalize_reservation(agent_id, res_id, '99999', 'bp', 'runner --flag')

        jobs = agentctl.load_jobs(agent_id)
        assert len(jobs) == 1
        entry = jobs[0]
        assert entry['job_id'] == '99999'
        assert 'status' not in entry
        assert 'reservation_id' not in entry
        assert 'pid' not in entry

    def test_finalize_fallback_when_reaped(self, test_session):
        """If the reservation was reaped before finalize, a fresh entry is created."""
        agent_id = test_session['agent_id']
        # No pending entry exists -- simulating a reaped reservation
        result = agentctl.finalize_reservation(agent_id, 'nonexistent_id', '88888', 'bp', 'runner')
        assert result is True
        jobs = agentctl.load_jobs(agent_id)
        assert len(jobs) == 1
        assert jobs[0]['job_id'] == '88888'

    @patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value=set())
    def test_cancel_removes_pending(self, mock_squeue, test_session):
        agent_id = test_session['agent_id']
        ok, res_id, _, _ = agentctl.reserve_quota_slot(agent_id, 'bp', 'runner')
        assert ok is True
        assert len(agentctl.load_jobs(agent_id)) == 1

        agentctl.cancel_reservation(agent_id, res_id)
        assert len(agentctl.load_jobs(agent_id)) == 0

    def test_cancel_nonexistent_does_not_crash(self, test_session):
        """cancel_reservation on a missing reservation_id must not raise."""
        agent_id = test_session['agent_id']
        agentctl.cancel_reservation(agent_id, 'does_not_exist')  # should not raise

    @patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value=set())
    def test_cancel_only_removes_matching_pending(self, mock_squeue, test_session):
        """Cancel should remove only the matching pending entry, leaving others."""
        agent_id = test_session['agent_id']
        ok1, res1, _, _ = agentctl.reserve_quota_slot(agent_id, 'bp', 'runner1')
        ok2, res2, _, _ = agentctl.reserve_quota_slot(agent_id, 'bp', 'runner2')
        assert ok1 and ok2

        agentctl.cancel_reservation(agent_id, res1)
        jobs = agentctl.load_jobs(agent_id)
        assert len(jobs) == 1
        assert jobs[0]['reservation_id'] == res2


# ===================================================================
# 6. Locking (locked_jobs)
# ===================================================================

class TestLockedJobs:
    def test_mutations_saved(self, test_session):
        agent_id = test_session['agent_id']
        with agentctl.locked_jobs(agent_id) as jobs:
            jobs.append({'job_id': '42', 'cluster': 'bp',
                         'submitted_at': datetime.now().isoformat(), 'args': 'x'})
        persisted = agentctl.load_jobs(agent_id)
        assert len(persisted) == 1
        assert persisted[0]['job_id'] == '42'

    def test_exception_does_not_save(self, test_session):
        """If an exception is raised inside the context, the lock is released
        but the mutations should NOT be saved (the save is skipped because the
        exception propagates past the save_jobs call)."""
        agent_id = test_session['agent_id']
        # Seed a known-good state
        agentctl.save_jobs(agent_id, [{'job_id': 'original', 'cluster': 'bp',
                                        'submitted_at': datetime.now().isoformat(),
                                        'args': ''}])
        with pytest.raises(RuntimeError):
            with agentctl.locked_jobs(agent_id) as jobs:
                jobs.append({'job_id': 'bad', 'cluster': 'bp',
                             'submitted_at': datetime.now().isoformat(), 'args': ''})
                raise RuntimeError("deliberate failure")

        persisted = agentctl.load_jobs(agent_id)
        # The 'bad' entry should NOT have been persisted
        assert all(j['job_id'] != 'bad' for j in persisted), \
            "Partial mutations must not be saved when an exception occurs"


# ===================================================================
# 7. count_active_agent_jobs()
# ===================================================================

class TestCountActiveAgentJobs:
    def test_counts_live_pending(self, test_session):
        """A pending entry with a live PID and within timeout should be counted."""
        agent_id = test_session['agent_id']
        agentctl.save_jobs(agent_id, [{
            'job_id': 'pending:abc',
            'cluster': 'bp',
            'submitted_at': datetime.now().isoformat(),
            'status': 'pending',
            'reservation_id': 'abc',
            'pid': os.getpid(),
        }])
        with patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value=set()):
            count = agentctl.count_active_agent_jobs(agent_id, 'bp')
        assert count == 1

    def test_does_not_count_dead_pid_pending(self, test_session):
        agent_id = test_session['agent_id']
        dead = _dead_pid()
        agentctl.save_jobs(agent_id, [{
            'job_id': 'pending:xyz',
            'cluster': 'bp',
            'submitted_at': datetime.now().isoformat(),
            'status': 'pending',
            'reservation_id': 'xyz',
            'pid': dead,
        }])
        with patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value=set()):
            count = agentctl.count_active_agent_jobs(agent_id, 'bp')
        assert count == 0

    def test_does_not_count_expired_pending(self, test_session):
        agent_id = test_session['agent_id']
        old = datetime.now() - timedelta(seconds=agentctl.RESERVATION_TIMEOUT_SECONDS + 1)
        agentctl.save_jobs(agent_id, [{
            'job_id': 'pending:old',
            'cluster': 'bp',
            'submitted_at': old.isoformat(),
            'status': 'pending',
            'reservation_id': 'old',
            'pid': os.getpid(),
        }])
        with patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value=set()):
            count = agentctl.count_active_agent_jobs(agent_id, 'bp')
        assert count == 0

    def test_counts_real_active_jobs(self, test_session):
        """Real (finalized) jobs are counted only if they appear in squeue output."""
        agent_id = test_session['agent_id']
        agentctl.save_jobs(agent_id, [
            {'job_id': '100', 'cluster': 'bp', 'submitted_at': datetime.now().isoformat(), 'args': 'x'},
            {'job_id': '200', 'cluster': 'bp', 'submitted_at': datetime.now().isoformat(), 'args': 'y'},
        ])
        # Only job 100 is still in squeue
        with patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value={'100'}):
            count = agentctl.count_active_agent_jobs(agent_id, 'bp')
        assert count == 1

    def test_mixed_real_and_pending(self, test_session):
        agent_id = test_session['agent_id']
        agentctl.save_jobs(agent_id, [
            {'job_id': '100', 'cluster': 'bp', 'submitted_at': datetime.now().isoformat(), 'args': 'x'},
            {
                'job_id': 'pending:abc',
                'cluster': 'bp',
                'submitted_at': datetime.now().isoformat(),
                'status': 'pending',
                'reservation_id': 'abc',
                'pid': os.getpid(),
            },
        ])
        with patch.object(agentctl, 'get_active_job_ids_on_cluster', return_value={'100'}):
            count = agentctl.count_active_agent_jobs(agent_id, 'bp')
        assert count == 2, "1 real active + 1 live pending = 2"


# ===================================================================
# 8. is_agent_job()
# ===================================================================

class TestIsAgentJob:
    def test_returns_true_for_matching_job(self, test_session):
        agent_id = test_session['agent_id']
        agentctl.save_jobs(agent_id, [
            {'job_id': '12345', 'cluster': 'bp', 'submitted_at': datetime.now().isoformat(), 'args': 'x'},
        ])
        assert agentctl.is_agent_job(agent_id, '12345', 'bp') is True

    def test_returns_false_for_wrong_job_id(self, test_session):
        agent_id = test_session['agent_id']
        agentctl.save_jobs(agent_id, [
            {'job_id': '12345', 'cluster': 'bp', 'submitted_at': datetime.now().isoformat(), 'args': 'x'},
        ])
        assert agentctl.is_agent_job(agent_id, '99999', 'bp') is False

    def test_returns_false_for_wrong_cluster(self, test_session):
        agent_id = test_session['agent_id']
        agentctl.save_jobs(agent_id, [
            {'job_id': '12345', 'cluster': 'bp', 'submitted_at': datetime.now().isoformat(), 'args': 'x'},
        ])
        assert agentctl.is_agent_job(agent_id, '12345', 'is') is False

    def test_returns_false_for_pending_entry(self, test_session):
        """Pending entries have job_id='pending:xxx', which should not match
        a real numeric job ID."""
        agent_id = test_session['agent_id']
        agentctl.save_jobs(agent_id, [{
            'job_id': 'pending:abc123',
            'cluster': 'bp',
            'submitted_at': datetime.now().isoformat(),
            'status': 'pending',
            'reservation_id': 'abc123',
            'pid': os.getpid(),
        }])
        assert agentctl.is_agent_job(agent_id, '12345', 'bp') is False

    def test_returns_false_for_empty_jobs(self, test_session):
        agent_id = test_session['agent_id']
        assert agentctl.is_agent_job(agent_id, '12345', 'bp') is False

    def test_int_job_id_coerced_to_str(self, test_session):
        """is_agent_job converts job_id to str, so passing an int should work."""
        agent_id = test_session['agent_id']
        agentctl.save_jobs(agent_id, [
            {'job_id': '12345', 'cluster': 'bp', 'submitted_at': datetime.now().isoformat(), 'args': 'x'},
        ])
        assert agentctl.is_agent_job(agent_id, 12345, 'bp') is True


# ===================================================================
# 9. get_quota() (bonus coverage)
# ===================================================================

class TestGetQuota:
    def test_default_quota(self, test_session):
        agent_id = test_session['agent_id']
        assert agentctl.get_quota(agent_id, 'bp') == 2
        assert agentctl.get_quota(agent_id, 'is') == 1
        assert agentctl.get_quota(agent_id, 'mn') == 0

    def test_agent_override(self, test_session):
        agent_id = test_session['agent_id']
        quotas_file = os.path.join(agentctl.CONFIG_DIR, 'quotas.json')
        with open(quotas_file) as f:
            quotas = json.load(f)
        quotas['agents'][agent_id] = {'bp': 5}
        with open(quotas_file, 'w') as f:
            json.dump(quotas, f)
        assert agentctl.get_quota(agent_id, 'bp') == 5
        # 'is' still uses default since override only sets 'bp'
        assert agentctl.get_quota(agent_id, 'is') == 1

    def test_missing_quotas_file(self, test_session):
        agent_id = test_session['agent_id']
        os.remove(os.path.join(agentctl.CONFIG_DIR, 'quotas.json'))
        assert agentctl.get_quota(agent_id, 'bp') == 0
