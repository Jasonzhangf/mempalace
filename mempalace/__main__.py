"""Allow running as: python -m mempalace"""

try:
    from .cli import main
except ImportError:
    # When running as pyinstaller binary
    from mempalace.cli import main

main()
