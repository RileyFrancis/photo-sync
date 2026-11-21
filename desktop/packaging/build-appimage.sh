#!/bin/sh
# Package the PyInstaller build (dist/PhotoSync) as dist/PhotoSync-x86_64.AppImage.
# Run from desktop/ after:  pyinstaller packaging/photosync.spec
set -eu
APPIMAGETOOL_VERSION=1.9.0
TOOLS=build/tools
APPDIR=build/AppDir

mkdir -p "$TOOLS"
if [ ! -x "$TOOLS/appimagetool" ]; then
    curl -fsSL -o "$TOOLS/appimagetool" \
        "https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_VERSION/appimagetool-x86_64.AppImage"
    chmod +x "$TOOLS/appimagetool"
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib"
cp -a dist/PhotoSync "$APPDIR/usr/lib/PhotoSync"
cp packaging/photosync.desktop "$APPDIR/photosync.desktop"
cp photosync_app/icon.png "$APPDIR/photosync.png"
cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
HERE=$(dirname "$(readlink -f "$0")")
exec "$HERE/usr/lib/PhotoSync/PhotoSync" "$@"
RUN
chmod +x "$APPDIR/AppRun"

# --appimage-extract-and-run: works without FUSE (e.g. in CI containers).
ARCH=x86_64 "$TOOLS/appimagetool" --appimage-extract-and-run --no-appstream "$APPDIR" dist/PhotoSync-x86_64.AppImage
echo "Built dist/PhotoSync-x86_64.AppImage"
