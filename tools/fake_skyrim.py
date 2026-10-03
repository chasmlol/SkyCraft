"""Stand-in for the SKSE plugin, for testing the Minecraft mod without Skyrim.

Creates the shared mapping, streams a flat floor + a staircase of collision, holds W / Space for a
bit, prints Minecraft's reported player state, and saves the overlay frame to a PNG.

    python tools/fake_skyrim.py [seconds] [out.png]
"""

import mmap
import os
import struct
import sys
import time
import zlib

MAGIC = 0x43594B53
VERSION = 11
NAME = os.environ.get("SKYCRAFT_LINK", "Local\\SkyCraft_v1")  # fake_guest.py runs one beside a real Skyrim
# Linux bridge: same layout in a file both the native Minecraft and Skyrim-under-Proton can mmap.
# Overridden with SKYCRAFT_LINK_FILE=<path>. Windows builds ignore this (named mapping above).
LINK_FILE = os.environ.get("SKYCRAFT_LINK_FILE", "/dev/shm/skycraft_v1")
IS_WINDOWS = os.name == "nt"
OFF_SKY = 0x100
OFF_MC = 0x200
OFF_OVL = 0x300
OFF_OVL_HDR = 0x340
OFF_IN = 0x1000
OFF_ACTORS = 0x12000
OFF_EVENTS = 0x17000
OFF_ENTITIES = 0x1C000
OFF_COL = 0x20000
COL_BYTES = 32 << 20
OFF_PIX = OFF_COL + COL_BYTES
SLOT = 3840 * 2160 * 4
OFF_RENDER = OFF_PIX + SLOT * 3
RENDER_BYTES = 64 << 20
SIZE = OFF_RENDER + RENDER_BYTES
COL_DATA = COL_BYTES - 0x80

W, H = 1280, 720
FLOOR_Y = 100  # blocks
X0 = 100000    # test area origin (blocks); must be a multiple of 8


def tick():
    if not IS_WINDOWS:
        # GetTickCount64() is millis since boot (CLOCK_MONOTONIC); same clock Wine uses.
        return int(time.clock_gettime(time.CLOCK_MONOTONIC) * 1000)
    import ctypes

    return ctypes.windll.kernel32.GetTickCount64()


