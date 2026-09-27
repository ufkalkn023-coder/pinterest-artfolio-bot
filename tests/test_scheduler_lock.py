"""Tests for the single-instance scheduler lock."""

import os

import pytest

from pinterest_art_bot.pipeline.lock import LockUnavailableError, ProcessLock


class TestAcquisition:
    def test_first_acquisition_succeeds(self, tmp_path):
        lock = ProcessLock(tmp_path / "scheduler.lock")
        with lock:
            assert lock.held is True
            assert lock.path.exists()
        assert lock.path.exists() is False

    def test_second_lock_is_refused(self, tmp_path):
        path = tmp_path / "scheduler.lock"
        first = ProcessLock(path)
        first.acquire()

        second = ProcessLock(path)
        with pytest.raises(LockUnavailableError, match="already running"):
            second.acquire()

        first.release()
        # Once released, the next process may take it.
        second.acquire()
        second.release()

    def test_lock_records_the_holder_pid(self, tmp_path):
        lock = ProcessLock(tmp_path / "scheduler.lock")
        with lock:
            assert lock.path.read_text().strip() == str(os.getpid())

    def test_parent_directory_is_created(self, tmp_path):
        lock = ProcessLock(tmp_path / "nested" / "dir" / "scheduler.lock")
        lock.acquire()
        assert lock.path.exists()
        lock.release()

    def test_release_is_idempotent(self, tmp_path):
        lock = ProcessLock(tmp_path / "scheduler.lock")
        lock.acquire()
        lock.release()
        lock.release()
        assert lock.path.exists() is False

    def test_release_without_acquire_is_safe(self, tmp_path):
        lock = ProcessLock(tmp_path / "scheduler.lock")
        lock.release()
        assert lock.held is False


class TestStaleLocks:
    def test_stale_lock_is_reclaimed(self, tmp_path):
        path = tmp_path / "scheduler.lock"
        # A PID that cannot be running.
        path.write_text("999999999")

        lock = ProcessLock(path)
        lock.acquire()
        assert lock.path.read_text().strip() == str(os.getpid())
        lock.release()

    def test_unreadable_lock_is_reclaimed(self, tmp_path):
        path = tmp_path / "scheduler.lock"
        path.write_text("not a pid")

        lock = ProcessLock(path)
        lock.acquire()
        assert lock.held is True
        lock.release()

    def test_empty_lock_is_reclaimed(self, tmp_path):
        path = tmp_path / "scheduler.lock"
        path.write_text("   ")

        lock = ProcessLock(path)
        lock.acquire()
        lock.release()

    def test_invalid_pid_is_treated_as_dead(self, tmp_path):
        path = tmp_path / "scheduler.lock"
        path.write_text("0")

        lock = ProcessLock(path)
        lock.acquire()
        lock.release()

    def test_lock_is_released_even_when_the_body_raises(self, tmp_path):
        lock = ProcessLock(tmp_path / "scheduler.lock")
        with pytest.raises(RuntimeError), lock:
            raise RuntimeError("run failed")
        assert lock.path.exists() is False
