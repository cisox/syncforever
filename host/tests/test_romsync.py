"""ROM sync planning and execution against the fake cart (no NES needed)."""

import pytest

from syncforever import ops, romsync
from syncforever.protocol import Cmd

ANYWHERE = ops.WritePolicy(anywhere=True)


def rom(seed, size=0x4000):
    return b"NES\x1a" + bytes((seed + i) & 0xFF for i in range(size - 4))


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "library"
    files = {
        "Zelda.nes": rom(1),
        "Contra.nes": rom(2),
        "Puzzle/Tetris.nes": rom(3),
        "Puzzle/readme.txt": b"not a ROM",
        ".hidden.nes": rom(4),
    }
    for rel, data in files.items():
        (lib / rel).parent.mkdir(parents=True, exist_ok=True)
        (lib / rel).write_bytes(data)
    return lib


@pytest.fixture
def cart(sd):
    """The fake SD card: a ROM of the user's own, a save folder, and firmware in the root."""
    (sd / "Own Game.nes").write_bytes(rom(9))
    (sd / "fw.efu").write_bytes(b"firmware")
    (sd / "EDN8" / "gamedata" / "Zelda.nes").mkdir(parents=True)
    (sd / "EDN8" / "nesos.nes").write_bytes(rom(8))
    return sd


@pytest.fixture
def manifest(tmp_path):
    return romsync.Manifest(tmp_path / "state" / "cart.json")


def plan(dev, library, manifest, mode, dest=""):
    return romsync.make_plan(dev, library, dest, mode, manifest)


def run(dev, library, manifest, mode, dest=""):
    p = plan(dev, library, manifest, mode, dest)
    romsync.run_plan(dev, p, library, dest, manifest, policy=ANYWHERE)
    return p


def test_add_copies_missing_roms_into_the_root(dev, library, cart, manifest):
    p = run(dev, library, manifest, "add")
    assert sorted(p.add) == ["Contra.nes", "Puzzle/Tetris.nes", "Zelda.nes"]
    assert p.replace == p.remove == []
    assert (cart / "Puzzle" / "Tetris.nes").read_bytes() == rom(3)
    assert not (cart / "Puzzle" / "readme.txt").exists()  # ROMs only
    assert not (cart / ".hidden.nes").exists()
    assert (cart / "Own Game.nes").exists() and (cart / "fw.efu").exists()
    assert sorted(manifest.owned()) == ["Contra.nes", "Puzzle/Tetris.nes", "Zelda.nes"]
    assert plan(dev, library, manifest, "add").empty  # second run: nothing to do


def test_add_never_replaces(dev, library, cart, manifest):
    (cart / "Zelda.nes").write_bytes(rom(77))
    p = run(dev, library, manifest, "add")
    assert "Zelda.nes" not in p.add
    assert (cart / "Zelda.nes").read_bytes() == rom(77)


def test_update_replaces_changed_roms(dev, library, cart, manifest):
    (cart / "Zelda.nes").write_bytes(rom(77))  # same size, other bytes
    (cart / "Contra.nes").write_bytes(rom(2, 0x2000))  # other size
    p = run(dev, library, manifest, "update")
    assert sorted(p.replace) == ["Contra.nes", "Zelda.nes"]
    assert (cart / "Zelda.nes").read_bytes() == rom(1)
    assert (cart / "Contra.nes").read_bytes() == rom(2)
    assert plan(dev, library, manifest, "update").empty


def test_update_is_case_insensitive_like_fat(dev, library, cart, manifest):
    (cart / "ZELDA.NES").write_bytes(rom(1))
    p = plan(dev, library, manifest, "update")
    assert "Zelda.nes" not in p.add and "Zelda.nes" not in p.replace


def test_update_reads_each_cart_file_once(dev, sim, library, cart, manifest):
    run(dev, library, manifest, "add")
    (cart / "Unrelated.nes").write_bytes(rom(5))
    sim.commands.clear()
    plan(dev, library, manifest, "update")  # everything we copied is in the manifest
    assert Cmd.F_FRD not in sim.commands
    manifest.files.clear()
    plan(dev, library, manifest, "update")  # unknown hashes: read once, then cached
    sim.commands.clear()
    plan(dev, library, manifest, "update")
    assert Cmd.F_FRD not in sim.commands


def test_mirror_removes_only_what_sync_copied(dev, library, cart, manifest):
    run(dev, library, manifest, "add")
    (library / "Contra.nes").unlink()
    p = run(dev, library, manifest, "mirror")
    assert p.remove == ["Contra.nes"]
    assert not (cart / "Contra.nes").exists()
    assert (cart / "Own Game.nes").exists()  # never synced: kept
    assert "Contra.nes" not in manifest.owned()


def test_strict_removes_every_rom_not_in_the_library(dev, library, cart, manifest):
    p = run(dev, library, manifest, "strict")
    assert p.remove == ["Own Game.nes", "roms/nes/Zelda.nes"]  # the latter from the shared sd fixture
    assert not (cart / "Own Game.nes").exists()
    assert (cart / "fw.efu").exists()  # not a ROM
    assert (cart / "EDN8" / "nesos.nes").exists()  # never inside EDN8


