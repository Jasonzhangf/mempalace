#!/bin/bash
# Build mempalace into a standalone binary using PyInstaller
# Usage: ./scripts/build-binary.sh
#
# Prerequisites:
#   python3 -m venv .venv && source .venv/bin/activate
#   pip install -e ".[binary]"

set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Cleaning previous builds..."
rm -rf build dist

echo "==> Building standalone binary with PyInstaller..."
pyinstaller --onefile --name mempalace \
  --hidden-import=chromadb \
  --hidden-import=chromadb.config \
  --hidden-import=chromadb.api \
  --hidden-import=chromadb.api.models \
  --hidden-import=chromadb.api.segment \
  --hidden-import=chromadb.api.types \
  --hidden-import=chromadb.db \
  --hidden-import=chromadb.db.base \
  --hidden-import=chromadb.db.impl \
  --hidden-import=chromadb.segment \
  --hidden-import=chromadb.segment.impl \
  --hidden-import=chromadb.segment.impl.manager \
  --hidden-import=chromadb.segment.impl.metadata \
  --hidden-import=chromadb.segment.impl.vector \
  --hidden-import=chromadb.segment.impl.vector.local \
  --hidden-import=chromadb.segment.impl.vector.local.hnsw \
  --hidden-import=chromadb.utils \
  --hidden-import=chromadb.utils.embedding_functions \
  --hidden-import=mempalace.cli \
  --hidden-import=mempalace.miner \
  --hidden-import=mempalace.searcher \
  --hidden-import=mempalace.knowledge_graph \
  --hidden-import=mempalace.layers \
  --hidden-import=mempalace.dialect \
  --hidden-import=mempalace.room_detector_local \
  --hidden-import=mempalace.convo_miner \
  --hidden-import=mempalace.general_extractor \
  --hidden-import=mempalace.entity_detector \
  --hidden-import=sentence_transformers \
  --hidden-import=onnxruntime \
  --hidden-import=tokenizers \
  --hidden-import=yaml \
  --hidden-import=sqlite3 \
  --noupx \
  entry_point.py

echo ""
echo "==> Build complete!"
echo "    Binary: dist/mempalace"
echo "    Size:   $(du -h dist/mempalace | cut -f1)"
echo ""
echo "    Install system-wide:"
echo "    cp dist/mempalace /usr/local/bin/"
echo ""
echo "    Or run directly:"
echo "    ./dist/mpalace --help"
