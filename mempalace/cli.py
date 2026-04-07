#!/usr/bin/env python3
"""
MemPalace — Give your AI a memory. No API key required.

Two ways to ingest:
  Projects:      mempalace mine ~/projects/my_app          (code, docs, notes)
  Conversations: mempalace mine ~/chats/ --mode convos     (Claude, ChatGPT, Slack)

Same palace. Same search. Different ingest strategies.

Commands:
    mempalace init <dir>                  Detect rooms from folder structure
    mempalace split <dir>                 Split concatenated mega-files into per-session files
    mempalace mine <dir>                  Mine project files (default)
    mempalace mine <dir> --mode convos    Mine conversation exports
    mempalace search "query"              Find anything, exact words
    mempalace wake-up                     Show L0 + L1 wake-up context
    mempalace wake-up --wing my_app       Wake-up for a specific project
    mempalace status                      Show what's been filed

Examples:
    mempalace init ~/projects/my_app
    mempalace mine ~/projects/my_app
    mempalace mine ~/chats/claude-sessions --mode convos
    mempalace search "why did we switch to GraphQL"
    mempalace search "pricing discussion" --wing my_app --room costs
"""

import os
import sys
import argparse
from . import __version__
from pathlib import Path

from .config import MempalaceConfig
from .backup import cmd_backup, cmd_restore, cmd_backup_list, cmd_backup_test


def cmd_serve(args):
    """Start mempalace server."""
    from .server import run_server

    run_server(
        palace_path=args.palace,
        socket_path=args.socket,
        port=args.port,
        daemon=args.daemon,
        idle_seconds=args.idle,
    )


def cmd_stop(args):
    """Stop mempalace server."""
    from .client import stop_server, cleanup_stale
    from .lifecycle import is_server_running

    running, pid = is_server_running()
    if not running:
        print("No server running.")
        return

    print(f"Stopping server (PID {pid})...")
    if stop_server():
        print("Server stopped.")
    else:
        # Force cleanup
        cleanup_stale()
        print("Server force-stopped (cleanup).")


def cmd_init(args):
    import json
    from pathlib import Path
    from .entity_detector import scan_for_detection, detect_entities, confirm_entities
    from .room_detector_local import detect_rooms_local

    # Pass 1: auto-detect people and projects from file content
    print(f"\n  Scanning for entities in: {args.dir}")
    files = scan_for_detection(args.dir)
    if files:
        print(f"  Reading {len(files)} files...")
        detected = detect_entities(files)
        total = len(detected["people"]) + len(detected["projects"]) + len(detected["uncertain"])
        if total > 0:
            confirmed = confirm_entities(detected, yes=getattr(args, "yes", False))
            # Save confirmed entities to <project>/entities.json for the miner
            if confirmed["people"] or confirmed["projects"]:
                entities_path = Path(args.dir).expanduser().resolve() / "entities.json"
                with open(entities_path, "w") as f:
                    json.dump(confirmed, f, indent=2)
                print(f"  Entities saved: {entities_path}")
        else:
            print("  No entities detected — proceeding with directory-based rooms.")

    # Pass 2: detect rooms from folder structure
    detect_rooms_local(project_dir=args.dir, yes=getattr(args, "yes", False))
    MempalaceConfig().init()


def cmd_mine(args):
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    if not args.local:
        try:
            from .client import mine as client_mine
            result = client_mine(args.dir, wing=args.wing, palace_path=palace_path)
            print(result)
            return
        except Exception as e:
            print(f"Server mode failed: {e}, falling back to local...")

    if args.mode == "convos":
        from .convo_miner import mine_convos

        mine_convos(
            convo_dir=args.dir,
            palace_path=palace_path,
            wing=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            extract_mode=args.extract,
        )
    else:
        from .miner import mine

        mine(
            project_dir=args.dir,
            palace_path=palace_path,
            wing_override=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
        )


def cmd_search(args):
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    if not args.local:
        try:
            from .client import search as client_search
            result = client_search(args.query, wing=args.wing, room=args.room, n_results=args.results)
            _print_search_results(result)
            return
        except Exception:
            pass  # fallback to local
    from .searcher import search
    search(
        query=args.query,
        palace_path=palace_path,
        wing=args.wing,
        room=args.room,
        n_results=args.results,
    )


