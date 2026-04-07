"""
MemPalace HTTP Server

A lightweight HTTP server that:
  - Manages a single ChromaDB instance
  - Handles search/mine/add/kg requests
  - Auto-shutdowns after idle timeout
  - Gracefully handles SIGTERM/SIGINT

Usage:
  mempalace serve                    # foreground
  mempalace serve --daemon           # background
  mempalace serve --port 7654        # HTTP port (instead of Unix socket)
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from . import __version__
from .lifecycle import (
    DEFAULT_DIR,
    SOCKET_FILE,
    PID_FILE,
    acquire_lock,
    release_lock,
    write_pid_file,
    remove_pid_file,
    cleanup_stale,
    touch_activity,
    IdleMonitor,
    GracefulShutdown,
    is_socket_reachable,
)


# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------

class ServerState:
    """Shared state for the server."""
    palace_path: str = str(DEFAULT_DIR / "palace")
    chroma_client = None
    collection = None
    shutdown_requested = False
    active_requests = 0
    started_at = None


state = ServerState()


# ---------------------------------------------------------------------------
# ChromaDB Helper (lazy load)
# ---------------------------------------------------------------------------

def get_chroma():
    """Lazy-load ChromaDB client and collection."""
    if state.chroma_client is None:
        import chromadb
        state.chroma_client = chromadb.PersistentClient(path=state.palace_path)
        try:
            state.collection = state.chroma_client.get_collection("mempalace_drawers")
        except Exception:
            state.collection = state.chroma_client.create_collection("mempalace_drawers")
    return state.chroma_client, state.collection


def get_knowledge_graph():
    """Lazy-load knowledge graph."""
    from .knowledge_graph import KnowledgeGraph
    kg_path = Path(state.palace_path).parent / "knowledge_graph.sqlite3"
    return KnowledgeGraph(str(kg_path))


# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------

class MempalaceHandler(BaseHTTPRequestHandler):
    """HTTP handler for mempalace API."""

    # Suppress default logging
    def log_message(self, format, *args):
        # Only log errors
        if "ERROR" in format or "500" in str(args):
            sys.stderr.write(f"[mempalace] {format % args}\n")

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _update_activity(self):
        """Update activity timestamp."""
        touch_activity()
        state.active_requests += 1

    def _finish_request(self):
        state.active_requests -= 1

    # --- Routes ---

    def do_GET(self):
        path = urlparse(self.path).path
        self._update_activity()

        try:
            if path == "/status":
                self._handle_status()
            elif path == "/health":
                self._send_json({"status": "ok", "version": __version__})
            else:
                self._send_json({"error": "Not found"}, status=404)
        finally:
            self._finish_request()

    def do_POST(self):
        path = urlparse(self.path).path
        self._update_activity()

        try:
            data = self._read_json()

            if path == "/search":
                self._handle_search(data)
            elif path == "/mine":
                self._handle_mine(data)
            elif path == "/add":
                self._handle_add(data)
            elif path == "/delete":
                self._handle_delete(data)
            elif path == "/kg/add":
                self._handle_kg_add(data)
            elif path == "/kg/query":
                self._handle_kg_query(data)
            elif path == "/keepalive":
                self._send_json({"status": "ok"})
            elif path == "/shutdown":
                self._handle_shutdown()
            else:
                self._send_json({"error": "Not found"}, status=404)
        finally:
            self._finish_request()

    # --- Handlers ---

    def _handle_status(self):
        """Return server status and wing overview."""
        _, collection = get_chroma()

        # Count drawers per wing
        all_items = collection.get()
        wings = {}
        for meta in all_items.get("metadatas", []):
            wing = meta.get("wing", "unknown")
            room = meta.get("room", "unknown")
            if wing not in wings:
                wings[wing] = {}
            if room not in wings[wing]:
                wings[wing][room] = 0
            wings[wing][room] += 1

        total = len(all_items.get("ids", []))

        self._send_json({
            "version": __version__,
            "palace_path": state.palace_path,
            "started_at": state.started_at,
            "total_drawers": total,
            "wings": wings,
            "active_requests": state.active_requests,
        })

    def _handle_search(self, data):
        """Semantic search."""

        query = data.get("query", "")
        wing = data.get("wing")
        room = data.get("room")
        n_results = data.get("n_results", 5)
        palace_path = data.get("palace_path", state.palace_path)

        # Override state palace_path if client specifies
        if palace_path != state.palace_path:
            state.palace_path = palace_path
            state.chroma_client = None  # force reload

        from .searcher import search_memories
        results = search_memories(query, palace_path, wing, room, n_results)
        self._send_json({"results": results})

    def _handle_mine(self, data):
        """Mine files."""
        from .miner import mine

        directory = data.get("directory")
        wing_override = data.get("wing")
        palace_path = data.get("palace_path", state.palace_path)

        if not directory:
           self._send_json({"error": "directory required"}, status=400)
           return

        if palace_path != state.palace_path:
           state.palace_path = palace_path
           state.chroma_client = None

        result = mine(
            project_dir=directory,
            palace_path=palace_path,
            wing_override=wing_override,
        )
        self._send_json(result)

    def _handle_add(self, data):
        """Add drawer manually."""
        from .miner import add_drawer

        wing = data.get("wing", "default")
        room = data.get("room", "general")
        content = data.get("content")
        source_file = data.get("source_file", "manual")
        added_by = data.get("added_by", "cli")
        palace_path = data.get("palace_path", state.palace_path)

        if not content:
            self._send_json({"error": "content required"}, status=400)
            return

        if palace_path != state.palace_path:
            state.palace_path = palace_path
            state.chroma_client = None

        drawer_id = add_drawer(wing, room, content, source_file, added_by, palace_path)
        self._send_json({"status": "ok", "drawer_id": drawer_id})

    def _handle_delete(self, data):
        """Delete drawer by ID."""
        _, collection = get_chroma()

        drawer_id = data.get("drawer_id")
        if not drawer_id:
            self._send_json({"error": "drawer_id required"}, status=400)
            return

        collection.delete(ids=[drawer_id])
        self._send_json({"status": "ok", "deleted": drawer_id})

    def _handle_kg_add(self, data):
        """Add knowledge triple."""
        kg = get_knowledge_graph()

        subject = data.get("subject")
        predicate = data.get("predicate")
        obj = data.get("object")
        valid_from = data.get("valid_from")
        valid_to = data.get("valid_to")

        if not all([subject, predicate, obj]):
            self._send_json({"error": "subject, predicate, object required"}, status=400)
            return

        triple_id = kg.add_triple(subject, predicate, obj, valid_from, valid_to)
        self._send_json({"status": "ok", "triple_id": triple_id})

    def _handle_kg_query(self, data):
        """Query knowledge graph."""
        kg = get_knowledge_graph()

        subject = data.get("subject")
        predicate = data.get("predicate")
        obj = data.get("object")

        triples = kg.query(subject, predicate, obj)
        self._send_json({"results": triples})

    def _handle_shutdown(self):
        """Graceful shutdown."""
        state.shutdown_requested = True
        self._send_json({"status": "ok", "message": "Shutting down"})
        # Trigger shutdown in main thread
        threading.Thread(target=lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


# ---------------------------------------------------------------------------
# Threading HTTP Server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server for concurrent requests."""
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Server Main
# ---------------------------------------------------------------------------

