#!/usr/bin/env python3
"""
MemPalace Watcher - Background file monitor for auto-indexing.
"""

import os
import sys
import time
import signal
import subprocess
from pathlib import Path
from datetime import datetime

# Watch config
WATCH_INTERVAL = 30  # seconds between scans
DEBOUNCE_SECONDS = 5  # wait time after change before indexing

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", "coverage", ".mempalace",
}

WATCHABLE_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".json", ".yaml", ".yml", ".html", ".css", ".java", ".go",
    ".rs", ".rb", ".sh", ".csv", ".sql", ".toml",
}


def get_watch_pid_path(project_dir: str) -> Path:
    """Get PID file path for watcher."""
    project_path = Path(project_dir).expanduser().resolve()
    return project_path / ".mempalace" / "watch.pid"


def is_watch_running(project_dir: str) -> tuple[bool, int | None]:
    """Check if watcher is running for this project."""
    pid_path = get_watch_pid_path(project_dir)
    if not pid_path.exists():
        return False, None
    
    try:
        pid = int(pid_path.read_text().strip())
        # Check if process exists
        os.kill(pid, 0)  # Raises OSError if process doesn't exist
        return True, pid
    except (ValueError, OSError):
        # Stale PID file
        pid_path.unlink(missing_ok=True)
        return False, None


def stop_watch(project_dir: str) -> bool:
    """Stop watcher for this project."""
    running, pid = is_watch_running(project_dir)
    if not running:
        print("No watcher running for this project.")
        return True
    
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped watcher (PID {pid})")
        return True
    except OSError:
        # Process already gone
        get_watch_pid_path(project_dir).unlink(missing_ok=True)
        return True


def scan_files(project_dir: str) -> dict[str, float]:
    """Scan project for watchable files and their mtimes."""
    project_path = Path(project_dir).expanduser().resolve()
    files = {}
    
    for root, dirs, filenames in os.walk(project_path):
        # Skip excluded directories
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        
        for filename in filenames:
            filepath = Path(root) / filename
            if filepath.suffix.lower() in WATCHABLE_EXTENSIONS:
                try:
                    files[str(filepath)] = filepath.stat().st_mtime
                except OSError:
                    pass
    
    return files


def run_mine(project_dir: str):
    """Run mempalace mine for this project."""
    project_path = Path(project_dir).expanduser().resolve()
    
    # Find mempalace executable
    mempalace_exe = sys.executable.replace("python", "mempalace")
    if not Path(mempalace_exe).exists():
        mempalace_exe = "mempalace"
    
    # Run mine in background
    subprocess.run(
        [mempalace_exe, "mine", str(project_path), "--local"],
        capture_output=True,
        timeout=300,
    )


def watch_loop(project_dir: str, debounce: int = DEBOUNCE_SECONDS):
    """Main watch loop."""
    project_path = Path(project_dir).expanduser().resolve()
    pid_path = get_watch_pid_path(project_dir)
    
    # Ensure .mempalace dir exists
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write PID
    pid_path.write_text(str(os.getpid()))
    
    print(f"Watcher started for: {project_path}")
    print(f"PID: {os.getpid()}")
    print(f"Watching for file changes...")
    
    # Track file states
    last_files = scan_files(project_dir)
    last_change_time = None
    
    # Signal handler for clean shutdown
    def handle_signal(signum, frame):
        print("\nStopping watcher...")
        pid_path.unlink(missing_ok=True)
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    
    while True:
        time.sleep(WATCH_INTERVAL)
        
        # Scan current files
        current_files = scan_files(project_dir)
        
        # Check for changes
        changes = False
        new_files = set(current_files.keys()) - set(last_files.keys())
        removed_files = set(last_files.keys()) - set(current_files.keys())
        
        # Check mtimes for existing files
        for filepath, mtime in current_files.items():
            if filepath in last_files and last_files[filepath] != mtime:
                changes = True
                break
        
        if new_files or removed_files or changes:
            last_change_time = time.time()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Detected changes: +{len(new_files)} -{len(removed_files)} modified")
        
        # Debounce: only mine after changes settle
        if last_change_time and time.time() - last_change_time >= debounce:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Running mine...")
            try:
                run_mine(project_dir)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Mine complete")
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Mine failed: {e}")
            
            last_files = current_files
            last_change_time = None


def start_watch_daemon(project_dir: str):
    """Start watcher as daemon process."""
    project_path = Path(project_dir).expanduser().resolve()
    pid_path = get_watch_pid_path(project_dir)
    
    # Check if already running
    running, pid = is_watch_running(project_dir)
    if running:
        print(f"Watcher already running (PID {pid})")
        return
    
    # Daemonize using simple fork
    # First fork
    pid = os.fork()
    if pid > 0:
        print(f"Watcher started (PID {pid})")
        return
    
    # Second fork
    os.setsid()
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    
    # Redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()
    
    # Ensure log directory exists
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    log_path = pid_path.parent / "watch.log"
    with open(log_path, 'a') as log:
        os.dup2(log.fileno(), sys.stdout.fileno())
        os.dup2(log.fileno(), sys.stderr.fileno())
    
    # Close stdin
    sys.stdin.close()
    
    # Run watch loop
    watch_loop(project_dir)