def test_dest_folder(dev, library, cart, manifest):
    run(dev, library, manifest, "strict", dest="syncforever-test/sync")
    assert (cart / "syncforever-test" / "sync" / "Puzzle" / "Tetris.nes").exists()
    assert (cart / "Own Game.nes").exists()  # outside dest: not part of the mirror


def test_plan_runs_against_the_write_policy(dev, library, cart, manifest):
    p = plan(dev, library, manifest, "add")
    with pytest.raises(ops.WriteNotAllowed):
        romsync.run_plan(dev, p, library, "", manifest, policy=ops.WritePolicy())


def test_guard_refuses_edn8():
    with pytest.raises(ops.WriteNotAllowed):
        romsync.guard("edn8/gamedata/x.nes")
    assert romsync.guard("EDN8x/y.nes") == "EDN8x/y.nes"


def test_manifest_persists(tmp_path):
    m = romsync.Manifest(tmp_path / "m.json")
    m.record("A/B.nes", 10, "ab", owned=True)
    again = romsync.Manifest(tmp_path / "m.json")
    assert again.owned() == ["A/B.nes"]
    assert again.get("a/b.NES")["sha"] == "ab"


def test_library_symlinks_out_of_the_library_are_skipped(library, tmp_path):
    outside = tmp_path / "secret.nes"
    outside.write_bytes(rom(6))
    (library / "Link.nes").symlink_to(outside)
    (library / "Inside.nes").symlink_to(library / "Zelda.nes")
    roms = romsync.library_roms(library)
    assert "link.nes" not in roms
    assert "inside.nes" in roms


@pytest.mark.parametrize("path", ["/EDN8/x.nes", "edn8/a/b.nes", "/MEGA/x.md", "mega/gamedata/x"])
def test_guard_refuses_every_system_folder(path):
    with pytest.raises(ops.WriteNotAllowed):
        romsync.guard(path)
    assert romsync.guard("/megadrive/x.md") == "megadrive/x.md"  # not a system folder


# --- the app's file and empty folders ------------------------------------------------------


def test_strict_never_touches_the_app(dev, library, cart, manifest):
    (cart / "Sync Forever.nes").write_bytes(rom(5))
    (library / "Sync Forever.nes").write_bytes(rom(6))  # one in the library isn't copied over it
    for mode in ("strict", "update"):
        p = romsync.make_plan(dev, library, "", mode, manifest, keep=("/Sync Forever.nes",))
        assert "Sync Forever.nes" not in p.remove + p.add + p.replace
    (cart / "Sync Forever.nes").unlink()
    p = romsync.make_plan(dev, library, "", "update", manifest, keep=("Sync Forever.nes",))
    assert "Sync Forever.nes" not in p.add  # missing from the cart: still not sync's to add


def test_strict_removes_folders_left_empty(dev, library, cart, manifest):
    (cart / "syncforever-test" / "deep").mkdir(parents=True)
    (cart / "syncforever-test" / "deep" / "Old.nes").write_bytes(rom(7))
    (cart / "syncforever-test" / "Old2.nes").write_bytes(rom(8))
    (cart / "Empty").mkdir()
    (cart / "Keep").mkdir()
    (cart / "Keep" / "notes.txt").write_bytes(b"not a ROM: the folder stays")
    (cart / "Hidden").mkdir()
    (cart / "Hidden" / ".DS_Store").write_bytes(b"x")
    (cart / "EDN8" / "emptydir").mkdir()
    p = run(dev, library, manifest, "strict")
    assert {"syncforever-test/deep", "syncforever-test", "Empty"} <= set(p.remove_dirs)
    assert p.remove_dirs.index("syncforever-test/deep") < p.remove_dirs.index("syncforever-test")
    assert p.removals == len(p.remove) + len(p.remove_dirs)
    assert not (cart / "syncforever-test").exists() and not (cart / "Empty").exists()
    assert (cart / "Keep" / "notes.txt").exists() and (cart / "Hidden" / ".DS_Store").exists()
    assert (cart / "EDN8" / "emptydir").is_dir() and (cart / "EDN8" / "gamedata" / "Zelda.nes").is_dir()
    assert (cart / "EDN8" / "nesos.nes").exists()
    assert p.summary().endswith(f"remove {p.removals} ({len(p.remove)} files, {len(p.remove_dirs)} folders)")
    assert romsync.Plan("strict", remove_dirs=["A"]).summary() == "add 0, replace 0, remove 1 (0 files, 1 folder)"


def test_an_empty_system_folder_is_never_removed(dev, library, sd, manifest):
    (sd / "EDN8").mkdir()  # nothing in it at all
    p = run(dev, library, manifest, "strict")
    assert "EDN8" not in p.remove_dirs
    assert (sd / "EDN8").is_dir()


def test_mirror_removes_only_folders_it_empties(dev, library, cart, manifest):
    (cart / "Empty").mkdir()  # the owner's: mirror leaves it
    run(dev, library, manifest, "add")
    (library / "Puzzle" / "Tetris.nes").unlink()
    p = run(dev, library, manifest, "mirror")
    assert p.remove == ["Puzzle/Tetris.nes"] and p.remove_dirs == ["Puzzle"]
    assert not (cart / "Puzzle").exists() and (cart / "Empty").is_dir()
