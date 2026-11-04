"""Put the Sync Forever app on a cart, and keep it the version this box ships.

One app per console: data/sync-forever.nes (`make -C nes package`) and data/sync-forever.md
(`make -C md package`), picked by the cart profile's app_ext. The configured path keeps its
folder and name and takes the cart's extension ("/Sync Forever.nes" -> "/Sync Forever.md").
The NES app ships with edio's MIT font; on install the box reads that cart's own menu ROM
(EDN8/nesos.nes) and patches the menu font in (chr.py), so the app reads like the menu
without the box ever shipping Krikzz's artwork. The Genesis app keeps its own font for now.
Whatever the cart holds at the app path is replaced when it differs.

This writes at the menu, an exception to "writes happen inside the app": adding a file
to a folder the menu shows is harmless (it appears when the menu reloads the folder), and
replacing one keeps the entry count (verified 2026-10-31, docs/test-results.md).
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from importlib import resources
from pathlib import PurePosixPath

from . import chr, ops, romsync, saves

MENU_ROM = "EDN8/nesos.nes"


def packaged(ext: str = ".nes") -> bytes:
    return (resources.files("syncforever") / "data" / f"sync-forever{ext}").read_bytes()


def cart_path(path: str, profile) -> str | None:
    """The configured app path with this cart's app extension; None if it has no app."""
    if not path or profile.app_ext is None:
        return None
    p = PurePosixPath("/" + ops.norm(path))
    return ops.norm(str(p.with_suffix(profile.app_ext)))


@dataclass(frozen=True)
class AppResult:
    path: str
    status: str  # "installed", "updated", "current"
    font: str  # "menu", "edio" (the NES app's fallback) or "app" (the app's own)


def app_image(dev) -> tuple[bytes, str]:
    """This box's app for this cart: the NES one with the cart's menu font when it has one."""
    base = packaged(dev.profile.app_ext)
    if dev.profile.app_ext != ".nes":
        return base, "app"
    menu = saves.read_file(dev, MENU_ROM)
    if menu:
        try:
            return chr.patch_rom(base, chr.menu_glyphs(menu)), "menu"
        except ValueError:
            pass
    return base, "edio"


def ensure_app(dev, path: str, *, check_menu=True) -> AppResult:
    """Install the app at path, or replace what's there if it differs. Needs the menu up;
    check_menu=False when the caller just pinged it."""
    ext = dev.profile.app_ext
    if ext is None:
        raise ValueError(f"no Sync Forever app for the {dev.profile.name} yet")
    path = romsync.guard(path)
    if not path.lower().endswith(ext):
        raise ValueError(f"the app path must end in {ext} on the {dev.profile.name} (/{path})")
    if check_menu:
        ops.require_menu(dev, "installing the Sync Forever app")
    image, font = app_image(dev)
    sha = hashlib.sha256(image).hexdigest()
    entry = dev.stat(path)
    if entry is not None and entry.is_dir:
        raise ValueError(f"/{path} is a folder")
    if entry is not None and entry.size == len(image):
        sink = ops.NullSink()
        if ops.download(dev, path, sink).sha256 == sha:
            return AppResult(path, "current", font)
    ops.upload(dev, io.BytesIO(image), path, policy=ops.WritePolicy(anywhere=True), overwrite=True, menu_check=False)
    ops.verify(dev, path, sha)
    return AppResult(path, "updated" if entry is not None else "installed", font)
