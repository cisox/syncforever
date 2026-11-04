"""sfe setup and sfe doctor (setup.py, doctor.py)."""

import os
import pwd

from syncforever import config, doctor
from syncforever.setup import Setup, set_values

ME = pwd.getpwuid(os.getuid()).pw_name
EXAMPLE = """# a comment that stays
[nes]
# the NES library
library = "/old/nes"
saves = "/old/saves"

[cart]
dest = "/"
sync_mode = "add"
"""


def test_set_values_replaces_in_place_adds_and_appends():
    text = set_values(EXAMPLE, "nes", {"library": "/new/nes", "saves": ""})
    text = set_values(text, "cart", {"app_path": '/My "App".nes'})
    text = set_values(text, "snes", {"library": "/roms/snes"})
    assert "# a comment that stays" in text and "# the NES library" in text
    cfg = config.parse(text)
    assert str(cfg.console("nes").library) == "/new/nes" and cfg.console("nes").saves is None
    assert cfg.app_path == '/My "App".nes'
    assert str(cfg.console("snes").library) == "/roms/snes"


def run_setup(tmp_path, answers):
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE + f'\n[box]\nstate = "{tmp_path / "state"}"\n')
    lib = tmp_path / "snes"
    lib.mkdir()
    answers = iter(answers)
    asked, out, ran = [], [], []

    def ask(prompt):
        asked.append(prompt)
        return next(answers)

    s = Setup(path, ask=ask, out=out.append, dropin=tmp_path / "dropin" / "10-user.conf", run=ran.append)
    assert s.run_setup() == 0
    return path, asked, out, ran, tmp_path / "dropin" / "10-user.conf"


def test_setup_keeps_current_values_on_enter_and_writes_the_user(tmp_path):
    snes = str(tmp_path / "snes")
    #         nes lib, nes saves, gen lib, gen saves, n64 x2, snes lib, snes saves, mode, user
    answers = ["", "-", "", "", "", "", "", snes, "", "", "update", ME]  # states: Genesis, SNES only
    path, asked, _out, ran, dropin = run_setup(tmp_path, ["", "y", *answers[1:]])
    cfg = config.load(str(path))
    assert str(cfg.console("nes").library) == "/old/nes"  # kept (and confirmed though missing)
    assert cfg.console("nes").saves is None  # "-" cleared it
    assert str(cfg.console("snes").library) == snes
    assert cfg.sync_mode == "update"
    assert f"User={ME}" in dropin.read_text() and "SupplementaryGroups=dialout" in dropin.read_text()
    assert ["systemctl", "restart", "syncforever"] in ran
    assert (tmp_path / "state").is_dir()
    assert any("/old/nes doesn't exist" in a for a in asked)


def test_setup_asks_where_save_states_go(tmp_path):
    states = tmp_path / "states"
    states.mkdir()

    def answer(reply, own=None):
        return Setup(tmp_path / "c.toml", ask=lambda prompt: reply, out=lambda m: None).states_folder(own)

    set_folder = config.ConsoleSettings(states=states)
    assert answer("") is None and answer("-") is None  # with the saves: no setting
    assert answer("off") == ""  # not backed up
    assert answer(str(states)) == str(states)
    assert answer("", set_folder) == str(states)  # Enter keeps it
    assert answer("-", set_folder) is None
    assert answer("", config.ConsoleSettings(states=config.OFF)) == ""


def test_set_values_removes_a_setting_given_none():
    text = '[snes]\nsaves = "/s"\nstates = "/t"\nmsu = true\n'
    assert set_values(text, "snes", {"states": None}) == '[snes]\nsaves = "/s"\nmsu = true\n'
    assert set_values(text, "snes", {"nothing": None}) == text
    assert "nothing" not in set_values("", "snes", {"saves": "/s", "nothing": None})


def test_setup_asks_again_for_a_bad_mode_or_user(tmp_path):
    #          nes lib (missing: y), nes saves, gen x2, n64 x2, snes x2, modes, users
    answers = ["", "y", "-", "", "", "", "", "", "", "", "", "sometimes", "mirror", "no-such-user-xyz", ME]
    path, *_ = run_setup(tmp_path, answers)
    assert config.load(str(path)).sync_mode == "mirror"


def write_cfg(tmp_path, body):
    p = tmp_path / "c.toml"
    p.write_text(body + f'\n[box]\nstate = "{tmp_path / "state"}"\n')
    (tmp_path / "state").mkdir(exist_ok=True)
    return p


def test_doctor_counts_roms_and_flags_links_out_and_missing_folders(tmp_path):
    lib, saves, elsewhere = tmp_path / "lib", tmp_path / "saves", tmp_path / "elsewhere"
    for d in (lib / "sub", saves, elsewhere):
        d.mkdir(parents=True)
    (lib / "a.nes").write_bytes(b"A")
    (lib / "sub" / "b.nes").write_bytes(b"B")
    (lib / "notes.txt").write_text("x")
    (elsewhere / "c.nes").write_bytes(b"C")
    (lib / "c.nes").symlink_to(elsewhere / "c.nes")
    p = write_cfg(tmp_path, f'[nes]\nlibrary = "{lib}"\nsaves = "{saves}"\n[snes]\nsaves = "{tmp_path / "gone"}"\n')
    out = []
    assert doctor.check_folders(p, out.append) is False  # the SNES save folder is missing
    text = "\n".join(out)
    assert f"library {lib}: 2 ROMs" in text
    assert "1 ROMs are links to files outside the library" in text
    assert f"save folder {saves}: writable" in text
    assert "[FAIL] save folder" in text and "gone is missing" in text
    assert not list(saves.iterdir())  # the test file is gone


def test_doctor_is_happy_with_a_good_setup(tmp_path):
    lib, saves = tmp_path / "lib", tmp_path / "saves"
    lib.mkdir()
    saves.mkdir()
    (lib / "Mario.sfc").write_bytes(b"M")
    p = write_cfg(tmp_path, f'[snes]\nlibrary = "{lib}"\nsaves = "{saves}"\n')
    out = []
    assert doctor.check_folders(p, out.append) is True
    assert "FAIL" not in "\n".join(out)