def _print_search_results(result):
    """Print search results from client response."""
    # Handle nested structure from search_memories
    data = result.get("results", {})
    if isinstance(data, dict):
        results = data.get("results", [])
    else:
        results = data if isinstance(data, list) else []

    if not results:
        print("No results found.")
        return
    for i, r in enumerate(results, 1):
        if isinstance(r, dict):
            text = r.get("text", r.get("content", str(r)))
            wing = r.get("wing", "")
            room = r.get("room", "")
            sim = r.get("similarity", r.get("distance", ""))
            loc = f"  [{wing}/{room}]" if wing else ""
            score = f" (score: {sim:.3f})" if isinstance(sim, (int, float)) else ""
            print(f"\n--- Result {i}{loc}{score} ---")
            print(text[:500])
        else:
            print(f"\n--- Result {i} ---")
            print(str(r)[:500])


def cmd_wakeup(args):
    """Show L0 (identity) + L1 (essential story) — the wake-up context."""
    from .layers import MemoryStack

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    stack = MemoryStack(palace_path=palace_path)

    text = stack.wake_up(wing=args.wing)
    tokens = len(text) // 4
    print(f"Wake-up text (~{tokens} tokens):")
    print("=" * 50)
    print(text)


def cmd_split(args):
    """Split concatenated transcript mega-files into per-session files."""
    from .split_mega_files import main as split_main
    import sys

    # Rebuild argv for split_mega_files argparse
    argv = [args.dir]
    if args.output_dir:
        argv += ["--output-dir", args.output_dir]
    if args.dry_run:
        argv.append("--dry-run")
    if args.min_sessions != 2:
        argv += ["--min-sessions", str(args.min_sessions)]

    old_argv = sys.argv
    sys.argv = ["mempalace split"] + argv
    try:
        split_main()
    finally:
        sys.argv = old_argv


def cmd_status(args):
    if not args.local:
        try:
            from .client import status as client_status
            result = client_status()
            _print_status(result)
            return
        except Exception as e:
            pass  # fallback to local

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    from .miner import status
    status(palace_path=palace_path)


def _print_status(result):
    """Print status from client response."""
    wings = result.get("wings", {})
    total = result.get("total_drawers", 0)
    print(f"\n{'=' * 55}")
    print(f"  MemPalace Status — {total} drawers")
    print(f"{'=' * 55}")
    for wing_name, rooms in sorted(wings.items()):
        print(f"\n  WING: {wing_name}")
        for room_name, count in sorted(rooms.items(), key=lambda x: x[1], reverse=True):
            print(f"    ROOM: {room_name:20} {count} drawers")
    print(f"\n{'=' * 55}\n")


def cmd_add(args):
    """Manually add a memory drawer."""
    from .miner import get_collection, add_drawer
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    collection = get_collection(palace_path)
    
    import hashlib
    from datetime import datetime
    drawer_id = f"drawer_{args.wing}_{args.room}_{hashlib.md5(args.content.encode()).hexdigest()[:16]}"
    
    success = add_drawer(
        collection=collection,
        wing=args.wing,
        room=args.room,
        content=args.content,
        source_file=args.source or "manual",
        chunk_index=0,
        agent="cli"
    )
    if success:
        print(f"✓ Added drawer: {drawer_id}")
        print(f"  Wing: {args.wing}")
        print(f"  Room: {args.room}")
    else:
        print("✗ Failed to add drawer (possibly duplicate)")


def cmd_delete(args):
    """Delete a drawer by ID."""
    import chromadb
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_collection("mempalace_drawers")
    
    existing = col.get(ids=[args.drawer_id])
    if not existing["ids"]:
        print(f"✗ Drawer not found: {args.drawer_id}")
        return
    
    col.delete(ids=[args.drawer_id])
    print(f"✓ Deleted drawer: {args.drawer_id}")


def cmd_kg(args):
    """Show knowledge graph overview."""
    from .knowledge_graph import KnowledgeGraph
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    kg_path = os.path.join(os.path.dirname(palace_path), "knowledge_graph.sqlite3")
    
    kg = KnowledgeGraph(kg_path)
    stats = kg.get_stats()
    
    print(f"\n{'=' * 50}")
    print("  Knowledge Graph Overview")
    print(f"{'=' * 50}")
    print(f"  Entities:    {stats['entities']}")
    print(f"  Relations:   {stats['relations']}")
    print(f"  Triples:     {stats['triples']}")
    print(f"{'─' * 50}\n")