class Link:
    def __init__(self):
        if not IS_WINDOWS:
            self._open_file_mapping()
            return
        import ctypes

        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_uint64
        self.m = mmap.mmap(-1, SIZE, tagname=NAME)
        self.m[0:0x100] = bytes(0x100)
        self.m[OFF_SKY:OFF_SKY + 0x40] = bytes(0x40)
        self.m[OFF_OVL:OFF_OVL + 0x100] = bytes(0x100)
        self.m[OFF_IN:OFF_IN + 0x80] = bytes(0x80)
        self.m[OFF_COL:OFF_COL + 0x80] = bytes(0x80)
        self.m[OFF_ACTORS:OFF_ACTORS + 0x40] = bytes(0x40)
        self.m[OFF_EVENTS:OFF_EVENTS + 0x80] = bytes(0x80)
        self.m[OFF_ENTITIES:OFF_ENTITIES + 0x40] = bytes(0x40)
        self.m[OFF_RENDER:OFF_RENDER + 0x80] = bytes(0x80)
        struct.pack_into("<IIII", self.m, 0, MAGIC, VERSION, os.getpid(), 0)
        self.front = 2
        self.sky_seq = 0
        self.col_head = 0
        self.actor_seq = 0
        self.events = []
        self.sections = 0
        self.atlas = None

    def _open_file_mapping(self):
        # Linux bridge backend: byte-identical layout in a file. Windows named mapping untouched.
        parent = os.path.dirname(os.path.abspath(LINK_FILE))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._file = open(LINK_FILE, "w+b")
        self._file.truncate(SIZE)
        self.m = mmap.mmap(self._file.fileno(), SIZE, access=mmap.ACCESS_WRITE)
        self.m[0:0x100] = bytes(0x100)
        self.m[OFF_SKY:OFF_SKY + 0x40] = bytes(0x40)
        self.m[OFF_OVL:OFF_OVL + 0x100] = bytes(0x100)
        self.m[OFF_IN:OFF_IN + 0x80] = bytes(0x80)
        self.m[OFF_COL:OFF_COL + 0x80] = bytes(0x80)
        self.m[OFF_ACTORS:OFF_ACTORS + 0x40] = bytes(0x40)
        self.m[OFF_EVENTS:OFF_EVENTS + 0x80] = bytes(0x80)
        self.m[OFF_ENTITIES:OFF_ENTITIES + 0x40] = bytes(0x40)
        self.m[OFF_RENDER:OFF_RENDER + 0x80] = bytes(0x80)
        struct.pack_into("<IIII", self.m, 0, MAGIC, VERSION, os.getpid(), 0)
        self.front = 2
        self.sky_seq = 0
        self.col_head = 0
        self.actor_seq = 0
        self.events = []
        self.sections = 0
        self.atlas = None
        print(f"fake Skyrim (linux file mapping): {LINK_FILE} ({SIZE >> 20} MB)")

    def heartbeat(self):
        struct.pack_into("<Q", self.m, 0x10, tick())
        self.drain_events()
        self.drain_render()

    def drain_events(self):
        head, tail = struct.unpack_from("<Q", self.m, OFF_EVENTS)[0], struct.unpack_from("<Q", self.m, OFF_EVENTS + 0x40)[0]
        while tail < head:
            typ, form, a, b, c, d, flags = struct.unpack_from("<IIffffI", self.m, OFF_EVENTS + 0x80 + (tail % 512) * 32)
            print(f"  event from Minecraft: type={typ} form={form:08X} damage={a:.2f} push=({b:.2f},{c:.2f})x{d:.2f} flags={flags:#x}")
            self.events.append(typ)
            tail += 1
        struct.pack_into("<Q", self.m, OFF_EVENTS + 0x40, tail)

    def drain_render(self):
        base = OFF_RENDER
        size = RENDER_BYTES - 0x80
        head, tail = struct.unpack_from("<Q", self.m, base)[0], struct.unpack_from("<Q", self.m, base + 0x40)[0]
        while tail < head:
            pos = tail % size
            typ, n = struct.unpack_from("<II", self.m, base + 0x80 + pos)
            if typ == 0:
                tail += size - pos
                continue
            if typ == 1:
                w, h = struct.unpack_from("<II", self.m, base + 0x88 + pos)
                print(f"  render: atlas {w}x{h}")
                start = base + 0x90 + pos
                self.atlas = (w, h, bytes(self.m[start:start + w * h * 4]))
            elif typ == 2:
                sx, sy, sz, verts = struct.unpack_from("<iiiI", self.m, base + 0x88 + pos)
                self.sections += 1
                if self.sections <= 5:
                    print(f"  render: section ({sx},{sy},{sz}) {verts} vertices")
                    vbase = base + 0x98 + pos
                    for k in range(min(verts, 6)):
                        x, y, z, u, v, r, g, b, a, light, flags = struct.unpack_from("<5f4BII", self.m, vbase + k * 32)
                        texel = ""
                        if self.atlas:
                            aw, ah, px = self.atlas
                            tx, ty = min(int(u * aw), aw - 1), min(int(v * ah), ah - 1)
                            o = (ty * aw + tx) * 4
                            texel = f" atlas texel ({tx},{ty}) = rgba{tuple(px[o:o + 4])}"
                        print(f"    v{k}: pos ({x:.2f},{y:.2f},{z:.2f}) uv ({u:.4f},{v:.4f}) rgba ({r},{g},{b},{a}) light {light:#x} flags {flags}{texel}")
            elif typ == 3:
                print("  render: clear all")
            tail += (8 + n + 7) & ~7
        struct.pack_into("<Q", self.m, base + 0x40, tail)

    def write_actor(self, form, x, y, z):
        struct.pack_into("<I", self.m, OFF_ACTORS, self.actor_seq * 2 + 1)
        rec = struct.pack("<IIfffffffHH24s", form, 1, x, y, z, 90.0, 0.6, 1.8, 1.0, 10, 0, b"Test Bandit")
        struct.pack_into("<I", self.m, OFF_ACTORS + 4, 1)
        self.m[OFF_ACTORS + 0x40:OFF_ACTORS + 0x40 + 64] = rec
        self.actor_seq += 1
        struct.pack_into("<I", self.m, OFF_ACTORS, self.actor_seq * 2)

    def mc_alive(self):
        (beat,) = struct.unpack_from("<Q", self.m, 0x18)
        return beat != 0 and tick() - beat < 3000

    def write_sky(self, flags, pos, yaw, pitch, teleport_seq, epoch):
        self.sky_seq += 1
        struct.pack_into("<I", self.m, OFF_SKY, self.sky_seq * 2 - 1)
        struct.pack_into("<IIIdddffIIIf", self.m, OFF_SKY + 4, flags, 0x3C, epoch, pos[0], pos[1], pos[2],
                         yaw, pitch, teleport_seq, W, H, 12.0)
        struct.pack_into("<I", self.m, OFF_SKY, self.sky_seq * 2)

    def read_mc(self):
        raw = self.m[OFF_MC:OFF_MC + 0x68]
        seq, flags, x, y, z, yaw, pitch, eye, sens, ack, gui, frame, fov, bph, bam, _, ex, ey, ez = struct.unpack("<IIdddffffIIQfffIddd", raw)
        return dict(flags=flags, pos=(x, y, z), yaw=yaw, pitch=pitch, eye=eye, ack=ack, frame=frame)

    def push_input(self, typ, code, a=0, b=0, c=0):
        a, b, c = (v - (1 << 32) if v >= 1 << 31 else v for v in (a, b, c))
        head, = struct.unpack_from("<Q", self.m, OFF_IN)
        struct.pack_into("<HHiii", self.m, OFF_IN + 0x80 + (head % 4096) * 16, typ, code, a, b, c)
        struct.pack_into("<Q", self.m, OFF_IN, head + 1)

    def write_col(self, typ, payload):
        msg = 8 + len(payload)
        msg = (msg + 7) & ~7
        pos = self.col_head % COL_DATA
        if pos + msg > COL_DATA:
            struct.pack_into("<II", self.m, OFF_COL + 0x80 + pos, 0, 0)
            self.col_head += COL_DATA - pos
            pos = 0
        base = OFF_COL + 0x80 + pos
        struct.pack_into("<II", self.m, base, typ, len(payload))
        self.m[base + 8:base + 8 + len(payload)] = payload
        self.col_head += msg
        struct.pack_into("<Q", self.m, OFF_COL, self.col_head)

    def acquire_overlay(self):
        (state,) = struct.unpack_from("<I", self.m, OFF_OVL)
        if not state & 4:
            return None
        struct.pack_into("<I", self.m, OFF_OVL, self.front)
        self.front = state & 3
        w, h, flags = struct.unpack_from("<III", self.m, OFF_OVL_HDR + self.front * 0x40)
        start = OFF_PIX + self.front * SLOT
        return w, h, flags, bytes(self.m[start:start + w * h * 4])


