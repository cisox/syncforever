import json
import subprocess

import pytest

from syncforever.cli import main, parse_size


def run(sd, *argv):
    return main(["--sim", str(sd), *argv])


def test_parse_size():
    assert [parse_size(s) for s in ("32K", "1m", "100")] == [32768, 1 << 20, 100]


def test_ls_and_info(sd, capsys):
    assert run(sd, "ls", "roms/nes") == 0
    assert "Zelda.nes" in capsys.readouterr().out
    assert run(sd, "info") == 0
    out = capsys.readouterr().out
    assert "mcu 1.03" in out and "at menu    : yes" in out


def test_put_get_round_trip(sd, tmp_path, capsys):
    src = tmp_path / "in.bin"
    src.write_bytes(b"abc" * 5000)
    assert run(sd, "put", str(src), "syncforever-test/in.bin") == 0
    assert "verified readback" in capsys.readouterr().out
    dst = tmp_path / "out.bin"
    assert run(sd, "get", "syncforever-test/in.bin", str(dst)) == 0
    assert dst.read_bytes() == src.read_bytes()


def test_get_missing_file_leaves_no_local_file(sd, tmp_path):
    dst = tmp_path / "out.bin"
    assert run(sd, "get", "missing.bin", str(dst)) == 2
    assert not dst.exists()


def test_put_outside_write_root_is_refused(sd, tmp_path, capsys):
    src = tmp_path / "in.bin"
    src.write_bytes(b"x")
    assert run(sd, "put", str(src), "roms/nes/in.bin") == 2
    assert "outside the write roots" in capsys.readouterr().err
    assert not (sd / "roms/nes/in.bin").exists()


def test_put_refused_in_game(sd, tmp_path, capsys):
    src = tmp_path / "in.bin"
    src.write_bytes(b"x")
    assert main(["--sim", str(sd), "--sim-in-game", "put", str(src), "syncforever-test/in.bin"]) == 2
    assert "menu did not answer" in capsys.readouterr().err


def test_bench_logs_json_lines(sd, tmp_path):
    log = tmp_path / "bench.jsonl"
    assert run(sd, "bench", "--sizes", "1K,64K", "--log", str(log)) == 0
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["size"] for r in records] == [1024, 65536]
    assert all(r["match"] and r["transport"] == "sim" for r in records)


def test_launch(sd, capsys):
    assert run(sd, "launch", "/roms/nes/Zelda.nes") == 0
    assert "started roms/nes/Zelda.nes" in capsys.readouterr().out


@pytest.fixture
def no_config(monkeypatch, tmp_path):
    from syncforever import config

    monkeypatch.delenv("SFE_CONFIG", raising=False)
    monkeypatch.setattr(config, "SEARCH", ())


def stop_after(monkeypatch, polls):
    from syncforever import appserver

    calls = []

    def poll(self, timeout=0.05):
        calls.append(self)
        if len(calls) == polls:
            raise KeyboardInterrupt

    monkeypatch.setattr(appserver.AppServer, "poll", poll)
    return calls


def test_serve_sets_the_clock_then_serves(sd, tmp_path, capsys, monkeypatch, no_config):
    calls = stop_after(monkeypatch, 3)
    assert run(sd, "serve", "--library", str(tmp_path), "--state", str(tmp_path / "state")) == 0
    out = capsys.readouterr().out
    assert "config: defaults (no config file)" in out
    assert "set the cart clock" in out
    assert "ready" in out and "stopped" in out
    assert "app installed" not in out  # only once a game or the app runs (box.StartWatch)
    assert len(calls) == 3
    assert calls[0].dest == ""


