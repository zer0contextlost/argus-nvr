"""
sentinel/services/supervisor.py — Generic child-process manager.

Wraps subprocess.Popen with:
  - start / stop / restart / status
  - configurable restart policy (always | on-failure | never)
  - backoff between restart attempts
  - per-process background watchdog thread that restarts crashed children

Global registry + health watchdog:
  - All ProcessSupervisor instances self-register in _ALL_SUPERVISORS.
  - A top-level periodic health check (start_global_watchdog) re-launches
    anything found dead every GLOBAL_WATCHDOG_INTERVAL seconds.
  - Restart counts are surfaced through the status() dict.

Usage:
    sup = ProcessSupervisor(
        name="go2rtc",
        cmd=["go2rtc.exe", "-config", "go2rtc.yaml"],
        cwd="F:/sentinel/app/config",
        restart_policy="always",
        max_restarts=10,
    )
    sup.start()
    ...
    sup.stop()
"""

from __future__ import annotations

import subprocess
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional

from sentinel.util.logging import get_logger

log = get_logger("sentinel.supervisor")

# Supervisor log-file rotation thresholds.
# Logs are rotated in _launch() — safe to do because the child process is
# always dead by the time _launch() is called (either first start, or after
# the watchdog has confirmed exit).
_LOG_MAX_BYTES: int = 10 * 1024 * 1024   # rotate when file exceeds 10 MB
_LOG_BACKUP_COUNT: int = 3               # keep at most 3 backup files
_LOG_DELETE_THRESHOLD: int = _LOG_MAX_BYTES * 10  # > 100 MB: delete instead of keep

# ---------------------------------------------------------------------------
# Global registry — all supervisors register themselves here.
# ---------------------------------------------------------------------------

_ALL_SUPERVISORS: dict[str, "ProcessSupervisor"] = {}
_registry_lock = threading.Lock()

GLOBAL_WATCHDOG_INTERVAL = 30  # seconds between health-check sweeps

_global_watchdog_thread: Optional[threading.Thread] = None
_global_stop_event = threading.Event()


def start_global_watchdog() -> None:
    """
    Start a daemon thread that periodically checks all registered supervisors
    and re-launches any that are in ERROR or unexpectedly STOPPED state.
    Idempotent — safe to call multiple times.
    """
    global _global_watchdog_thread
    if _global_watchdog_thread is not None and _global_watchdog_thread.is_alive():
        return
    _global_stop_event.clear()
    _global_watchdog_thread = threading.Thread(
        target=_global_health_loop,
        name="global-watchdog",
        daemon=True,
    )
    _global_watchdog_thread.start()
    log.info("global watchdog started (interval=%ss)", GLOBAL_WATCHDOG_INTERVAL)


def stop_global_watchdog() -> None:
    """Signal the global watchdog to stop."""
    _global_stop_event.set()
    if _global_watchdog_thread and _global_watchdog_thread.is_alive():
        _global_watchdog_thread.join(timeout=5.0)


def _global_health_loop() -> None:
    """Periodic sweep: find dead supervisors and attempt to re-launch them."""
    while not _global_stop_event.wait(GLOBAL_WATCHDOG_INTERVAL):
        with _registry_lock:
            supervisors = list(_ALL_SUPERVISORS.values())
        for sup in supervisors:
            try:
                st = sup.status()
                if st["status"] in (ProcessStatus.ERROR.value, ProcessStatus.STOPPED.value):
                    # Only auto-revive if the policy calls for it.
                    if sup.restart_policy != RestartPolicy.NEVER:
                        log.warning(
                            "[global-watchdog] %s is %s — attempting revival",
                            sup.name, st["status"],
                        )
                        # Hold the supervisor's lock for all mutations so we
                        # don't race with the per-process watchdog thread.
                        with sup._lock:
                            sup._restarts = 0
                            sup._stop_event.clear()
                            sup._launch()
                        # Restart watchdog thread if it exited.
                        if sup._watchdog is None or not sup._watchdog.is_alive():
                            sup._watchdog = threading.Thread(
                                target=sup._watch,
                                name=f"watchdog-{sup.name}",
                                daemon=True,
                            )
                            sup._watchdog.start()
            except Exception as exc:
                log.warning("[global-watchdog] error checking %s: %s", sup.name, exc)


