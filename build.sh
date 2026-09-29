#!/bin/sh
# Rebuild the iPad Extended Display AppImage from the source in this
# directory and install it over the copy in ~/Applications.
#
# Needs appimagetool on PATH or at ~/.local/bin/appimagetool-x86_64.AppImage.
set -eu

HERE="$(dirname "$(readlink -f "$0")")"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

APPDIR="$BUILD/AppDir"
mkdir -p "$APPDIR/usr/bin"
cp "$HERE/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp "$HERE/ipad-extended-display.desktop" "$APPDIR/"
cp "$HERE/ipad-extended-display.png" "$APPDIR/"
cp "$APPDIR/ipad-extended-display.png" "$APPDIR/.DirIcon"
cp "$HERE/ipad_extended_display.py" "$APPDIR/usr/bin/"

APPIMAGETOOL="$(command -v appimagetool || echo "$HOME/.local/bin/appimagetool-x86_64.AppImage")"
if [ ! -x "$APPIMAGETOOL" ]; then
    echo "appimagetool not found (looked on PATH and at ~/.local/bin/appimagetool-x86_64.AppImage)" >&2
    exit 1
fi

OUT="$HERE/iPad-Extended-Display-1.0.0-x86_64.AppImage"
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$OUT"

mkdir -p "$HOME/Applications"
cp "$OUT" "$HOME/Applications/iPad-Extended-Display-1.0.0-x86_64.AppImage"
chmod +x "$HOME/Applications/iPad-Extended-Display-1.0.0-x86_64.AppImage"
echo "Installed to $HOME/Applications/iPad-Extended-Display-1.0.0-x86_64.AppImage"
