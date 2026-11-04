"""MSU games: ROM hacks that play CD-quality audio from big files beside the ROM. The box syncs
them only when the console's `msu` setting is on (config.py); off, they're left out of the sync
and left alone on the cart. Their files are hundreds of MB: romsync never reads them back, and
takes a copy already on the cart as the library's (make_plan).

  msu1    (SNES, FXPAK PRO)       Game.sfc + Game.msu and/or Game-1.pcm, Game-2.pcm, ...
  msu-md  (Genesis, Mega PRO)     Game.md + Game.cue, and the files that cue names (Game.bin):
                                  a Sega CD game's cue names its own .bin, so it isn't one.

Each game sits in a folder of its own in the library, unzipped (the box doesn't unzip).
"""

from __future__ import annotations

import re
from pathlib import Path

from .ops import fat_key

KINDS = ("msu1", "msu-md")
# File types a cart listing must include to see MSU files that aren't ROM types already.
CART_SUFFIXES = {"msu1": (".msu", ".pcm"), "msu-md": ()}
MD_ROMS = (".md", ".gen", ".smd", ".bin")
PCM = re.compile(r"-\d+\.pcm$")
CUE_FILE = re.compile(r'^\s*FILE\s+"?([^"\r\n]+?)"?\s+\w+\s*$', re.IGNORECASE | re.MULTILINE)


def games(kind: str, library: Path, files: dict[str, str], rom_suffixes) -> dict[str, list[str]]:
    """{ROM key: the library-relative paths of its MSU files} for every MSU game in the
    library. files: {key: library-relative path} of every file (romsync.library_roms)."""
    out: dict[str, list[str]] = {}
    for k, rel in files.items():
        if not k.endswith(tuple(rom_suffixes)):
            continue
        stem = k[: -len(Path(k).suffix)]
        if kind == "msu1":
            found = [
                files[c] for c in files if c == f"{stem}.msu" or (c.startswith(stem) and PCM.fullmatch(c[len(stem) :]))
            ]
        elif kind == "msu-md":
            found = _msu_md(library, k, rel, stem, files)
        else:
            raise ValueError(f"unknown MSU kind {kind!r}")
        if found:
            out[k] = sorted(found)
    return out


def _msu_md(library: Path, k: str, rel: str, stem: str, files: dict[str, str]) -> list[str]:
    if not k.endswith(MD_ROMS) or f"{stem}.cue" not in files:
        return []
    cue = files[f"{stem}.cue"]
    try:
        text = (library / cue).read_text(errors="replace")
    except OSError:
        return []
    folder = Path(rel).parent
    named = [(folder / m.group(1).strip()).as_posix() for m in CUE_FILE.finditer(text)]
    if any(fat_key(n) == k for n in named):
        return []  # the cue plays this file: a Sega CD disc, not an MSU-MD ROM
    return [cue, *(files[fat_key(n)] for n in named if fat_key(n) in files)]