def all_supervisor_statuses() -> list[dict]:
    """Return status dicts for every registered supervisor."""
    with _registry_lock:
        return [s.status() for s in _ALL_SUPERVISORS.values()]


class RestartPolicy(str, Enum):
    ALWAYS = "always"       # Restart unconditionally (e.g. go2rtc)
    ON_FAILURE = "on-failure"  # Restart only on non-zero exit code
    NEVER = "never"         # Do not restart


class ProcessStatus(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    RESTARTING = "restarting"
    ERROR = "error"


class ProcessSupervisor:
    """Supervise a single child process with automatic restart."""

    def __init__(
        self,
        name: str,
        cmd: list[str],
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        restart_policy: RestartPolicy | str = RestartPolicy.ON_FAILURE,
        max_restarts: int = 20,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
        log_file: Optional[Path] = None,
        register_global: bool = True,
    ):
        self.name = name
        self.cmd = cmd
        self.cwd = str(cwd) if cwd else None
        self.env = env
        self.restart_policy = RestartPolicy(restart_policy)
        self.max_restarts = max_restarts
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.log_file = log_file

        self._proc: Optional[subprocess.Popen] = None
        self._status = ProcessStatus.STOPPED
        self._restarts = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._watchdog: Optional[threading.Thread] = None

        # Register in global registry for the top-level health watchdog.
        # Opt-out (register_global=False) for processes that are gate-controlled
        # (e.g. motion-triggered recorders) — the watchdog must not revive them
        # unconditionally or it defeats the gate logic and floods go2rtc.
        if register_global:
            with _registry_lock:
                _ALL_SUPERVISORS[name] = self

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the child process and its watchdog thread."""
        with self._lock:
            if self._status == ProcessStatus.RUNNING:
                log.warning("[%s] already running (pid %s)", self.name, self._pid())
                return
            self._stop_event.clear()
            self._restarts = 0
            self._launch()

        self._watchdog = threading.Thread(
            target=self._watch, name=f"watchdog-{self.name}", daemon=True
        )
        self._watchdog.start()
        log.info("[%s] supervisor started", self.name)

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the watchdog to stop and terminate the child."""
        self._stop_event.set()
        # Grab and null out _proc under the lock, then do the blocking wait
        # outside so status() callers are not blocked for up to 10 seconds.
        proc_to_kill = None
        with self._lock:
            proc_to_kill = self._proc
            self._proc = None
            self._status = ProcessStatus.STOPPED
        if proc_to_kill is not None:
            try:
                proc_to_kill.terminate()
                proc_to_kill.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("[%s] terminate timed out — killing", self.name)
                proc_to_kill.kill()
                proc_to_kill.wait()
            except OSError:
                pass
        if self._watchdog and self._watchdog.is_alive():
            self._watchdog.join(timeout=5.0)
        log.info("[%s] supervisor stopped", self.name)

    def restart(self) -> None:
        """Stop and immediately restart the child."""
        log.info("[%s] manual restart requested", self.name)
        # Stop the watchdog first so it can't race with our own _launch() call.
        self._stop_event.set()
        if self._watchdog and self._watchdog.is_alive():
            self._watchdog.join(timeout=5.0)
        with self._lock:
            self._terminate(timeout=5.0)
        # Brief pause outside the lock so sockets/pipes drain.
        time.sleep(0.1)
        self._stop_event.clear()
        with self._lock:
            self._launch()
        # Restart the watchdog thread.
        self._watchdog = threading.Thread(
            target=self._watch,
            name=f"watchdog-{self.name}",
            daemon=True,
        )
        self._watchdog.start()

    def status(self) -> dict:
        """Return a status dict suitable for the health endpoint."""
        with self._lock:
            pid = self._pid()
            return {
                "name": self.name,
                "status": self._status.value,
                "pid": pid,
                "restarts": self._restarts,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def _rotate_log(self) -> None:
        """
        Rotate self.log_file if it exceeds _LOG_MAX_BYTES.
        Called from _launch() while the child is NOT running (safe to rename).

        Very large files (> _LOG_DELETE_THRESHOLD) are deleted outright rather
        than kept as a backup — avoids preserving a 1+ GB file as a backup.
        Normal-sized old files are shifted: .3 dropped, .2→.3, .1→.2, current→.1.
        """
        if not self.log_file or not self.log_file.exists():
            return
        try:
            size = self.log_file.stat().st_size
        except OSError:
            return
        if size < _LOG_MAX_BYTES:
            return

        if size >= _LOG_DELETE_THRESHOLD:
            try:
                self.log_file.unlink()
                log.info(
                    "[%s] deleted oversized log %s (%.0f MB)",
                    self.name, self.log_file.name, size / 1e6,
                )
            except OSError as exc:
                log.warning("[%s] could not delete oversized log: %s", self.name, exc)
            return

        # Shift existing backups down, dropping the oldest.
        for i in range(_LOG_BACKUP_COUNT - 1, 0, -1):
            src = Path(str(self.log_file) + f".{i}")
            dst = Path(str(self.log_file) + f".{i + 1}")
            if src.exists():
                try:
                    dst.unlink(missing_ok=True)
                    src.replace(dst)
                except OSError:
                    pass

        rotated = Path(str(self.log_file) + ".1")
        try:
            rotated.unlink(missing_ok=True)
            self.log_file.replace(rotated)
            log.info(
                "[%s] rotated log %s → %s (was %.1f MB)",
                self.name, self.log_file.name, rotated.name, size / 1e6,
            )
        except OSError as exc:
            log.warning("[%s] log rotation failed: %s", self.name, exc)

    def _launch(self) -> None:
        """Start the subprocess (must be called under self._lock)."""
        self._rotate_log()

        log_fh = None
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(self.log_file, "ab")

        try:
            self._proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                env=self.env,
                stdout=log_fh or subprocess.DEVNULL,
                stderr=log_fh or subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        finally:
            # Close our copy of the file handle — the child has its own copy.
            if log_fh is not None:
                log_fh.close()
        self._status = ProcessStatus.RUNNING
        log.info("[%s] launched pid=%s", self.name, self._proc.pid)

    def _terminate(self, timeout: float) -> None:
        """Terminate the running process gracefully, then forcibly."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("[%s] terminate timed out — killing", self.name)
            self._proc.kill()
            self._proc.wait()
        except OSError:
            pass
        finally:
            self._proc = None

    def _watch(self) -> None:
        """Watchdog loop — runs in a daemon thread."""
        backoff = self.initial_backoff

        while not self._stop_event.is_set():
            # Wait for the process to exit.
            proc = None
            with self._lock:
                proc = self._proc
            if proc is None:
                time.sleep(0.5)
                continue

            launch_time = time.monotonic()
            rc = proc.wait()  # blocks until exit
            uptime = time.monotonic() - launch_time

            if self._stop_event.is_set():
                break

            log.warning("[%s] exited with code %s (uptime=%.1fs)", self.name, rc, uptime)

            # If the process ran long enough to be considered stable, reset the
            # backoff so the next crash doesn't inherit a maxed-out delay.
            if uptime >= self.max_backoff:
                backoff = self.initial_backoff

            should_restart = (
                self.restart_policy == RestartPolicy.ALWAYS
                or (self.restart_policy == RestartPolicy.ON_FAILURE and rc != 0)
            )

            if not should_restart:
                with self._lock:
                    self._status = ProcessStatus.STOPPED
                log.info("[%s] not restarting (policy=%s)", self.name, self.restart_policy)
                break

            if self._restarts >= self.max_restarts:
                with self._lock:
                    self._status = ProcessStatus.ERROR
                log.error("[%s] max restarts (%s) reached", self.name, self.max_restarts)
                break

            with self._lock:
                self._status = ProcessStatus.RESTARTING
                self._restarts += 1

            log.info(
                "[%s] restarting in %.1fs (attempt %s/%s)",
                self.name, backoff, self._restarts, self.max_restarts,
            )
            self._stop_event.wait(backoff)
            if self._stop_event.is_set():
                break

            backoff = min(backoff * 2, self.max_backoff)

            with self._lock:
                self._launch()