def run_server(
    palace_path: str | None = None,
    socket_path: str | None = None,
    port: int | None = None,
    daemon: bool = False,
    idle_seconds: int | None = None,
):
    """
    Run mempalace server.

    Args:
        palace_path: Path to ChromaDB (default: ~/.mempalace/palace)
        socket_path: Unix socket path (default: ~/.mempalace/server.sock)
        port: HTTP port (if specified, uses HTTP instead of socket)
        daemon: If True, detach from terminal
        idle_seconds: Auto-shutdown after this idle time (default: 600s)
    """
    # --- Lock & Stale Cleanup ---
    if not acquire_lock():
        sys.stderr.write("[mempalace] Another server is starting, exiting.\n")
        sys.exit(1)

    cleanup_stale()

    # --- Paths ---
    state.palace_path = palace_path or str(DEFAULT_DIR / "palace")
    socket_path = socket_path or str(SOCKET_FILE)

    # --- Daemon Mode ---
    if daemon:
        # Double-fork to detach
        pid = os.fork()
        if pid > 0:
            sys.exit(0)  # parent exits

        os.setsid()
        pid = os.fork()
        if pid > 0:
            sys.exit(0)  # intermediate exits

        # Redirect stdout/stderr
        sys.stdout.flush()
        sys.stderr.flush()
        si = open(os.devnull, "r")
        so = open(os.devnull, "a+")
        se = open(os.devnull, "a+")
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())

    # --- PID File ---
    write_pid_file(os.getpid(), {"socket": socket_path, "palace": state.palace_path})
    state.started_at = time.time()
    touch_activity()

    # --- Shutdown Handler ---
    def do_shutdown():
        if state.shutdown_requested:
            return  # already shutting down
        state.shutdown_requested = True

        # Wait for active requests
        for _ in range(30):
            if state.active_requests == 0:
                break
            time.sleep(1)

        # Cleanup
        remove_pid_file()
        cleanup_stale()
        release_lock()

        # Remove socket file
        try:
            Path(socket_path).unlink()
        except FileNotFoundError:
            pass

        sys.stderr.write("[mempalace] Server stopped.\n")
        os._exit(0)

    shutdown_handler = GracefulShutdown(do_shutdown)
    idle_monitor = IdleMonitor(do_shutdown, idle_seconds)
    idle_monitor.start()

    # --- Create Server ---
    if port:
        # HTTP mode
        server = ThreadedHTTPServer(("127.0.0.1", port), MempalaceHandler)
        addr = f"http://127.0.0.1:{port}"
    else:
        # Unix socket mode
        # Remove old socket if exists
        try:
            Path(socket_path).unlink()
        except FileNotFoundError:
            pass

        server = ThreadedHTTPServer(("127.0.0.1", 0), MempalaceHandler)

        # Bind to Unix socket instead
        class UnixSocketServer(ThreadedHTTPServer):
            def server_bind(self):
                self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.socket.bind(socket_path)
                self.socket.listen(5)

        server = UnixSocketServer(("", 0), MempalaceHandler)
        addr = f"unix:{socket_path}"

    # --- Run ---
    sys.stderr.write(f"[mempalace] Server started (v{__version__})\n")
    sys.stderr.write(f"[mempalace] Listening on {addr}\n")
    sys.stderr.write(f"[mempalace] Palace: {state.palace_path}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        do_shutdown()


if __name__ == "__main__":
    run_server()
