from pathlib import Path

import pytest

from syncforever import apps, chr, config
from syncforever.protocol import EverDriveError, UnsafeStateError

# --- config ----------------------------------------------------------------------------


def test_defaults_without_a_file(monkeypatch, tmp_path):
    monkeypatch.delenv("SFE_CONFIG", raising=False)
    monkeypatch.setattr(config, "SEARCH", (tmp_path / "missing.toml",))
    cfg = config.load()
    assert cfg == config.Config()
    assert (cfg.dest, cfg.app_path, cfg.sync_mode, cfg.set_clock) == ("/", "/Sync Forever.nes", "add", True)
    assert cfg.library is None and cfg.saves is None


def test_example_config_gives_each_console_its_own_folders():
    example = Path(__file__).resolve().parents[2] / "deploy" / "config.example.toml"
    cfg = config.parse(example.read_text(), example)
    assert cfg.library is None and cfg.saves is None and cfg.port is None
    for key in ("nes", "genesis"):
        c = cfg.console(key)
        assert (c.library, c.saves) == (Path(f"/mnt/games/roms/{key}"), Path(f"/mnt/games/saves/{key}"))
        assert (c.dest, c.sync_mode) == ("/", "add")


def test_parse_reads_every_setting(tmp_path):
    cfg = config.parse(
        """
        [library]
        path = "~/roms"
        [saves]
        path = "/srv/saves"
        [cart]
        dest = "/syncforever-test/sync"
        app_path = "/Tools/Sync Forever.nes"
        sync_mode = "mirror"
        set_clock = false
        [box]
        state = "/var/lib/sfe"
        port = "/dev/ttyACM0"
        """
    )
    assert cfg.library == Path("~/roms").expanduser()
    assert cfg.saves == Path("/srv/saves")
    assert cfg.dest == "/syncforever-test/sync"
    assert cfg.app_path == "/Tools/Sync Forever.nes"
    assert cfg.sync_mode == "mirror"
    assert cfg.set_clock is False
    assert cfg.state == Path("/var/lib/sfe")
    assert cfg.port == "/dev/ttyACM0"


@pytest.mark.parametrize(
    "text, message",
    [
        ("[libary]\npath = 'x'", "unknown section [libary]"),
        ("[cart]\ndestination = '/'", "unknown setting cart.destination"),
        ("[cart]\nset_clock = 'yes'", "cart.set_clock must be bool"),
        ("[cart]\nsync_mode = 'copy'", "sync mode must be one of"),
        ("[library]\npath = 3", "library.path must be a path string"),
        ("[cart\n", "config"),
    ],
)
def test_parse_rejects_mistakes(text, message):
    with pytest.raises(ValueError, match=message.replace("[", r"\[")):
        config.parse(text)


def test_empty_saves_path_turns_backup_off():
    assert config.parse("[saves]\npath = ''").saves is None


def test_flags_override_only_when_given():
    cfg = config.Config(dest="/", sync_mode="add")
    assert cfg.override(dest="/x", sync_mode=None).dest == "/x"
    assert cfg.override(dest=None).dest == "/"


def test_search_order(monkeypatch, tmp_path):
    user, system = tmp_path / "user.toml", tmp_path / "system.toml"
    system.write_text("[cart]\ndest = '/a'")
    monkeypatch.delenv("SFE_CONFIG", raising=False)
    monkeypatch.setattr(config, "SEARCH", (user, system))
    assert config.load().dest == "/a"
    user.write_text("[cart]\ndest = '/b'")
    assert config.load().dest == "/b"
    env = tmp_path / "env.toml"
    env.write_text("[cart]\ndest = '/c'")
    monkeypatch.setenv("SFE_CONFIG", str(env))
    assert config.load().dest == "/c"
    assert config.load(str(system)).dest == "/a"
    with pytest.raises(ValueError, match="can't read config"):
        config.load(str(tmp_path / "nope.toml"))


# --- app install -----------------------------------------------------------------------


def menu_rom(seed=7) -> bytes:
    """A stand-in for EDN8/nesos.nes: 2 PRG banks, one CHR bank of arbitrary glyphs."""
    return b"NES\x1a\x02\x01" + bytes(10) + bytes(0x8000) + bytes((seed * i) & 0xFF for i in range(0x2000))


def put_menu(sd, data):
    (sd / "EDN8").mkdir(exist_ok=True)
    (sd / "EDN8" / "nesos.nes").write_bytes(data)


def test_packaged_app_has_the_edio_font():
    rom = apps.packaged()
    edio = Path(__file__).resolve().parents[2] / "nes" / "edio" / "font.chr"
    assert rom[-chr.CHR_SIZE :] == chr.build(edio.read_bytes()[: chr.GLYPHS])


def test_installs_with_the_carts_menu_font(dev, sd):
    put_menu(sd, menu_rom())
    r = apps.ensure_app(dev, "/Sync Forever.nes")
    assert (r.status, r.font, r.path) == ("installed", "menu", "Sync Forever.nes")
    on_cart = (sd / "Sync Forever.nes").read_bytes()
    assert on_cart == chr.patch_rom(apps.packaged(), chr.menu_glyphs(menu_rom()))
    assert on_cart[: -chr.CHR_SIZE] == apps.packaged()[: -chr.CHR_SIZE]


def test_second_run_leaves_it_alone(dev, sd, sim):
    put_menu(sd, menu_rom())
    apps.ensure_app(dev, "/Sync Forever.nes")
    writes = len(sim.write_sizes)
    assert apps.ensure_app(dev, "/Sync Forever.nes").status == "current"
    assert sum(sim.write_sizes[writes:]) < 2000  # pings, stat and a readback; no upload


