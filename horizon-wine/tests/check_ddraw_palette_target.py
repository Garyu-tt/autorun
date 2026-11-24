#!/usr/bin/env python3
"""Compile the actual DirectDraw predicate against a minimal host fixture."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
surface = (ROOT / "dlls/ddraw/surface.c").read_text()
header = (ROOT / "dlls/ddraw/ddraw_private.h").read_text()
predicate = re.search(r"static bool is_render_target\([^}]+\}", surface).group()
palette = re.search(r"static inline BOOL format_is_paletteindexed\([^}]+\}", header).group()
assert surface.count("is_render_target(ddraw, desc, version)") == 3
assert len(re.findall(r"\bis_render_target\(", surface)) == 4

preamble = """
#include <stdbool.h>
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
typedef uint32_t DWORD;
typedef int BOOL;
#define DDSCAPS_3DDEVICE 1u
#define DDSCAPS_PRIMARYSURFACE 2u
#define DDRAW_NO3D 4u
#define DDPF_PALETTEINDEXED1 1u
#define DDPF_PALETTEINDEXED2 2u
#define DDPF_PALETTEINDEXED4 4u
#define DDPF_PALETTEINDEXED8 8u
#define DDPF_PALETTEINDEXEDTO8 16u
typedef struct { DWORD dwFlags; } DDPIXELFORMAT;
typedef struct { struct { DWORD dwCaps; } ddsCaps; DDPIXELFORMAT ddpfPixelFormat; } DDSURFACEDESC2;
struct ddraw { DWORD flags; };
"""
cases = """
int main(void)
{
    unsigned version, caps, no3d, palette_flags, count = 0;
    for (version = 1; version <= 7; version++)
        for (caps = 0; caps < 4; caps++)
            for (no3d = 0; no3d < 2; no3d++)
                for (palette_flags = 0; palette_flags < 64; palette_flags++)
                {
                    struct ddraw dd = {no3d ? DDRAW_NO3D : 0};
                    DDSURFACEDESC2 desc = {{caps}, {palette_flags}};
                    bool expected = (caps & DDSCAPS_3DDEVICE) != 0;
                    if (version == 1 && caps == DDSCAPS_PRIMARYSURFACE &&
                            !no3d && !(palette_flags & 31u))
                        expected = true;
                    assert(is_render_target(&dd, &desc, version) == expected);
                    count++;
                }
    printf("%u predicate cases passed\\n", count);
    return 0;
}
"""
with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / "test.c"
    exe = Path(tmp) / "test"
    src.write_text(preamble + palette + "\n" + predicate + "\n" + cases)
    subprocess.run(shlex.split(os.environ.get("CC", "cc")) +
                   ["-std=c99", "-Wall", "-Wextra", "-Werror", str(src), "-o", str(exe)],
                   check=True)
    subprocess.run([str(exe)], check=True)
