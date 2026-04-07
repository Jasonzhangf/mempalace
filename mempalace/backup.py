"""
MemPalace backup system with WebDAV support.

Features:
- Backup: pack ~/.mempalace into timestamped ZIP, upload to WebDAV
- Restore: download ZIP from WebDAV, unpack, restore (with confirmation)
- Test: verify WebDAV connection and upload capability
"""

import os
import sys
import json
import zipfile
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from urllib.parse import quote


def get_webdav_client():
    """Get WebDAV client if webdavclient3 is installed."""
    try:
        from webdav3.client import Client
        return Client
    except ImportError:
        return None


def create_backup_zip(mempalace_dir: str, output_path: str = None) -> str:
    """Create a ZIP backup of ~/.mempalace directory.
    
    Args:
        mempalace_dir: Path to mempalace data directory (default: ~/.mempalace)
        output_path: Optional output path for ZIP file
        
    Returns:
        Path to created ZIP file
    """
    mempalace_path = Path(mempalace_dir).expanduser().resolve()
    
    if not mempalace_path.exists():
        print(f"ERROR: MemPalace directory not found: {mempalace_path}")
        sys.exit(1)
    
    # Generate timestamped filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if output_path:
        zip_path = Path(output_path)
    else:
        zip_path = Path(tempfile.gettempdir()) / f"mempalace_backup_{timestamp}.zip"
    
    print(f"Creating backup ZIP: {zip_path}")
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(mempalace_path):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(mempalace_path.parent)
                zf.write(file_path, arcname)
                print(f"  Added: {arcname}")
    
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"Backup created: {zip_path} ({size_mb:.2f} MB)")
    return str(zip_path)


def upload_to_webdav(zip_path: str, config: dict) -> bool:
    """Upload backup ZIP to WebDAV server.
    
    Args:
        zip_path: Path to local ZIP file
        config: WebDAV config dict with url, username, password, path
        
    Returns:
        True if upload succeeded
    """
    Client = get_webdav_client()
    if not Client:
        print("ERROR: webdavclient3 not installed. Run: pip install webdavclient3")
        sys.exit(1)
    
    options = {
        'webdav_hostname': config['url'],
        'webdav_login': config['username'],
        'webdav_password': config['password'],
    }
    
    client = Client(options)
    
    # Ensure remote directory exists
    remote_path = config.get('path', '/mempalace')
    remote_file = f"{remote_path}/{Path(zip_path).name}"
    
    try:
        # Check if remote path exists, create if not
        if not client.check(remote_path):
            print(f"Creating remote directory: {remote_path}")
            client.mkdir(remote_path)
        
        print(f"Uploading to: {remote_file}")
        client.upload_sync(remote_path=remote_file, local_path=zip_path)
        print("Upload successful!")
        return True
    except Exception as e:
        print(f"ERROR: Upload failed: {e}")
        return False


def list_webdav_backups(config: dict) -> list:
    """List available backups on WebDAV server.
    
    Returns:
        List of backup filenames
    """
    Client = get_webdav_client()
    if not Client:
        print("ERROR: webdavclient3 not installed. Run: pip install webdavclient3")
        sys.exit(1)
    
    options = {
        'webdav_hostname': config['url'],
        'webdav_login': config['username'],
        'webdav_password': config['password'],
    }
    
    client = Client(options)
    remote_path = config.get('path', '/mempalace')
    
    try:
        if not client.check(remote_path):
            print(f"No backups found (remote path does not exist: {remote_path})")
            return []
        
        files = client.list(remote_path)
        backups = [f for f in files if f.endswith('.zip') and 'mempalace_backup' in f]
        backups.sort(reverse=True)  # Most recent first
        return backups
    except Exception as e:
        print(f"ERROR: Failed to list backups: {e}")
        return []