def region_payload(rx, ry, rz, epoch, block_fn):
    blocks = []
    for by in range(8):
        for bz in range(8):
            for bx in range(8):
                x, y, z = rx * 8 + bx, ry * 8 + by, rz * 8 + bz
                bits = block_fn(x, y, z)
                if bits and any(bits):
                    blocks.append(struct.pack("<iiiI8Q", x, y, z, 0, *bits))
    head = struct.pack("<iiiiiiII", rx * 8, ry * 8, rz * 8, rx * 8 + 7, ry * 8 + 7, rz * 8 + 7, epoch, len(blocks))
    return head + b"".join(blocks)


def floor_tris_payload(rx, ry, rz, epoch):
    """Exact triangles for the flat floor top (y = FLOOR_Y) inside one region (the player collides with these)."""
    y = float(FLOOR_Y)
    tris = []
    if ry * 8 <= FLOOR_Y - 1 <= ry * 8 + 7:
        x0, z0, x1, z1 = rx * 8.0, rz * 8.0, rx * 8.0 + 8.0, rz * 8.0 + 8.0
        tris.append(struct.pack("<9fI", x0, y, z0, x1, y, z0, x1, y, z1, 0))
        tris.append(struct.pack("<9fI", x0, y, z0, x1, y, z1, x0, y, z1, 0))
    head = struct.pack("<iiiiiiII", rx * 8, ry * 8, rz * 8, rx * 8 + 7, ry * 8 + 7, rz * 8 + 7, epoch, len(tris))
    return head + b"".join(tris)


FULL = [0xFFFFFFFFFFFFFFFF] * 8


