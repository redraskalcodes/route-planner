#!/bin/bash
# Build Route Planner for macOS
# Run this on a Mac: bash build_mac.sh
# Output: dist/Route Planner.app  (zip it and distribute)

set -e
cd "$(dirname "$0")"

echo "==> Installing / updating dependencies..."
pip install -r requirements.txt

echo "==> Cleaning previous build..."
rm -rf build dist

echo "==> Building .app bundle..."
pyinstaller route_planner.spec

echo "==> Packaging as zip..."
cd dist
zip -r "Route Planner Mac.zip" "Route Planner.app"
cd ..

echo ""
echo "✓  Done!  Distributable: dist/Route Planner Mac.zip"
echo "   Unzip → double-click 'Route Planner.app' to launch."
