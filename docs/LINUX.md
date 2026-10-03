# SkyCraft on Linux (bridge mode)

> Same PC: Skyrim + the SKSE plugin run under Proton/Wine (still the Windows DLL),
> Minecraft runs **natively** on Linux. No Wine for the Java side.

This is experimental. The Windows path (`README.md`, `tools/package.ps1`) is unchanged
and remains the default; everything here is opt-in so it can be upstreamed.

## How it works

The protocol (`protocol/skycraft_protocol.h`, mirrored in
`fabric/src/main/java/dev/skycraft/link/Proto.java`) is byte-identical on both
platforms. Only the *transport* changes:

| | Windows | Linux bridge |
|---|---|---|
| Mapping | `Local\SkyCraft_v1` pagefile mapping (`skse/src/Link.cpp`) | file `/dev/shm/skycraft_v1`, which Wine sees as `Z:\dev\shm\skycraft_v1` |
| Minecraft open | `kernel32.OpenFileMappingW` (`SkyLink.java`) | `FileChannel.map` on the same file |
| Clock | `GetTickCount64` / QPC | `nanoTime` (same host monotonic clock Wine uses; QPC scaled to Wine's 10 MHz) |
| "Already running" guard | named mutex | lock file (`skycraft_v1.lock`) |
| Auto-start | SKSE launches Prism | manual: you start the Linux Prism yourself (`Launcher.cpp` stays silent, status `kOff`) |

D3D11 rendering (`Overlay.cpp`, `WorldRender.cpp`) stays on the Skyrim side under
Proton/DXVK. The GPU-interop fast path can't cross into a native process, so bridge
mode uses the CPU fallback compositing path.

## Requirements

- Skyrim SE Anniversary Edition via Steam/Proton, SKSE64, Address Library, and the
  SkyCraft SKSE plugin DLL (build on Windows or take it from a Windows release zip —
  MSVC has no Linux target; Proton runs it as-is). Install it like any SKSE plugin.
- A Microsoft account owning Minecraft: Java Edition, Java 25+, ~3 GB free RAM and
  ~200 MB in `/dev/shm` for the bridge file.
- The Linux Minecraft bundle from `tools/package-linux.sh`
  (`dist/SkyCraft-Minecraft-linux.zip`): portable Prism Launcher (Linux Qt6) with a
  ready `SkyCraft` instance (Minecraft 26.3, Fabric, Fabric API, e4mc, SkyCraft).

## Setup

1. Unpack `SkyCraft-Minecraft-linux.zip` to `~/.local/share/SkyCraft`, so the launcher
   is at `~/.local/share/SkyCraft/Prism/PrismLauncher`.
2. Tell Skyrim to use the bridge. In your Steam launch options (or the environment
   Proton inherits):
   ```
   SKYCRAFT_LINUX_BRIDGE=1 %command%
   ```
   This makes the plugin create the file-backed mapping instead of `Local\...`.
3. Start the `SkyCraft` Prism instance **manually** (sign in once; Prism downloads
   Minecraft/Java on first run). `bStartWithSkyrim` is ignored in bridge mode —
   Wine can't (and won't try to) start a native Linux process.
4. Start Skyrim through SKSE. The Fabric mod waits on the title screen until the
   link appears, then hides and opens its world by itself, like on Windows.

### Paths and overrides

| Setting | Default | Notes |
|---|---|---|
| `SKYCRAFT_LINUX_BRIDGE` | off | `1` = default file. Any other non-`0` value = custom path (a `/unix/abs` path is translated to `Z:\...` for Wine) |
| `SKYCRAFT_LINK_FILE` / `-Dskycraft.linkFile` | `/dev/shm/skycraft_v1` | same file the plugin mmaps; set both sides to the same value when customizing |
| `skycraft.link` | `Local\SkyCraft_v1` | Windows named mapping; ignored on Linux |

Skyrim under Proton must *see* the file: anything under `/` is reachable as `Z:\...`.
Don't point the bridge at `C:\...` (inside the prefix) — the native side can't reach it.

## Troubleshooting

- `SkyCraft.log` (in the prefix's `Documents/My Games/Skyrim Special Edition/SKSE/`)
  says `[linux bridge]` with the backing file when active. No such line = the env var
  didn't reach Skyrim; check launch options.
- `can't open ... yet: Skyrim hasn't created it yet` (Minecraft log) = start Skyrim;
  the file appears when the plugin loads.
- `... is N bytes, expected M ...` = version skew between plugin and mod; update both.
- Stuck with no link after a minute: bridge mode intentionally stays silent (no
  Windows-only mutex/process diagnosis). Check both logs and that both sides agree
  on the file path.
- `/dev/shm` full: the mapping is ~191 MB; free space or point `SKYCRAFT_LINK_FILE`
  at a tmpfs/SSD path both sides can reach.

## Developing without Skyrim

`tools/fake_skyrim.py` runs natively on Linux now (file backend, monotonic clock):

```
SKYCRAFT_LINK_FILE=/tmp/skycraft_test python3 tools/fake_skyrim.py 60 overlay.png
SKYCRAFT_LINK_FILE=/tmp/skycraft_test ./gradlew runClient   # in fabric/
```

(`tools/test_mc.sh` still targets Windows: its java-kill line calls
`powershell.exe`. On Linux, stop the dev client by closing it.)

## Upstreaming notes

All Linux support is additive and off by default: the Windows mapping, mutex,
auto-start, `package.ps1`, and `fake_skyrim.py`'s `windll` path are untouched.
Known follow-ups before a PR: shared QPC-frequency handshake instead of the
assumed 10 MHz, a `package-linux.sh` CI job, and ENB/Sodium notes for Proton.