def test_serve_reads_the_config_file_and_flags_win(sd, tmp_path, capsys, monkeypatch, no_config):
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""
        [library]
        path = "{lib}"
        [cart]
        dest = "/syncforever-test/sync"
        app_path = ""
        sync_mode = "mirror"
        set_clock = false
        [box]
        state = "{tmp_path / "state"}"
        """
    )
    calls = stop_after(monkeypatch, 1)
    assert run(sd, "serve", "--config", str(cfg), "--sync-mode", "update") == 0
    out = capsys.readouterr().out
    assert f"config: {cfg}" in out
    assert "app off" in out and "clock" not in out
    assert not (sd / "Sync Forever.nes").exists()
    server = calls[0]
    assert (server.library, server.dest, server.sync_mode) == (lib, "syncforever-test/sync", "update")


def test_serve_refuses_bad_setup(sd, tmp_path, capsys, no_config):
    assert run(sd, "serve") == 2
    assert "nothing to do: set a library or saves folder" in capsys.readouterr().err
    bad = tmp_path / "bad.toml"
    bad.write_text("[cart]\nsync_mode = 'copy'")
    assert run(sd, "serve", "--config", str(bad), "--library", str(tmp_path)) == 2
    assert "sync mode must be one of" in capsys.readouterr().err


def test_serve_warns_when_the_library_is_missing(sd, tmp_path, capsys, monkeypatch, no_config):
    stop_after(monkeypatch, 1)
    assert run(sd, "serve", "--library", str(tmp_path / "store"), "--state", str(tmp_path / "s")) == 0
    assert "library folder missing" in capsys.readouterr().out


def test_clock_show_and_set(sd, capsys):
    assert run(sd, "clock") == 0
    assert "cart : 2026-03-09 15:13:00" in capsys.readouterr().out
    assert run(sd, "clock", "--set") == 0
    out = capsys.readouterr().out
    assert "off by +0 s" in out or "off by -1 s" in out or "off by +1 s" in out


def test_clock_set_refuses_in_game(sd, capsys):
    assert main(["--sim", str(sd), "--sim-in-game", "clock", "--set"]) == 2
    assert "did not answer" in capsys.readouterr().err


def test_rtc_codec():
    from datetime import datetime

    from syncforever.n8pro import rtc_decode, rtc_encode

    t = datetime(2026, 11, 1, 10, 30, 59)
    assert rtc_encode(t) == bytes([0x26, 0x11, 0x01, 0x10, 0x30, 0x59])
    assert rtc_decode(rtc_encode(t)) == t
    assert rtc_decode(bytes(6)) is None  # month 0


# --- the box service holding the cart ----------------------------------------------------


class _Dev:
    class t:
        @staticmethod
        def close():
            pass


def _busy_then(monkeypatch, *, active, root, frees=True):
    """open_device: busy until `systemctl stop` (if frees); systemctl calls recorded."""
    from syncforever import cli
    from syncforever.transport import PortBusy

    calls, state = [], {"stopped": False}

    def run(cmd, check=False, **kw):
        calls.append(cmd[1:])
        if cmd[1] == "is-active":
            return subprocess.CompletedProcess(cmd, 0 if active else 3)
        if cmd[1] == "stop":
            state["stopped"] = frees
        return subprocess.CompletedProcess(cmd, 0)

    def open_device(args):
        if not state["stopped"]:
            raise PortBusy("/dev/ttyACM0 is in use by another program (the box service?)")
        return _Dev()

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "open_device", open_device)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0 if root else 1000)
    monkeypatch.setitem(cli.READ_COMMANDS, "info", lambda args, dev: 0)
    return calls


def test_a_busy_port_without_the_service_says_so(monkeypatch, capsys):
    _busy_then(monkeypatch, active=False, root=True)
    assert main(["info"]) == 2
    assert "in use by another program" in capsys.readouterr().err


def test_the_service_holding_the_cart_needs_sudo(monkeypatch, capsys):
    calls = _busy_then(monkeypatch, active=True, root=False)
    assert main(["info"]) == 2
    assert "run this with sudo" in capsys.readouterr().err
    assert ["stop", "syncforever"] not in calls


def test_with_sudo_the_service_is_paused_for_the_command(monkeypatch, capsys):
    calls = _busy_then(monkeypatch, active=True, root=True)
    assert main(["info"]) == 0
    assert calls == [["is-active", "--quiet", "syncforever"], ["stop", "syncforever"], ["start", "syncforever"]]
    err = capsys.readouterr().err
    assert "pausing the syncforever service" in err and "started again" in err


def test_the_service_is_started_again_even_when_the_port_stays_busy(monkeypatch):
    from syncforever import cli

    monkeypatch.setattr(cli, "FREE_WAIT", 0.0)
    calls = _busy_then(monkeypatch, active=True, root=True, frees=False)
    assert main(["info"]) == 2
    assert calls[-1] == ["start", "syncforever"]


def test_fxpak_writes_only_at_its_menu(tmp_path, capsys):
    sd = tmp_path / "sd"
    (sd / "sd2snes").mkdir(parents=True)
    src = tmp_path / "a.sfc"
    src.write_bytes(b"A" * 100)
    assert main(["--sim", str(sd), "--fxpak", "--sim-in-game", "put", str(src), "syncforever-test/a.sfc"]) == 2
    assert "go back to the menu first" in capsys.readouterr().err
    assert not (sd / "syncforever-test").exists()
    assert main(["--sim", str(sd), "--fxpak", "put", str(src), "syncforever-test/a.sfc"]) == 0
    assert (sd / "syncforever-test" / "a.sfc").read_bytes() == b"A" * 100


def test_mv_renames_a_file(tmp_path, capsys):
    sd = tmp_path / "sd"
    (sd / "syncforever-test").mkdir(parents=True)
    (sd / "syncforever-test" / "a.nes").write_bytes(b"A")
    assert run(sd, "mv", "syncforever-test/a.nes", "syncforever-test/b.nes") == 0
    assert (sd / "syncforever-test" / "b.nes").read_bytes() == b"A"
    assert not (sd / "syncforever-test" / "a.nes").exists()
    assert run(sd, "mv", "syncforever-test/b.nes", "EDN8/b.nes") == 2  # outside the write root


def test_cp_copies_a_file_on_the_cart(tmp_path):
    sd = tmp_path / "sd"
    (sd / "syncforever-test").mkdir(parents=True)
    (sd / "syncforever-test" / "a.nes").write_bytes(b"A")
    assert run(sd, "cp", "syncforever-test/a.nes", "syncforever-test/b.nes") == 0
    assert (sd / "syncforever-test" / "b.nes").read_bytes() == b"A"
    assert (sd / "syncforever-test" / "a.nes").exists()


def test_crc_is_computed_on_the_cart_and_compared(tmp_path, capsys):
    sd = tmp_path / "sd"
    (sd / "syncforever-test").mkdir(parents=True)
    (sd / "syncforever-test" / "a.nes").write_bytes(b"A" * 5000)
    local = tmp_path / "a.nes"
    local.write_bytes(b"A" * 5000)
    assert run(sd, "crc", "syncforever-test/a.nes", "--local", str(local)) == 0
    assert ": matches" in capsys.readouterr().out
    local.write_bytes(b"B" * 5000)
    assert run(sd, "crc", "syncforever-test/a.nes", "--local", str(local)) == 0
    assert "DIFFERS" in capsys.readouterr().out