def world(x, y, z):
    """A flat floor at FLOOR_Y-1, plus a staircase of half-block steps towards +X, and a 1/8 slope towards -X."""
    x -= X0
    if y == FLOOR_Y - 1:
        return FULL
    if 3 <= x < 11 and -2 <= z <= 2 and y == FLOOR_Y + (x - 3) // 2:
        return FULL if (x - 3) % 2 == 1 else [0xFFFFFFFFFFFFFFFF] * 4 + [0] * 4
    if -12 <= x <= -4 and -2 <= z <= 2:
        # gentle 1/8-step ramp going up towards -X: height rises one sub-voxel per sub-voxel... per block: 1 voxel/8 voxels
        rise = (-4 - x)  # sub-voxels of height at this block
        base = FLOOR_Y + rise // 8
        if y == base:
            layers = rise % 8 + 1
            return [0xFFFFFFFFFFFFFFFF] * layers + [0] * (8 - layers)
    return None


def save_png(path, w, h, rgba, bottom_up):
    rows = [rgba[r * w * 4:(r + 1) * w * 4] for r in range(h)]
    if bottom_up:
        rows.reverse()
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def check_arrow_texture(link):
    # An arrow's two atlas rects (side view, back plate) should cover the arrow entity texture.
    count, _ = struct.unpack_from("<II", link.m, OFF_ENTITIES + 4)
    for i in range(min(count, 16)):
        base = OFF_ENTITIES + 0x40 + i * 96
        (kind,) = struct.unpack_from("<I", link.m, base)
        if kind != 1 or not link.atlas:
            continue
        aw, ah, px = link.atlas
        for name, k in (("side", 0), ("back", 1)):
            u0, v0, u1, v1 = struct.unpack_from("<4f", link.m, base + 44 + k * 16)
            x0, y0, x1, y1 = round(u0 * aw), round(v0 * ah), round(u1 * aw), round(v1 * ah)
            rows = []
            for y in range(y0, y1):
                rows.append("".join("#" if px[(y * aw + x) * 4 + 3] > 127 else "." for x in range(x0, x1)))
            print(f"  arrow {name} rect ({x0},{y0})-({x1},{y1}):")
            for r in rows:
                print("    " + r)
        return