def test_replaces_an_old_version(dev, sd):
    (sd / "Sync Forever.nes").write_bytes(b"old app")
    r = apps.ensure_app(dev, "Sync Forever.nes")
    assert (r.status, r.font) == ("updated", "edio")  # no EDN8/nesos.nes on this card
    assert (sd / "Sync Forever.nes").read_bytes() == apps.packaged()


def test_a_new_menu_font_updates_the_app(dev, sd):
    put_menu(sd, menu_rom(7))
    apps.ensure_app(dev, "Sync Forever.nes")
    put_menu(sd, menu_rom(9))  # firmware update
    assert apps.ensure_app(dev, "Sync Forever.nes").status == "updated"


def test_unusable_menu_rom_falls_back_to_edio(dev, sd):
    put_menu(sd, b"not a ROM")
    assert apps.ensure_app(dev, "Sync Forever.nes").font == "edio"


def test_installs_into_a_new_folder(dev, sd):
    assert apps.ensure_app(dev, "/Tools/Sync Forever.nes").status == "installed"
    assert (sd / "Tools" / "Sync Forever.nes").is_file()


@pytest.mark.parametrize("path", ["/EDN8/Sync Forever.nes", "/edn8/x.nes", "/Sync Forever.txt"])
def test_refuses_bad_paths(dev, path):
    with pytest.raises((ValueError, EverDriveError)):
        apps.ensure_app(dev, path)


def test_needs_the_menu(sd):
    from syncforever.n8pro import N8Pro
    from syncforever.sim import FakeN8Pro

    d = N8Pro(FakeN8Pro(sd, in_game=True))
    d.connect()
    with pytest.raises(UnsafeStateError):
        apps.ensure_app(d, "Sync Forever.nes")
    assert not (sd / "Sync Forever.nes").exists()


def test_the_app_path_takes_the_carts_extension():
    from syncforever.carts import MEGA_PRO, N8_PRO

    assert apps.cart_path("/Sync Forever.nes", N8_PRO) == "Sync Forever.nes"
    assert apps.cart_path("/Sync Forever.nes", MEGA_PRO) == "Sync Forever.md"
    assert apps.cart_path("/Tools/Sync Forever", MEGA_PRO) == "Tools/Sync Forever.md"
    assert apps.cart_path("", MEGA_PRO) is None


def test_packaged_genesis_app_is_up_to_date(tmp_path):
    """data/sync-forever.md must be rebuilt (`make -C md package`) whenever the app changes."""
    import shutil
    import subprocess

    if not shutil.which("m68k-elf-gcc"):
        pytest.skip("m68k-elf-gcc not installed (brew install m68k-elf-gcc)")
    md = Path(__file__).resolve().parents[2] / "md"
    out = tmp_path / "md"
    subprocess.run(["make", "-C", str(md), "-s", f"B={out}"], check=True, capture_output=True)
    assert (out / "sync-forever.md").read_bytes() == apps.packaged(".md"), "run: make -C md package"


def test_folders_per_console_fall_back_to_the_defaults(tmp_path):
    cfg = config.parse(
        """
        [library]
        path = "/mnt/games/roms"
        [saves]
        path = "/mnt/games/saves"
        [cart]
        dest = "/"
        [nes]
        library = "/mnt/games/roms/nes"
        saves = "/mnt/games/saves/nes"
        [genesis]
        library = "/mnt/games/roms/sega"
        saves = ""
        dest = "/sync"
        sync_mode = "mirror"
        """
    )
    nes, gen = cfg.console("nes"), cfg.console("genesis")
    assert (nes.library, nes.saves, nes.dest, nes.sync_mode) == (
        Path("/mnt/games/roms/nes"),
        Path("/mnt/games/saves/nes"),
        "/",
        "add",
    )
    assert (gen.library, gen.saves, gen.dest, gen.sync_mode) == (Path("/mnt/games/roms/sega"), None, "/sync", "mirror")
    assert config.parse("[library]\npath = '/r'").console("genesis").library == Path("/r")  # defaults only
    assert config.Config().any_library() is False and cfg.any_library()


@pytest.mark.parametrize(
    "text, message",
    [
        ("[nes]\nlibary = '/x'", "unknown setting nes.libary"),
        ("[genesis]\nsync_mode = 'copy'", "sync mode must be one of"),
        ("[n64]\nfirst_look = 'never'", "first_look must be one of"),
        ("[nes]\nlibrary = 3", "nes.library must be a string"),
    ],
)
def test_console_sections_reject_mistakes(text, message):
    with pytest.raises(ValueError, match=message):
        config.parse(text)


def test_first_look_defaults_to_auto_for_each_console():
    cfg = config.parse("[n64]\nfirst_look = 'trust'\n")
    assert cfg.console("n64").first_look == "trust" and cfg.console("snes").first_look == "auto"


def test_msu_is_off_unless_turned_on():
    assert not config.parse("").console("snes").msu
    assert config.parse("[genesis]\nmsu = true\n").console("genesis").msu
    with pytest.raises(ValueError, match="true or false"):
        config.parse('[snes]\nmsu = "no"\n')


def test_save_states_follow_the_saves_unless_told_otherwise():
    assert config.parse('[snes]\nsaves = "/s"\n').console("snes").states == Path("/s")
    assert config.parse('[snes]\nsaves = "/s"\nstates = "/t"\n').console("snes").states == Path("/t")
    assert config.parse('[snes]\nsaves = "/s"\nstates = ""\n').console("snes").states is None
    assert config.parse('[saves]\npath = "/d"\n').console("snes").states == Path("/d")
    assert config.parse("").console("snes").states is None
