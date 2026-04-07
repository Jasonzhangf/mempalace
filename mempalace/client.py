"""
MemPalace Thin Client

Auto-detects / starts / talks to mempalace server.
All heavy work (ChromaDB, embeddings) stays in the server.
"""

import json
import os
import socket
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from .lifecycle import (
    DEFAULT_DIR,
    SOCKET_FILE,
    PID_FILE,
    is_server_running,
    is_socket_reachable,
    wait_for_socket,
    cleanup_stale,
    read_pid_file,
)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _get_socket_path() -> str:
    return str(SOCKET_FILE)


def _make_request(method: str, path: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    """
    Send HTTP request to server via Unix socket.
    Auto-ensures server is running.
    """
    _ensure_server()
    sock_path = _get_socket_path()

    # Build HTTP request manually over Unix socket
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock_path)

        # Build HTTP request
        body_bytes = json.dumps(body).encode() if body else b""
        headers = {
            "Host": "mempalace",
            "Content-Type": "application/json",
            "Content-Length": str(len(body_bytes)),
            "Connection": "close",
        }
        header_str = f"{method} {path} HTTP/1.1\r\n"
        for k, v in headers.items():
            header_str += f"{k}: {v}\r\n"
        header_str += "\r\n"

        s.sendall(header_str.encode() + body_bytes)

        # Read response
        response = b""
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                response += chunk
            except socket.timeout:
                break
        s.close()

        # Parse HTTP response
        return _parse_http_response(response)

    except ConnectionRefusedError:
        # Server might have just shut down, retry once
        cleanup_stale()
        _ensure_server()
        return _make_request(method, path, body, timeout)
    except FileNotFoundError:
        cleanup_stale()
        _ensure_server()
        return _make_request(method, path, body, timeout)


def _parse_http_response(raw: bytes) -> dict:
    """Parse raw HTTP response, return JSON body."""
    # Split headers and body
    parts = raw.split(b"\r\n\r\n", 1)
    if len(parts) < 2:
        raise RuntimeError(f"Invalid HTTP response: {raw[:200]}")

    header_section = parts[0].decode()
    body = parts[1]

    # Check status
    status_line = header_section.split("\r\n")[0]
    status_code = int(status_line.split(" ")[1])

    if not body:
        return {"status": "empty", "http_code": status_code}

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"status": "raw", "http_code": status_code, "body": body.decode(errors="replace")}


# ---------------------------------------------------------------------------
# Server Management
# ---------------------------------------------------------------------------

def _ensure_server():
    """Ensure server is running. Start it if not."""
    running, pid = is_server_running()
    if running:
        return

    _start_server_daemon()


def _start_server_daemon():
    """Start server in daemon mode."""
    # Clean up any stale files
    cleanup_stale()

    # Find the executable: prefer the same binary/script that's running us
    python = sys.executable

    # Try using the mempalace entry point
    cmd = [python, "-m", "mempalace", "serve", "--daemon"]

    # If running from a pyinstaller binary, use that
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "serve", "--daemon"]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from parent
        )
    except FileNotFoundError:
        # Fallback: direct module execution
        cmd = [python, "-c", "from mempalace.server import run_server; run_server(daemon=True)"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Wait for server to be ready
    sock_path = _get_socket_path()
    ready = wait_for_socket(sock_path, timeout=15.0)
    if not ready:
        raise RuntimeError("mempalace server failed to start within 15 seconds")


def stop_server() -> bool:
    """Send shutdown request to server."""
    try:
        result = _make_request("POST", "/shutdown", {})
        return result.get("status") == "ok"
    except Exception:
        # Force cleanup
        cleanup_stale()
        return False


# ---------------------------------------------------------------------------
# High-Level API
# ---------------------------------------------------------------------------

def status() -> dict:
    """Get server status."""
    return _make_request("GET", "/status")


def search(query: str, wing: str | None = None, room: str | None = None, n_results: int = 5) -> dict:
    """Semantic search."""
    body = {"query": query, "n_results": n_results}
    if wing:
        body["wing"] = wing
    if room:
        body["room"] = room
    return _make_request("POST", "/search", body)


def mine(directory: string, wing: str | None = None, palace_path: str | None = None) -> dict:
    """Mine files from directory."""
    body = {"directory": directory}
    if wing:
        body["wing"] = wing
    if palace_path:
        body["palace_path"] = palace_path
    return _make_request("POST", "/mine", body, timeout=300.0)


def add_drawer(wing: str, room: str, content: str, source_file: str = "manual", added_by: str = "cli") -> dict:
    """Add a drawer manually."""
    return _make_request("POST", "/add", {
        "wing": wing,
        "room": room,
        "content": content,
        "source_file": source_file,
        "added_by": added_by,
    })


def delete_drawer(drawer_id: str) -> dict:
    """Delete a drawer by ID."""
    return _make_request("POST", "/delete", {"drawer_id": drawer_id})


def kg_add(subject: str, predicate: str, obj: str, valid_from: str | None = None, valid_to: str | None = None) -> dict:
    """Add knowledge graph triple."""
    body = {"subject": subject, "predicate": predicate, "object": obj}
    if valid_from:
        body["valid_from"] = valid_from
    if valid_to:
        body["valid_to"] = valid_to
    return _make_request("POST", "/kg/add", body)


def kg_query(subject: str | None = None, predicate: str | None = None, obj: str | None = None) -> dict:
    """Query knowledge graph."""
    body = {}
    if subject:
        body["subject"] = subject
    if predicate:
        body["predicate"] = predicate
    if obj:
        body["object"] = obj
    return _make_request("POST", "/kg/query", body)


def keepalive() -> dict:
    """Send keepalive to server."""
    return _make_request("POST", "/keepalive", {})
def mine(directory: str, wing: str | None = None, palace_path: str | None = None) -> dict:
    """Mine files from directory."""
    body = {"directory": directory}
    if wing:
        body["wing"] = wing
    if palace_path:
        body["palace_path"] = palace_path
    return _make_request("POST", "/mine", body, timeout=300.0)