def read_world_entities(link):
    count, has_sel = struct.unpack_from("<II", link.m, OFF_ENTITIES + 4)
    kinds = []
    for i in range(min(count, 16)):
        kind, eid, x, y, z = struct.unpack_from("<IIfff", link.m, OFF_ENTITIES + 0x40 + i * 96)
        kinds.append((kind, round(x, 2), round(y, 2), round(z, 2)))
    sel = struct.unpack_from("<6f", link.m, OFF_ENTITIES + 0x0C) if has_sel else None
    return kinds, sel


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 90
    out = sys.argv[2] if len(sys.argv) > 2 else "overlay.png"
    link = Link()
    print("fake Skyrim up; waiting for Minecraft...")
    epoch = 1
    sent = False
    start = time.time()
    # Far from anywhere a real Skyrim worldspace maps to, so test blocks never show up in the game.
    spawn = (X0 + 0.5, FLOOR_Y, 0.5)
    yaw = -90.0  # facing +X
    pitch = 10.0
    script_t0 = None
    last_print = 0
    saved = False
    checked = False
    tseq = int(time.time()) % 100000 + 2  # new teleport every run
    while time.time() - start < seconds:
        link.heartbeat()
        link.write_sky(1, spawn, yaw, pitch, tseq, epoch)
        if link.mc_alive() and not sent:
            link.write_col(1, struct.pack("<I", epoch))
            for rx in range(X0 // 8 - 3, X0 // 8 + 3):
                for rz in range(-3, 3):
                    for ry in range(11, 15):
                        link.write_col(3, floor_tris_payload(rx, ry, rz, epoch))
                        link.write_col(2, region_payload(rx, ry, rz, epoch, world))
            sent = True
            print("collision sent")
        mc = link.read_mc()
        in_world = mc["flags"] & 1 and mc["ack"] == tseq and sent and abs(mc["pos"][1] - FLOOR_Y) < 0.05 and abs(mc["pos"][0] - spawn[0]) < 1
        if in_world and script_t0 is None:
            script_t0 = time.time()
            print("MC in world at", mc["pos"])
        if script_t0 is not None:
            t = time.time() - script_t0
            # A fake NPC two blocks ahead: punch it, then have it hit back.
            link.write_actor(0xFF00ABCD, X0 + 2.5, FLOOR_Y, 0.5)
            for click in (0.8, 1.5):  # the first click only grabs the mouse, like focusing a window
                if click <= t < click + 0.05:
                    link.push_input(2, 1, 1)  # left mouse down
                if click + 0.1 <= t < click + 0.15:
                    link.push_input(2, 1, 0)
            if 2.5 <= t < 2.55:
                link.push_input(7, 0, 2000, 0xFF00ABCD, 0)  # 20 Skyrim damage, melee, from the NPC
            # Build: cobblestone (hotbar 8) onto the Skyrim floor, looking down and to the side.
            if 3.0 <= t < 3.05:
                yaw, pitch = 0.0, 55.0  # facing +Z, away from the NPC
                link.push_input(1, 37, 1)  # '8'
            if 3.1 <= t < 3.15:
                link.push_input(1, 37, 0)
            if 3.6 <= t < 3.65:
                link.push_input(2, 3, 1)  # right mouse: place
            if 3.7 <= t < 3.75:
                link.push_input(2, 3, 0)
            # Bow (hotbar 3): draw and loose an arrow into the floor further out.
            if 4.2 <= t < 4.25:
                yaw, pitch = 180.0, 35.0  # facing -Z: open Skyrim floor, no Minecraft blocks
                link.push_input(1, 32, 1)  # '3'
            if 4.3 <= t < 4.35:
                link.push_input(1, 32, 0)
            if 4.6 <= t < 4.65:
                link.push_input(2, 3, 1)
            if 5.8 <= t < 5.85:
                link.push_input(2, 3, 0)
            if 8.0 <= t and checked is False:
                checked = 1
                kinds, sel = read_world_entities(link)
                print(f"world entities at t=8: {kinds} (kind 1 = arrow), block outline {sel}")
            if 12.0 <= t and checked == 1:
                checked = 2
                kinds, sel = read_world_entities(link)
                print(f"world entities at t=12: {kinds} (a stuck arrow hasn't moved)")
                check_arrow_texture(link)
            # Pickup: drop one cobblestone (Q) ahead, walk over it, see whether it's gone.
            if 12.5 <= t < 12.55:
                yaw, pitch = 90.0, 10.0  # facing -X
                link.push_input(1, 37, 1)  # '8'
            if 12.6 <= t < 12.65:
                link.push_input(1, 37, 0)
            if 13.0 <= t < 13.05:
                link.push_input(1, 20, 1)  # Q (drop)
            if 13.1 <= t < 13.15:
                link.push_input(1, 20, 0)
            if 15.0 <= t and checked == 2:
                checked = 3
                kinds, sel = read_world_entities(link)
                print(f"world entities after dropping: {kinds} (kind 2 = dropped item)")
                link.push_input(1, 26, 1)  # W: walk over it
            if 16.5 <= t and checked == 3:
                checked = 4
                link.push_input(1, 26, 0)
            if 18.0 <= t and checked == 4:
                checked = 5
                kinds, sel = read_world_entities(link)
                print(f"world entities after walking over it: {kinds}")
            if 10 <= t and not saved:
                frame = link.acquire_overlay()
                if frame:
                    w, h, flags, px = frame
                    save_png(out, w, h, px, flags & 1)
                    nonzero = sum(1 for i in range(3, len(px), 4 * 97) if px[i])
                    print(f"saved overlay {w}x{h} -> {out} (sampled non-transparent pixels: {nonzero})")
                    saved = True
        if time.time() - last_print > 1.0:
            last_print = time.time()
            print(f"t={time.time()-start:5.1f} mc flags={mc['flags']:#x} ack={mc['ack']} pos=({mc['pos'][0]:.3f}, {mc['pos'][1]:.3f}, {mc['pos'][2]:.3f}) yaw={mc['yaw']:.1f} frame={mc['frame']}")
        time.sleep(1 / 60)
    print(f"summary: {link.sections} section meshes, events {link.events}")
    if link.atlas and len(sys.argv) > 3:
        w, h, px = link.atlas
        save_png(sys.argv[3], w, h, px, False)
        print(f"atlas saved to {sys.argv[3]}")


if __name__ == "__main__":
    main()