def download_from_webdav(backup_name: str, config: dict, output_dir: str = None) -> str:
    """Download backup ZIP from WebDAV server.
    
    Args:
        backup_name: Filename of backup on WebDAV
        config: WebDAV config dict
        output_dir: Optional local output directory
        
    Returns:
        Path to downloaded ZIP file
    """
    Client = get_webdav_client()
    if not Client:
        print("ERROR: webdavclient3 not installed. Run: pip install webdavclient3")
        sys.exit(1)
    
    options = {
        'webdav_hostname': config['url'],
        'webdav_login': config['username'],
        'webdav_password': config['password'],
    }
    
    client = Client(options)
    remote_path = config.get('path', '/mempalace')
    remote_file = f"{remote_path}/{backup_name}"
    
    if output_dir:
        local_path = Path(output_dir) / backup_name
    else:
        local_path = Path(tempfile.gettempdir()) / backup_name
    
    try:
        print(f"Downloading: {remote_file}")
        client.download_sync(remote_path=remote_file, local_path=str(local_path))
        print(f"Downloaded to: {local_path}")
        return str(local_path)
    except Exception as e:
        print(f"ERROR: Download failed: {e}")
        sys.exit(1)


def restore_from_zip(zip_path: str, target_dir: str = None, confirm: bool = True) -> bool:
    """Restore MemPalace from backup ZIP.
    
    Args:
        zip_path: Path to backup ZIP file
        target_dir: Target directory (default: ~/.mempalace)
        confirm: Ask for confirmation before restoring
        
    Returns:
        True if restore succeeded
    """
    if target_dir:
        mempalace_path = Path(target_dir).expanduser().resolve()
    else:
        mempalace_path = Path.home() / ".mempalace"
    
    if confirm:
        print(f"\n{'=' * 60}")
        print("  WARNING: Restore will OVERWRITE existing data!")
        print(f"  Target: {mempalace_path}")
        print(f"  Source: {zip_path}")
        print(f"{'=' * 60}\n")
        
        response = input("  Type 'YES' to confirm restore: ").strip()
        if response != "YES":
            print("Restore cancelled.")
            return False
    
    # Backup existing data before restore
    if mempalace_path.exists():
        backup_existing = mempalace_path.parent / f"mempalace_pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print(f"Backing up existing data to: {backup_existing}")
        shutil.move(str(mempalace_path), str(backup_existing))
    
    # Extract ZIP
    print(f"Extracting backup to: {mempalace_path}")
    with zipfile.ZipFile(zip_path, 'r') as zf:
        # ZIP contains path like mempalace/..., extract to parent
        zf.extractall(mempalace_path.parent)
    
    print(f"Restore complete: {mempalace_path}")
    return True


def test_webdav_connection(config: dict) -> bool:
    """Test WebDAV connection and upload capability.
    
    Returns:
        True if connection and test upload succeeded
    """
    Client = get_webdav_client()
    if not Client:
        print("ERROR: webdavclient3 not installed. Run: pip install webdavclient3")
        sys.exit(1)
    
    options = {
        'webdav_hostname': config['url'],
        'webdav_login': config['username'],
        'webdav_password': config['password'],
    }
    
    print(f"Testing WebDAV connection to: {config['url']}")
    
    try:
        client = Client(options)
        
        # Test basic connection
        print("  Checking connection...")
        if not client.check():
            print("  ERROR: Cannot connect to WebDAV server")
            return False
        print("  Connection OK")
        
        # Test remote path
        remote_path = config.get('path', '/mempalace')
        print(f"  Checking remote path: {remote_path}")
        
        if not client.check(remote_path):
            print(f"  Remote path does not exist, creating...")
            client.mkdir(remote_path)
            print("  Remote path created")
        else:
            print("  Remote path exists")
        
        # Test upload with small file
        test_file = Path(tempfile.gettempdir()) / "mempalace_test.txt"
        test_file.write_text("MemPalace WebDAV test - " + datetime.now().isoformat())
        test_remote = f"{remote_path}/test.txt"
        
        print(f"  Testing upload to: {test_remote}")
        client.upload_sync(remote_path=test_remote, local_path=str(test_file))
        print("  Upload OK")
        
        # Cleanup test file
        client.clean(test_remote)
        test_file.unlink()
        print("  Cleanup OK")
        
        print("\nWebDAV connection test PASSED!")
        return True
        
    except Exception as e:
        print(f"\nERROR: WebDAV test failed: {e}")
        return False


