import pytest

from syncforever import saves
from syncforever.carts import MEGA_PRO
from syncforever.cli import main
from syncforever.n8pro import EverDrive
from syncforever.protocol import UnsafeStateError
from syncforever.saves import SaveStore
from syncforever.sim import FakeN8Pro


def recent_bin(*paths):
    out = bytearray(21 * saves.SLOT)
    for i, p in enumerate(paths):
        raw = p.encode("latin-1")
        out[i * saves.SLOT : i * saves.SLOT + len(raw)] = raw
    return bytes(out)


def put(sd, game, data, name="bram.srm", system="EDN8"):
    folder = sd / system / "gamedata" / game
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)


@pytest.fixture
def cart(sd):
    (sd / "EDN8" / "sysdata").mkdir(parents=True)
    (sd / "EDN8" / "gamedata").mkdir(parents=True)
    (sd / "EDN8" / "gamedata" / "readme.txt").write_bytes(b"All game data stored here\n")
    (sd / "EDN8" / "sysdata" / "recent.bin").write_bytes(
        recent_bin("/Zelda.nes", "/roms/nes/Metroid.nes", "/Zelda.nes", "/8 Eyes (USA).nes")
    )
    put(sd, "Zelda.nes", b"\x01" * 8192)
    put(sd, "8 Eyes (USA).nes", b"\x08" * 8192)
    (sd / "EDN8" / "gamedata" / "Metroid.nes").mkdir()  # cheats or settings, no save
    return sd


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def store(tmp_path):
    return saves.SaveStore(tmp_path / "backups", clock=Clock())


def test_parse_recent():
    data = recent_bin("/A.nes", "/sub/B.nes") + bytes(saves.SLOT)
    assert saves.parse_recent(data) == ["A.nes", "sub/B.nes"]


def test_backup_recent_takes_the_newest_distinct_games(dev, cart, store):
    results = saves.backup_recent(dev, store, n=3)  # Zelda, Metroid (nothing), 8 Eyes
    assert [(r.game, r.file, r.status) for r in results] == [
        ("Zelda.nes", "bram.srm", "saved"),
        ("8 Eyes (USA).nes", "bram.srm", "saved"),
    ]
    assert results[0].path.read_bytes() == b"\x01" * 8192
    assert results[0].path.parent == store.root / "Zelda.nes" / "bram.srm"
    assert results[0].path.suffix == ".srm"


def test_unchanged_file_is_not_stored_again(dev, cart, store):
    saves.backup_recent(dev, store)
    store.clock.t += 60
    (r,) = saves.backup_recent(dev, store)
    assert r.status == "unchanged"
    assert len(store.versions("Zelda.nes", "bram.srm")) == 1


def test_changed_file_adds_a_version_and_keeps_the_old(dev, cart, store):
    saves.backup_recent(dev, store)
    put(cart, "Zelda.nes", b"\x02" * 8192)
    store.clock.t += 60
    (r,) = saves.backup_recent(dev, store)
    assert r.status == "saved"
    old, new = store.versions("Zelda.nes", "bram.srm")
    assert old.read_bytes() == b"\x01" * 8192 and new.read_bytes() == b"\x02" * 8192


def test_same_second_versions_never_overwrite(store):
    a = store.add("G.nes", "bram.srm", b"a")
    b = store.add("G.nes", "bram.srm", b"b")
    c = store.add("G.nes", "bram.srm", b"a")  # back to an older save: still a new version
    assert len({a, b, c}) == 3
    assert [p.read_bytes() for p in store.versions("G.nes", "bram.srm")] == [b"a", b"b", b"a"]


def test_files_are_versioned_separately(store):
    store.add("G.md", "bram.srm", b"battery")
    store.add("G.md", "00.SAV", b"state")
    assert store.files("G.md") == ["00.SAV", "bram.srm"]
    (state,) = store.versions("G.md", "00.SAV")
    assert state.suffix == ".SAV" and state.read_bytes() == b"state"


def test_backup_all_takes_every_file_in_each_game_folder(dev, cart, store):
    put(cart, "Zelda.nes", b"cheats", name="cheats.txt")  # any name, not just bram.srm
    results = {(r.game, r.file): r.status for r in saves.backup_all(dev, store)}
    assert results == {
        ("8 Eyes (USA).nes", "bram.srm"): "saved",
        ("Zelda.nes", "bram.srm"): "saved",
        ("Zelda.nes", "cheats.txt"): "saved",
    }


def test_backup_refuses_while_a_game_runs(sd, cart, store):
    dev = EverDrive(FakeN8Pro(sd, in_game=True))
    dev.connect()
    with pytest.raises(UnsafeStateError):
        saves.backup_recent(dev, store)


def test_no_recent_file(dev, sd, store):
    assert saves.backup_recent(dev, store) == []