def cmd_kg_add(args):
    """Add a knowledge triple."""
    from .knowledge_graph import KnowledgeGraph
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    kg_path = os.path.join(os.path.dirname(palace_path), "knowledge_graph.sqlite3")
    
    kg = KnowledgeGraph(kg_path)
    triple_id = kg.add_triple(
        args.subject,
        args.predicate,
        args.object,
        valid_from=args.valid_from,
        source_closet=args.source
    )
    print(f"✓ Added triple: {args.subject} → {args.predicate} → {args.object}")
    print(f"  ID: {triple_id}")


def cmd_kg_query(args):
    """Query the knowledge graph."""
    from .knowledge_graph import KnowledgeGraph
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    kg_path = os.path.join(os.path.dirname(palace_path), "knowledge_graph.sqlite3")
    
    kg = KnowledgeGraph(kg_path)
    results = kg.query_entity(args.entity, as_of=args.as_of, direction=args.direction)
    
    print(f"\n{'=' * 50}")
    print(f"  Knowledge Graph Query: {args.entity}")
    if args.as_of:
        print(f"  As of: {args.as_of}")
    print(f"{'=' * 50}\n")
    
    if not results:
        print("  No results found")
    else:
        for r in results:
            print(f"  {r['subject']} → {r['predicate']} → {r['object']}")
            if r.get('valid_from'):
                print(f"    Valid from: {r['valid_from']}")
    print()


def cmd_compress(args):
    """Compress drawers in a wing using AAAK Dialect."""
    import chromadb
    from .dialect import Dialect

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    # Load dialect (with optional entity config)
    config_path = args.config
    if not config_path:
        for candidate in ["entities.json", os.path.join(palace_path, "entities.json")]:
            if os.path.exists(candidate):
                config_path = candidate
                break

    if config_path and os.path.exists(config_path):
        dialect = Dialect.from_config(config_path)
        print(f"  Loaded entity config: {config_path}")
    else:
        dialect = Dialect()

    # Connect to palace
    try:
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        sys.exit(1)

    # Query drawers in the wing
    where = {"wing": args.wing} if args.wing else None
    try:
        kwargs = {"include": ["documents", "metadatas"]}
        if where:
            kwargs["where"] = where
        results = col.get(**kwargs)
    except Exception as e:
        print(f"\n  Error reading drawers: {e}")
        sys.exit(1)

    docs = results["documents"]
    metas = results["metadatas"]
    ids = results["ids"]

    if not docs:
        wing_label = f" in wing '{args.wing}'" if args.wing else ""
        print(f"\n  No drawers found{wing_label}.")
        return

    print(
        f"\n  Compressing {len(docs)} drawers"
        + (f" in wing '{args.wing}'" if args.wing else "")
        + "..."
    )
    print()

    total_original = 0
    total_compressed = 0
    compressed_entries = []

    for doc, meta, doc_id in zip(docs, metas, ids):
        compressed = dialect.compress(doc, metadata=meta)
        stats = dialect.compression_stats(doc, compressed)

        total_original += stats["original_chars"]
        total_compressed += stats["compressed_chars"]

        compressed_entries.append((doc_id, compressed, meta, stats))

        if args.dry_run:
            wing_name = meta.get("wing", "?")
            room_name = meta.get("room", "?")
            source = Path(meta.get("source_file", "?")).name
            print(f"  [{wing_name}/{room_name}] {source}")
            print(
                f"    {stats['original_tokens']}t -> {stats['compressed_tokens']}t ({stats['ratio']:.1f}x)"
            )
            print(f"    {compressed}")
            print()

    # Store compressed versions (unless dry-run)
    if not args.dry_run:
        try:
            comp_col = client.get_or_create_collection("mempalace_compressed")
            for doc_id, compressed, meta, stats in compressed_entries:
                comp_meta = dict(meta)
                comp_meta["compression_ratio"] = round(stats["ratio"], 1)
                comp_meta["original_tokens"] = stats["original_tokens"]
                comp_col.upsert(
                    ids=[doc_id],
                    documents=[compressed],
                    metadatas=[comp_meta],
                )
            print(
                f"  Stored {len(compressed_entries)} compressed drawers in 'mempalace_compressed' collection."
            )
        except Exception as e:
            print(f"  Error storing compressed drawers: {e}")
            sys.exit(1)

    # Summary
    ratio = total_original / max(total_compressed, 1)
    orig_tokens = Dialect.count_tokens("x" * total_original)
    comp_tokens = Dialect.count_tokens("x" * total_compressed)
    print(f"  Total: {orig_tokens:,}t -> {comp_tokens:,}t ({ratio:.1f}x compression)")
    if args.dry_run:
        print("  (dry run -- nothing stored)")


