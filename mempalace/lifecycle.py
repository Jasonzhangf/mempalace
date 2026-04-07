"""
MemPalace Server Lifecycle Management

Handles:
  - PID file management (write / read / stale detection / cleanup)
  - Idle timer (last-activity tracking + auto-shutdown)
  - Process liveness checks
  - Graceful shutdown with file cleanup
"""

import json
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DIR = Path.home() / ".mempalace"
PID_FILE = DEFAULT_DIR / "server.pid"
SOCKET_FILE = DEFAULT_DIR / "server.sock"
ACTIVITY_FILE = DEFAULT_DIR / "server.last_activity"
LOCK_FILE = DEFAULT_DIR / "server.lock"

# Default idle timeout: 5 min warning, 10 min shutdown
IDLE_WARN_SECONDS = int(os.environ.get("MEMPALACE_IDLE_WARN", 300))
IDLE_SHUTDOWN_SECONDS = int(os.environ.get("MEMPALACE_IDLE_SHUTDOWN", 600))
CHECK_INTERVAL_SECONDS = 30
GRACEFUL_SHUTDOWN_WAIT = 30  # max seconds to wait for in-flight requests


# ---------------------------------------------------------------------------
# PID File Management
# ---------------------------------------------------------------------------

def write_pid_file(pid: int, extra: dict | None = None):
    """Write server PID file with metadata."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "pid": pid,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "socket": str(SOCKET_FILE),
        "python": sys.executable,
        **(extra or {}),
    }
    PID_FILE.write_text(json.dumps(data, indent=2))


def read_pid_file() -> dict | None:
    """Read PID file, returns None if missing or corrupt."""
    try:
        return json.loads(PID_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def remove_pid_file():
    """Remove PID file if it exists."""
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Process Liveness
# ---------------------------------------------------------------------------

def is_process_alive(pid: int) -> bool:
    """Check if a process is alive (Unix only)."""
    try:
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — treat as alive
        return True


def is_server_running() -> tuple[bool, int | None]:
    """
    Check if mempalace server is running.
    Returns (is_running, pid_or_none).
    """
    info = read_pid_file()
    if info is None:
        return False, None

    pid = info.get("pid")
    if pid is None:
        return False, None

    if not is_process_alive(pid):
        # Stale PID file — clean up
        cleanup_stale()
        return False, None

    # Process alive — check socket
    sock_path = info.get("socket", str(SOCKET_FILE))
    if not is_socket_reachable(sock_path):
        # Process alive but socket not responding — might be starting up
        # or crashed without cleanup. Check age.
        started = info.get("started_at", "")
        try:
            started_dt = datetime.fromisoformat(started)
            age = (datetime.now(timezone.utc) - started_dt).total_seconds()
            if age > 60:  # running >1 min but no socket = stale
                cleanup_stale()
                return False, None
        except (ValueError, TypeError):
            pass
        return True, pid  # give benefit of doubt if recently started

    return True, pid


# ---------------------------------------------------------------------------
# Socket Reachability
# ---------------------------------------------------------------------------

def is_socket_reachable(sock_path: str, timeout: float = 1.0) -> bool:
    """Check if a Unix domain socket is accepting connections."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock_path)
        s.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        try:
            s.close()
        except Exception:
            pass
        return False


def wait_for_socket(sock_path: str, timeout: float = 15.0) -> bool:
    """Wait until socket becomes reachable. Returns True if ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_socket_reachable(sock_path):
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Stale Cleanup
# ---------------------------------------------------------------------------

def cleanup_stale():
    """Remove stale PID file, socket file, and activity file."""
    for f in [PID_FILE, ACTIVITY_FILE]:
        try:
            f.unlink()
        except FileNotFoundError:
            pass

    # Remove socket file
    try:
        SOCKET_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Activity Tracking
# ---------------------------------------------------------------------------

def touch_activity():
    """Update last-activity timestamp."""
    ACTIVITY_FILE.write_text(datetime.now(timezone.utc).isoformat())


def read_last_activity() -> float | None:
    """Read seconds since last activity. Returns None if no activity file."""
    try:
        ts = ACTIVITY_FILE.read_text().strip()
        dt = datetime.fromisoformat(ts)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (FileNotFoundError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Idle Monitor Thread (runs inside server)
# ---------------------------------------------------------------------------

class IdleMonitor:
    """
    Background thread that checks idle time and triggers shutdown.
    
    Lifecycle:
      - Every CHECK_INTERVAL_SECONDS, check last activity
      - If idle > IDLE_SHUTDOWN_SECONDS → call shutdown callback
    """

    def __init__(self, shutdown_callback, idle_seconds=None):
        self.shutdown_callback = shutdown_callback
        self.idle_seconds = idle_seconds or IDLE_SHUTDOWN_SECONDS
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mempalace-idle-monitor"
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(CHECK_INTERVAL_SECONDS)
            if self._stop_event.is_set():
                break

            idle = read_last_activity()
            if idle is None:
                # No activity file — maybe just started
                continue

            if idle > self.idle_seconds:
                sys.stderr.write(
                    f"[mempalace] Idle for {idle:.0f}s > {self.idle_seconds}s, shutting down.\n"
                )
                self.shutdown_callback()
                break


# ---------------------------------------------------------------------------
# Graceful Shutdown Helper
# ---------------------------------------------------------------------------

class GracefulShutdown:
    """
    Installs signal handlers for SIGTERM/SIGINT.
    Calls the provided callback on signal receipt.
    """

    def __init__(self, callback):
        self.callback = callback
        self.received = False
        signal.signal(signal.SIGTERM, self._handler)
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, signum, frame):
        sig_name = signal.Signals(signum).name
        sys.stderr.write(f"[mempalace] Received {sig_name}, shutting down gracefully.\n")
        self.received = True
        self.callback()


# ---------------------------------------------------------------------------
# Lock file (prevent concurrent server starts)
# ---------------------------------------------------------------------------

def acquire_lock() -> bool:
    """Try to acquire server lock. Returns True if acquired."""
    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, f"{os.getpid()}\n".encode())
        os.close(lock_fd)
        return True
    except FileExistsError:
        # Lock exists — check if owner is alive
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if is_process_alive(pid):
                return False  # another server is starting
            else:
                LOCK_FILE.unlink()
                return acquire_lock()  # retry
        except (ValueError, FileNotFoundError):
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
            return acquire_lock()


def release_lock():
    """Release server lock."""
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass
