#!/usr/bin/env bash
# Builds the Linux half of SkyCraft and packs the native Minecraft side into dist/:
#   SkyCraft-Minecraft-linux.zip   portable Prism Launcher (Linux Qt6) with a ready "SkyCraft"
#                                  instance (Minecraft 26.3, Fabric, Fabric API, e4mc, SkyCraft).
#                                  Unpack to ~/.local/share/SkyCraft and start it yourself;
#                                  the SKSE plugin (still the Windows DLL, under Proton) connects
#                                  over the Linux bridge file (see docs/LINUX.md).
#   skycraft-fabric-<version>.jar  the Minecraft mod on its own (same file as the Windows pack).
#
# The SKSE plugin DLL is still built on Windows (package.ps1) or taken from a Windows release
# zip: MSVC has no Linux target and Proton runs the DLL as-is. If a DLL is present at
# skse/build/RelWithDebInfo/SkyCraft.dll it is also copied to dist/ for convenience.
#
#   tools/package-linux.sh [--no-build]
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(grep -E '^version=' "$ROOT/fabric/gradle.properties" | cut -d= -f2 | tr -d '[:space:]')

# Pinned downloads (same mods as tools/package.ps1; Prism is the Linux Qt6 portable build).
PRISM_VERSION="11.1.1"
PRISM_TGZ="PrismLauncher-Linux-Qt6-Portable-$PRISM_VERSION.tar.gz"
PRISM_URL="https://github.com/PrismLauncher/PrismLauncher/releases/download/$PRISM_VERSION/$PRISM_TGZ"
PRISM_SHA256="e21949a8101c1803dbc6f67a2cdad48ba80ede1c133f0d1f2a89da07af09e823"
PRISM_LICENSE_URL="https://raw.githubusercontent.com/PrismLauncher/PrismLauncher/$PRISM_VERSION/LICENSE"
FABRIC_API_JAR="fabric-api-0.161.0+26.3.jar"
FABRIC_API_URL="https://cdn.modrinth.com/data/P7dR8mSH/versions/bNnaTiuM/fabric-api-0.161.0%2B26.3.jar"
FABRIC_API_SHA512="ed6b2586d6fde11fde8472f5a527c51e99b67026e46f94d4bfd85e7e28ce5ee299173ee16ad576ceb51f39f98d30a811086a6deb1a86a524859cc16e12da109d"
E4MC_JAR="e4mc-fabric-6.2.2-modern.jar"
E4MC_URL="https://cdn.modrinth.com/data/qANg5Jrr/versions/AouleFRY/e4mc-fabric-6.2.2-modern.jar"
E4MC_SHA512="01ef0a8c5b76e2cb0effd337bad3350d8807d100d0ec661e01b2ffb20af7b652f756c5eaa11bee233c37905bfd7b573f7a85f3d15bfd2833962c76f02cd59a86"

CACHE="$ROOT/.tools/prism-linux"

get_pinned() { # url path algorithm hash
    local url=$1 path=$2 algo=$3 hash=$4
    mkdir -p "$(dirname "$path")"
    if [ ! -f "$path" ]; then
        curl -sSL -o "$path" "$url"
    fi
    if [ -n "${hash:-}" ]; then
        local got
        case "$algo" in
            SHA256) got=$(sha256sum "$path" | cut -d' ' -f1) ;;
            SHA512) got=$(sha512sum "$path" | cut -d' ' -f1) ;;
            *) echo "unknown hash algorithm $algo" >&2; exit 1 ;;
        esac
        if [ "${got,,}" != "${hash,,}" ]; then
            rm -f "$path"
            echo "$path doesn't match its pinned $algo hash" >&2
            exit 1
        fi
    fi
}

if [ "${1:-}" != "--no-build" ]; then
    (cd "$ROOT/fabric" && ./gradlew build --no-configuration-cache)
fi

JAR="$ROOT/fabric/build/libs/skycraft-$VERSION.jar"
if [ ! -f "$JAR" ]; then
    echo "missing $JAR (build first, or pass --no-build with one present)" >&2
    exit 1
fi
get_pinned "$PRISM_URL" "$CACHE/$PRISM_TGZ" SHA256 "$PRISM_SHA256"
get_pinned "$FABRIC_API_URL" "$CACHE/$FABRIC_API_JAR" SHA512 "$FABRIC_API_SHA512"
get_pinned "$E4MC_URL" "$CACHE/$E4MC_JAR" SHA512 "$E4MC_SHA512"
if [ ! -f "$CACHE/PrismLauncher-$PRISM_VERSION-LICENSE.txt" ]; then
    curl -sSL -o "$CACHE/PrismLauncher-$PRISM_VERSION-LICENSE.txt" "$PRISM_LICENSE_URL"
fi

DIST="$ROOT/dist"
BUNDLE="$DIST/bundle-linux"
rm -rf "$DIST" "$BUNDLE"
mkdir -p "$BUNDLE" "$DIST"

# The bundled Minecraft: portable Prism (Linux) + the SkyCraft instance + its mods.
# The tarball IS the portable launcher root (PrismLauncher start script, portable.txt):
# extract it as-is so bundle/Prism mirrors the Windows bundle layout (instances/ next
# to the launcher).
cp -r "$ROOT/tools/minecraft-bundle/." "$BUNDLE/"
tar xzf "$CACHE/$PRISM_TGZ" -C "$BUNDLE/Prism"
cp "$CACHE/PrismLauncher-$PRISM_VERSION-LICENSE.txt" "$BUNDLE/Prism/LICENSE-PrismLauncher.txt"
sed -i "s/{PRISM_VERSION}/$PRISM_VERSION/g; s/unmodified portable Windows build/unmodified portable Linux (Qt6) build/" \
    "$BUNDLE/Prism/THIRD-PARTY.txt"
MODS="$BUNDLE/Prism/instances/SkyCraft/.minecraft/mods"
mkdir -p "$MODS"
cp "$CACHE/$FABRIC_API_JAR" "$CACHE/$E4MC_JAR" "$MODS/"
cp "$JAR" "$MODS/skycraft-$VERSION.jar"
printf 'SkyCraft %s, Prism Launcher %s (Linux Qt6 portable), %s, %s' \
    "$VERSION" "$PRISM_VERSION" "$FABRIC_API_JAR" "$E4MC_JAR" > "$BUNDLE/bundle-version.txt"

# Zip with forward slashes, sorted (mod managers and the plugin expect this).
python3 - "$BUNDLE" "$DIST/SkyCraft-Minecraft-linux.zip" <<'EOF'
import os, sys, zipfile
root, out = sys.argv[1], sys.argv[2]
names = []
for dirpath, _, files in os.walk(root):
    for f in files:
        full = os.path.join(dirpath, f)
        names.append(os.path.relpath(full, root).replace(os.sep, '/'))
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for n in sorted(names):
        z.write(os.path.join(root, *n.split('/')), n)
EOF
rm -rf "$BUNDLE"

cp "$JAR" "$DIST/skycraft-fabric-$VERSION.jar"
# The Windows DLL, if one was provided (e.g. copied from a Windows release zip).
if [ -f "$ROOT/skse/build/RelWithDebInfo/SkyCraft.dll" ]; then
    cp "$ROOT/skse/build/RelWithDebInfo/SkyCraft.dll" "$DIST/"
fi

ls -l "$DIST"