def main():
    parser = argparse.ArgumentParser(
        description="MemPalace — Give your AI a memory. No API key required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--palace",
        default=None,
        help="Where the palace lives (default: from ~/.mempalace/config.json or ~/.mempalace/palace)",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"mempalace {__version__}",
    )

    sub = parser.add_subparsers(dest="command")

    # serve
    p_serve = sub.add_parser("serve", help="Start mempalace server (daemon mode)")
    p_serve.add_argument("--daemon", action="store_true", help="Run in background")
    p_serve.add_argument("--port", type=int, default=None, help="HTTP port (default: Unix socket)")
    p_serve.add_argument("--socket", default=None, help="Unix socket path")
    p_serve.add_argument("--idle", type=int, default=600, help="Idle timeout before auto-shutdown (seconds)")

    # stop
    p_stop = sub.add_parser("stop", help="Stop mempalace server")

    # init
    p_init = sub.add_parser("init", help="Detect rooms from your folder structure")
    p_init.add_argument("dir", help="Project directory to set up")
    p_init.add_argument(
        "--yes", action="store_true", help="Auto-accept all detected entities (non-interactive)"
    )

    # mine
    p_mine = sub.add_parser("mine", help="Mine files into the palace")
    p_mine.add_argument("dir", help="Directory to mine")
    p_mine.add_argument("--local", action="store_true", help="Force local mine (skip server)")
    p_mine.add_argument(
        "--mode",
        choices=["projects", "convos"],
        default="projects",
        help="Ingest mode: 'projects' for code/docs (default), 'convos' for chat exports",
    )
    p_mine.add_argument("--wing", default=None, help="Wing name (default: directory name)")
    p_mine.add_argument(
        "--agent",
        default="mempalace",
        help="Your name — recorded on every drawer (default: mempalace)",
    )
    p_mine.add_argument("--limit", type=int, default=0, help="Max files to process (0 = all)")
    p_mine.add_argument(
        "--dry-run", action="store_true", help="Show what would be filed without filing"
    )
    p_mine.add_argument(
        "--extract",
        choices=["exchange", "general"],
        default="exchange",
        help="Extraction strategy for convos mode: 'exchange' (default) or 'general' (5 memory types)",
    )

    # search
    p_search = sub.add_parser("search", help="Find anything, exact words")
    p_search.add_argument("query", help="What to search for")
    p_search.add_argument("--wing", default=None, help="Limit to one project")
    p_search.add_argument("--room", default=None, help="Limit to one room")
    p_search.add_argument("--results", type=int, default=5, help="Number of results")

    p_search.add_argument("--local", action="store_true", help="Force local search (skip server)")

    # compress
    p_compress = sub.add_parser(
        "compress", help="Compress drawers using AAAK Dialect (~30x reduction)"
    )
    p_compress.add_argument("--wing", default=None, help="Wing to compress (default: all wings)")
    p_compress.add_argument(
        "--dry-run", action="store_true", help="Preview compression without storing"
    )
    p_compress.add_argument(
        "--config", default=None, help="Entity config JSON (e.g. entities.json)"
    )

    # wake-up
    p_wakeup = sub.add_parser("wake-up", help="Show L0 + L1 wake-up context (~600-900 tokens)")
    p_wakeup.add_argument("--wing", default=None, help="Wake-up for a specific project/wing")

    # split
    p_split = sub.add_parser(
        "split",
        help="Split concatenated transcript mega-files into per-session files (run before mine)",
    )
    p_split.add_argument("dir", help="Directory containing transcript files")
    p_split.add_argument(
        "--output-dir",
        default=None,
        help="Write split files here (default: same directory as source files)",
    )
    p_split.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be split without writing files",
    )
    p_split.add_argument(
        "--min-sessions",
        type=int,
        default=2,
        help="Only split files containing at least N sessions (default: 2)",
    )

    # status
    p_status = sub.add_parser("status", help="Show what's been filed")
    p_status.add_argument("--local", action="store_true", help="Force local status (skip server)")

    # add - manually add a drawer
    p_add = sub.add_parser("add", help="Manually add a memory drawer")
    p_add.add_argument("content", help="Content to store")
    p_add.add_argument("--wing", default="manual", help="Wing/project name (default: manual)")
    p_add.add_argument("--room", default="general", help="Room/category (default: general)")
    p_add.add_argument("--source", default=None, help="Source file name")

    # delete - remove a drawer
    p_delete = sub.add_parser("delete", help="Delete a drawer by ID")
    p_delete.add_argument("drawer_id", help="Drawer ID to delete")

    # kg - knowledge graph overview
    p_kg = sub.add_parser("kg", help="Show knowledge graph overview")

    # kg-add - add knowledge triple
    p_kg_add = sub.add_parser("kg-add", help="Add a knowledge triple")
    p_kg_add.add_argument("subject", help="Subject entity")
    p_kg_add.add_argument("predicate", help="Relationship type")
    p_kg_add.add_argument("object", help="Object entity")
    p_kg_add.add_argument("--valid-from", default=None, help="When this became true (YYYY-MM-DD)")
    p_kg_add.add_argument("--source", default=None, help="Source drawer ID")

    # kg-query - query knowledge graph
    p_kg_query = sub.add_parser("kg-query", help="Query the knowledge graph")
    p_kg_query.add_argument("entity", help="Entity to query")
    p_kg_query.add_argument("--as-of", default=None, help="Query as of date (YYYY-MM-DD)")
    p_kg_query.add_argument("--direction", default="both", choices=["out", "in", "both"], help="Relationship direction")

    # Backup commands
    p_backup = sub.add_parser("backup", help="Backup MemPalace to WebDAV or local ZIP")
    p_backup.add_argument("--webdav", action="store_true", help="Upload to WebDAV (default if configured)")
    p_backup.add_argument("--output", "-o", help="Local output path for ZIP file")
    p_backup.add_argument("--keep", action="store_true", help="Keep local ZIP after WebDAV upload")

    p_restore = sub.add_parser("restore", help="Restore MemPalace from backup")
    p_restore.add_argument("--file", "-f", help="Restore from local ZIP file")
    p_restore.add_argument("--webdav", action="store_true", help="Restore from WebDAV")
    p_restore.add_argument("--latest", action="store_true", help="Restore latest backup from WebDAV")
    p_restore.add_argument("--force", action="store_true", help="Skip confirmation prompt")

    p_backup_list = sub.add_parser("backup-list", help="List available backups on WebDAV")

    p_backup_test = sub.add_parser("backup-test", help="Test WebDAV connection")
    p_backup_test.add_argument("--url", help="WebDAV URL (override config)")
    p_backup_test.add_argument("--username", "-u", help="WebDAV username (override config)")
    p_backup_test.add_argument("--password", "-p", help="WebDAV password (override config)")
    p_backup_test.add_argument("--path", help="WebDAV remote path (override config)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    dispatch = {
        "serve": cmd_serve,
        "stop": cmd_stop,
        "init": cmd_init,
        "mine": cmd_mine,
        "split": cmd_split,
        "search": cmd_search,
        "compress": cmd_compress,
        "wake-up": cmd_wakeup,
        "status": cmd_status,
    }
    dispatch["add"] = cmd_add
    dispatch["delete"] = cmd_delete
    dispatch["kg"] = cmd_kg
    dispatch["kg-add"] = cmd_kg_add
    dispatch["kg-query"] = cmd_kg_query
    dispatch["backup"] = cmd_backup
    dispatch["restore"] = cmd_restore
    dispatch["backup-list"] = cmd_backup_list
    dispatch["backup-test"] = cmd_backup_test
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