def cmd_backup(args):
    """CLI handler for backup command."""
    from .config import MempalaceConfig
    
    config = MempalaceConfig()
    mempalace_dir = Path(config.palace_path).parent  # ~/.mempalace (not just palace/)
    
    # Create local backup ZIP
    zip_path = create_backup_zip(mempalace_dir, args.output)
    
    # Upload to WebDAV if configured
    if args.webdav or config.has_webdav_config():
        if not config.has_webdav_config():
            print("ERROR: WebDAV not configured. Set in ~/.mempalace/config.json or use env vars.")
            sys.exit(1)
        
        webdav_config = {
            'url': config.webdav_url,
            'username': config.webdav_username,
            'password': config.webdav_password,
            'path': config.webdav_path,
        }
        
        upload_to_webdav(zip_path, webdav_config)
    
    # Cleanup local ZIP unless --keep
    if not args.keep:
        Path(zip_path).unlink()
        print(f"Local ZIP cleaned up: {zip_path}")
    else:
        print(f"Local ZIP kept at: {zip_path}")


def cmd_restore(args):
    """CLI handler for restore command."""
    from .config import MempalaceConfig
    
    config = MempalaceConfig()
    
    # Determine source
    if args.file:
        # Restore from local file
        zip_path = args.file
    elif args.webdav:
        # Restore from WebDAV
        if not config.has_webdav_config():
            print("ERROR: WebDAV not configured.")
            sys.exit(1)
        
        webdav_config = {
            'url': config.webdav_url,
            'username': config.webdav_username,
            'password': config.webdav_password,
            'path': config.webdav_path,
        }
        
        # List available backups
        backups = list_webdav_backups(webdav_config)
        if not backups:
            print("No backups found on WebDAV server.")
            sys.exit(1)
        
        if args.latest:
            backup_name = backups[0]  # Most recent
        else:
            print("\nAvailable backups:")
            for i, b in enumerate(backups[:10], 1):
                print(f"  [{i}] {b}")
            
            choice = input("\nSelect backup number (or 'q' to quit): ").strip()
            if choice == 'q':
                print("Restore cancelled.")
                return
            try:
                idx = int(choice) - 1
                backup_name = backups[idx]
            except (ValueError, IndexError):
                print("Invalid selection.")
                sys.exit(1)
        
        print(f"Selected backup: {backup_name}")
        zip_path = download_from_webdav(backup_name, webdav_config)
    else:
        print("ERROR: Specify --file <path> or --webdav")
        sys.exit(1)
    
    # Restore
    restore_from_zip(zip_path, confirm=not args.force)


def cmd_backup_list(args):
    """CLI handler for listing WebDAV backups."""
    from .config import MempalaceConfig
    
    config = MempalaceConfig()
    
    if not config.has_webdav_config():
        print("ERROR: WebDAV not configured.")
        sys.exit(1)
    
    webdav_config = {
        'url': config.webdav_url,
        'username': config.webdav_username,
        'password': config.webdav_password,
        'path': config.webdav_path,
    }
    
    backups = list_webdav_backups(webdav_config)
    
    if not backups:
        print("No backups found.")
        return
    
    print(f"\n{'=' * 60}")
    print(f"  Backups on WebDAV ({config.webdav_url})")
    print(f"{'=' * 60}\n")
    
    for i, b in enumerate(backups, 1):
        print(f"  [{i}] {b}")
    
    print()


def cmd_backup_test(args):
    """CLI handler for testing WebDAV connection."""
    from .config import MempalaceConfig
    
    config = MempalaceConfig()
    
    # Allow override via CLI args
    webdav_config = {
        'url': args.url or config.webdav_url,
        'username': args.username or config.webdav_username,
        'password': args.password or config.webdav_password,
        'path': args.path or config.webdav_path,
    }
    
    if not webdav_config['url']:
        print("ERROR: WebDAV URL required (--url or config)")
        sys.exit(1)
    
    test_webdav_connection(webdav_config)