def test_mega_paths_and_save_states(sd, store):
    """MEGA/sysdata/recent.dat and per-game folders with save states (hardware 2026-11-01)."""
    (sd / "MEGA" / "sysdata").mkdir(parents=True)
    (sd / "MEGA" / "sysdata" / "recent.dat").write_bytes(
        recent_bin("/megadrive/Castlevania - Bloodlines (USA).md", "/segacd/Sonic CD/Sonic CD.cue")
    )
    put(sd, "Castlevania - Bloodlines (USA).md", b"S" * 1000, name="00.SAV", system="MEGA")
    put(sd, "Castlevania - Bloodlines (USA).md", b"T" * 1000, name="03.SAV", system="MEGA")
    put(sd, "Sonic CD.cue", b"C" * 8192, name="cd-bram.brm", system="MEGA")
    dev = EverDrive(FakeN8Pro(sd, profile=MEGA_PRO))
    dev.connect()
    assert saves.recent_path(dev) == "MEGA/sysdata/recent.dat"
    results = {(r.game, r.file) for r in saves.backup_recent(dev, store, n=2)}
    assert results == {
        ("Castlevania - Bloodlines (USA).md", "00.SAV"),
        ("Castlevania - Bloodlines (USA).md", "03.SAV"),
        ("Sonic CD.cue", "cd-bram.brm"),
    }


def test_summary_counts_files():
    r = [saves.BackupResult("Z.nes", "bram.srm", "saved"), saves.BackupResult("Z.nes", "00.SAV", "unchanged")]
    assert saves.summary(r) == "checked 2 files, backed up 1: Z.nes/bram.srm"


def test_cli_backup(cart, tmp_path, capsys):
    dest = tmp_path / "b"
    assert main(["--sim", str(cart), "backup", "--store", str(dest)]) == 0
    assert "Zelda.nes/bram.srm: saved" in capsys.readouterr().out
    assert main(["--sim", str(cart), "backup", "--store", str(dest), "--all"]) == 0
    out = capsys.readouterr().out
    assert "Zelda.nes/bram.srm: unchanged" in out and "8 Eyes (USA).nes/bram.srm: saved" in out


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "..\\x", "a\0b", "C:x"])
def test_names_from_the_cart_cant_leave_the_store(store, bad):
    with pytest.raises(ValueError):
        store.add(bad, "bram.srm", b"x")
    with pytest.raises(ValueError):
        store.add("G.nes", bad, b"x")
    assert not any(store.root.parent.rglob("*.srm"))


def test_backup_skips_unsafe_names(dev, cart, store):
    assert saves.backup_files(dev, store, "..", ["bram.srm"]) == []
    assert saves.backup_files(dev, store, "Zelda.nes", ["../evil"]) == []


# -- save states in a folder of their own (the console's `states` setting) -----------------


def test_save_states_can_have_their_own_folder(tmp_path):
    saves, states = tmp_path / "saves", tmp_path / "states"
    store = SaveStore(saves, states=states)
    store.add("Sonic.md", "bram.srm", b"battery")
    store.add("Sonic.md", "00.SAV", b"slot 0")
    store.add("Zelda", "Zelda01.state", b"fxpak state")
    store.add("Zelda", "Zelda.srm", b"fxpak save")
    assert [p.name for p in (saves / "Sonic.md").iterdir()] == ["bram.srm"]
    assert [p.name for p in (states / "Sonic.md").iterdir()] == ["00.SAV"]
    assert [p.name for p in (states / "Zelda").iterdir()] == ["Zelda01.state"]
    assert [p.name for p in (saves / "Zelda").iterdir()] == ["Zelda.srm"]
    assert store.files("Sonic.md") == ["00.SAV", "bram.srm"]
    assert store.add("Sonic.md", "00.SAV", b"slot 0") is None  # compared with its own folder's copy
    assert store.add("Sonic.md", "00.SAV", b"slot 0 again").parent == states / "Sonic.md" / "00.SAV"


def test_by_default_save_states_go_with_the_saves(tmp_path):
    store = SaveStore(tmp_path)
    assert store.add("Sonic.md", "00.SAV", b"slot").parent == tmp_path / "Sonic.md" / "00.SAV"


def test_states_off_are_neither_read_nor_stored(tmp_path):
    store = SaveStore(tmp_path, states=None)
    assert not store.wants("00.SAV") and not store.wants("Zelda01.state") and store.wants("bram.srm")
    assert store.add("Sonic.md", "00.SAV", b"slot") is None
    assert not (tmp_path / "Sonic.md").exists()


def test_only_real_state_names_count():
    from syncforever.saves import is_state

    for name in ("00.SAV", "09.sav", "Zelda.state", "Secret of Mana (USA)01.STATE"):
        assert is_state(name), name
    for name in ("bram.srm", "cd-bram.brm", "Zelda.srm", "save.sav", "123.SAV", "cheats.txt", "Zelda.fla"):
        assert not is_state(name), name


def test_a_missing_folder_of_either_kind_is_reported(tmp_path):
    (tmp_path / "saves").mkdir()
    store = SaveStore(tmp_path / "saves", states=tmp_path / "gone")
    assert store.missing() == tmp_path / "gone"
    (tmp_path / "gone").mkdir()
    assert store.missing() is None
    assert SaveStore(None, states=tmp_path / "gone").root is None
