"""Sync Forever (sfe): list, copy and benchmark files on an EverDrive-N8 PRO over USB."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import ops
from .n8pro import N8Pro
from .protocol import EverDriveError, error_name
from .transport import PortBusy, SerialTransport, find_ports

_UNITS = {"K": 1024, "M": 1024 * 1024}


def parse_size(text):
    text = text.strip().upper()
    if text[-1] in _UNITS:
        return int(text[:-1]) * _UNITS[text[-1]]
    return int(text)


APP_Z64 = Path(__file__).parent / "data" / "sync-forever.z64"


def open_x7(args):
    """Our app on an EverDrive-64 X7, booted from the menu if it isn't running (x7.py)."""
    from . import x7

    if args.sim:
        from .x7sim import FakeX7

        transport = FakeX7(args.sim)
    else:
        port = args.port
        if not port:
            ports = x7.find_ports()
            if len(ports) != 1:
                found = ", ".join(p.device for p in ports) or "none"
                raise EverDriveError(f"pass --port: X7 ports found: {found}")
            port = ports[0].device
        transport = SerialTransport(port, timeout=0.2)
    rom = APP_Z64.read_bytes() if APP_Z64.exists() else None
    return x7.connect(transport, rom, log=lambda msg: print(msg, file=sys.stderr))


def open_fxpak(args):
    """An FXPAK PRO (SNES): the cart's own USB file service (fxpak.py)."""
    from . import fxpak

    if args.sim:
        from .fxsim import FakeFxPak

        game = {"rom": "/SNES/Game.sfc"} if getattr(args, "sim_in_game", False) else {}
        return fxpak.FxPak(FakeFxPak(args.sim, **game))
    from .transport import open_port

    port = args.port
    if not port:
        ports = fxpak.find_ports()
        if len(ports) != 1:
            found = ", ".join(p.device for p in ports) or "none"
            raise EverDriveError(f"pass --port: FXPAK ports found: {found}")
        port = ports[0].device
    return fxpak.FxPak(open_port(port, timeout=0.2, write_timeout=fxpak.WRITE_TIMEOUT))


def open_device(args):
    if args.x7:
        return open_x7(args)
    if args.fxpak:
        return open_fxpak(args)
    if args.sim:
        from .sim import FakeN8Pro

        transport = FakeN8Pro(args.sim, in_game=args.sim_in_game)
    elif args.port:
        transport = SerialTransport(args.port)
    else:
        transport = SerialTransport.auto()
    dev = N8Pro(transport)
    dev.connect()
    return dev


def _progress(label):
    t0 = time.monotonic()

    def show(done, total):
        rate = done / 1024 / max(time.monotonic() - t0, 1e-3)
        of = f"/{total / 1024:.0f}" if total else ""
        sys.stderr.write(f"\r  {label}: {done / 1024:.0f}{of} KiB ({rate:.0f} KiB/s)")
        if total and done >= total:
            sys.stderr.write("\n")

    return show


def _fmt_ver(v):
    return f"{v >> 8:X}.{v & 0xFF:02X}"


def cmd_ports(args):
    ports = find_ports()
    for p in ports:
        print(f"{p.device}\t{p.vid:04X}:{p.pid:04X}\t{p.description}")
    if not ports:
        print("no EverDrive found on USB", file=sys.stderr)
        return 1
    return 0


def cmd_info(args, dev):
    if dev.profile.key == "snes":
        i = dev.info()
        print(f"device     : {i.device} ({dev.profile.name})")
        print(f"firmware   : {i.version}")
        print(f"running    : {i.rom}{' (the menu)' if i.at_menu else ''}")
        return
    if dev.profile.flat_saves:  # the X7: no SYS_INF, our app answers instead
        print(f"device     : {dev.profile.name}, our app v{dev.hello.version} (protocol {dev.hello.proto})")
        return
    info = dev.sys_info()
    status = dev.status()
    at_menu = "yes" if dev.menu_ping() else "no"
    built = f"{info.built:%Y-%m-%d %H:%M}" if info.built else "?"
    print(f"device     : protocol 0x{dev.protocol:02X}, device 0x{dev.device:02X} ({dev.profile.name})")
    print(f"firmware   : mcu {_fmt_ver(info.sw_ver)}, hw {_fmt_ver(info.hw_ver)}, boot {_fmt_ver(info.boot_ver)}")
    print(f"built      : {built} (the menu's own version is not reported over USB)")
    print(f"serial     : {info.serial}")
    print(f"form       : {info.cart_form}")
    console = dev.profile.console.lower()
    print(f"power      : {console} {'on' if info.pwr_sys else 'off'}, usb {'on' if info.pwr_usb else 'off'}")
    print(f"counters   : boot {info.boot_ctr}, game {info.game_ctr}")
    print(f"at menu    : {at_menu}")
    print(f"last result: 0x{status:02X}{'' if status == 0 else ' ' + error_name(status)}")
    if args.raw:
        print(f"sys_inf raw: {info.raw.hex()}")


def cmd_ls(args, dev):
    for e in dev.list_dir(args.path):
        when = e.mtime.strftime("%Y-%m-%d %H:%M") if e.mtime else "????-??-?? ??:??"
        size = "<dir>" if e.is_dir else str(e.size)
        print(f"{size:>10}  {when}  {e.name}{'/' if e.is_dir else ''}")


def cmd_stat(args, dev):
    e = dev.stat(args.path)
    if e is None:
        print(f"{args.path}: not found", file=sys.stderr)
        return 1
    print(json.dumps({"name": e.name, "size": e.size, "is_dir": e.is_dir, "mtime": str(e.mtime)}))


def cmd_get(args, dev):
    local = Path(args.local)
    try:
        with open(local, "wb" if args.overwrite else "xb") as f:
            r = ops.download(dev, args.remote, f, progress=_progress(args.remote))
    except FileExistsError:
        raise
    except BaseException:
        local.unlink(missing_ok=True)
        raise
    print(f"{r.size} bytes  sha256 {r.sha256}  {r.kib_per_s:.0f} KiB/s")


def cmd_launch(args, dev):
    mapper = ops.launch(dev, args.path)
    print(f"started {ops.norm(args.path)}" + ("" if mapper is None else f" (mapper {mapper})"))


def cmd_put(args, dev, policy):
    local = Path(args.local)
    with open(local, "rb") as f:
        r = ops.upload(
            dev,
            f,
            args.remote,
            policy=policy,
            overwrite=args.overwrite,
            progress=_progress(args.remote),
            total=local.stat().st_size,
        )
    print(f"sent {r.size} bytes  sha256 {r.sha256}  {r.kib_per_s:.0f} KiB/s")
    if not args.no_verify:
        v = ops.verify(dev, r.path, r.sha256)
        print(f"verified readback  {v.kib_per_s:.0f} KiB/s")


def cmd_install(args, dev, policy):
    """Put this box's Sync Forever app on the cart (or bring it up to date). At the menu on the
    PRO carts; on the X7 through the app itself, which --x7 boots from the menu first."""
    from . import apps

    path = apps.cart_path(args.app_path, dev.profile)
    if path is None:
        raise EverDriveError(f"no Sync Forever app for the {dev.profile.name}")
    r = apps.ensure_app(dev, path)
    print(f"/{r.path}: {r.status}")


def cmd_mkdir(args, dev, policy):
    path = policy.check(args.path)
    ops.require_menu(dev)
    print("created" if dev.mkdir(path) else "already exists")


def cmd_rm(args, dev, policy):
    path = policy.check(args.path)
    ops.require_menu(dev)
    if not dev.delete(path):
        print(f"{args.path}: not found", file=sys.stderr)
        return 1


def cmd_crc(args, dev):
    """A file's CRC computed on the cart (F_FCRC), timed, against a local copy if given: to try
    on a cart before a sync relies on it (CartProfile.crc)."""
    ops.require_menu(dev)
    entry = dev.stat(args.path)
    if entry is None or entry.is_dir:
        print(f"{args.path}: no such file", file=sys.stderr)
        return 1
    t0 = time.monotonic()
    crc = ops.cart_crc(dev, args.path, entry.size)
    took = time.monotonic() - t0
    rate = entry.size / 1024 / took if took else 0
    print(f"{args.path}: {entry.size} bytes, crc {crc:08x}, {took:.2f} s ({rate:.0f} KiB/s)")
    if args.local:
        want = ops.file_hashes(args.local)[1]
        swapped = int.from_bytes(want.to_bytes(4, "big"), "little")
        same = "matches" if crc == want else "matches byte-swapped" if crc == swapped else "DIFFERS"
        print(f"{args.local}: crc32 {want:08x}: {same}")


def cmd_mv(args, dev, policy):
    """Rename a file on the card: the command a sync's moves use (romsync), to try on each cart
    before the box relies on it (CartProfile.moves)."""
    src, dst = policy.check(args.src), policy.check(args.dst)
    if args.raw:  # exactly as typed (a leading slash, a folder as dst): to probe the firmware
        src, dst = args.src, args.dst
    ops.require_menu(dev)
    do = dev.copy if args.cmd == "cp" else dev.move
    if args.mode is not None:
        do(src, dst, mode=args.mode)
    else:
        do(src, dst)
    print(f"{'copied' if args.cmd == 'cp' else 'moved'} {src} -> {dst}")


def cmd_bench(args, dev, policy):
    sizes = [parse_size(s) for s in args.sizes.split(",")]
    if dev.profile.key == "snes":  # the FXPAK: INFO, not SYS_INF; optional upload pacing
        fx = dev.info()
        firmware, serial = fx.version, "FXPAK"
        if args.gap is not None:
            dev.block_gap = args.gap / 1000
    else:
        info = dev.sys_info()
        firmware, serial = _fmt_ver(info.sw_ver), info.serial
    print(f"{'size':>9}  {'up KiB/s':>9}  {'down KiB/s':>10}  match", flush=True)

    def row(r):
        print(f"{r.size:>9}  {r.upload.kib_per_s:>9.0f}  {r.download.kib_per_s:>10.0f}  {r.match}", flush=True)

    results = ops.bench(dev, args.dir, sizes, policy=policy, keep=args.keep, report=row)
    if args.log:
        with open(args.log, "a") as f:
            for r in results:
                record = {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "firmware": firmware,
                    "serial": serial,
                    "transport": "sim" if args.sim else "usb",
                    **asdict(r),
                }
                f.write(json.dumps(record) + "\n")
    return 0 if all(r.match for r in results) else 1


def cmd_backup(args, dev):
    """Copy saves to versioned files under --store (reads only, at the menu)."""
    from . import saves

    store = saves.SaveStore(args.store)
    flat = dev.profile.flat_saves  # the X7: no per-game folders, back up all of gamedata
    results = saves.backup_all(dev, store) if args.all or flat else saves.backup_recent(dev, store, args.recent)
    for r in results:
        where = f" -> {r.path}" if r.path else ""
        print(f"{r.game}/{r.file}: {r.status}{where}")
    if not results:
        print("nothing to back up")
    return 0


def cmd_clock(args, dev):
    """Show the cart clock next to this computer's; --set copies ours over (at the menu)."""
    from datetime import datetime

    if args.set:
        ops.require_menu(dev, "setting the clock")
        dev.rtc_set(datetime.now())
    cart = dev.rtc_get()
    now = datetime.now().replace(microsecond=0)
    if cart is None:
        print(f"cart : (invalid date)\nhere : {now}")
    else:
        print(f"cart : {cart}\nhere : {now}\noff by {(cart - now).total_seconds():+.0f} s")
    return 0


def _cart_id(dev) -> str:
    """The name of this cart's sync records, as the box names them (box.open_serial)."""
    from . import fxpak
    from .box import safe_id

    device = getattr(dev.t, "port", None)
    for p in fxpak.find_ports() if device else ():
        if p.device == device:
            return safe_id(p.serial_number or "FXPAK")
    return "sim"


def _fxpak_at_menu(dev) -> None:
    """The FXPAK writes only at its menu: in a game (our app included), the cart's autosave can
    meet an upload and hang its USB (fxapp.py). The box's app link has its own gate."""
    if dev.profile.key != "snes":
        return
    info = dev.info()
    if not info.at_menu:
        raise EverDriveError(
            f"the SNES is running {info.rom}: go back to the menu first (in a game, the cart's"
            " autosave can meet an upload and hang its USB)"
        )


def cmd_sync(args, dev):
    """Sync the library to the cart from here, with the box's settings and records. The FXPAK
    only for now: the other carts sync from the Sync Forever app, which shows the plan first."""
    from . import apps, config, romsync
    from .box import policy_for

    if dev.profile.key != "snes":
        raise EverDriveError("sync from the command line is for the FXPAK (--fxpak); other carts sync from the app")
    cfg = config.load(args.config).override(state=Path(args.state).expanduser() if args.state else None)
    c = cfg.console(dev.profile.key)
    library = Path(args.library).expanduser() if args.library else c.library
    dest = args.dest if args.dest is not None else c.dest
    mode = args.mode or c.sync_mode
    if library is None or not library.is_dir():
        raise EverDriveError(f"no library folder: {library or 'set [snes] library in the config, or pass --library'}")
    policy = policy_for(dest)
    dest = policy.check(dest)
    manifest = romsync.Manifest(cfg.state.expanduser() / f"cart-{_cart_id(dev)}.json")
    print(f"{library} -> cart /{dest}, {romsync.MODE_NAMES[mode]}; records {manifest.path}", flush=True)

    def progress(msg, detail=None, expect=None, left=None):
        when = f", ~{expect // 60}:{expect % 60:02d}" if expect else ""
        print(f"  {msg} {detail or ''}".rstrip() + when, flush=True)

    app = apps.cart_path(cfg.app_path, dev.profile)  # never synced away: the box's app
    plan = romsync.make_plan(
        dev,
        library,
        dest,
        mode,
        manifest,
        progress=progress,
        keep=(app,) if app else (),
        msu=dev.profile.msu,
        msu_on=c.msu,
        first_look=c.first_look,
    )
    for label, items in (
        ("add", plan.add),
        ("move", [f"{src} -> {rel}" for src, rel in plan.moves]),
        ("replace", plan.replace),
        ("remove", plan.remove + plan.remove_dirs),
        ("skip", plan.skipped),
        ("no msu", plan.left_out),
    ):
        for item in items:
            print(f"  {label:7} {item}")
    print(f"plan: {plan.summary()}, unchanged {plan.unchanged}", flush=True)
    if plan.empty:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("nothing copied: pass --yes to carry it out")
            return 0
        if input("carry it out? [y/N] ").strip().lower() not in ("y", "yes"):
            return 0
    t0 = time.monotonic()
    summary = romsync.run_plan(
        dev,
        plan,
        library,
        dest,
        manifest,
        policy=policy,
        progress=progress,
        log=lambda line: print(f"  {line}", flush=True),
    )
    print(f"done in {time.monotonic() - t0:.0f}s: {summary}")
    return 0


def cmd_serve(args):
    """Be the box: wait for a cart, set it up, back up saves, answer the app (box.py). Ctrl-C stops."""
    from . import config
    from .box import Box, open_serial, serial_carts, serial_devices
    from .carts import CONSOLES

    cfg = config.load(args.config).override(
        library=Path(args.library).expanduser() if args.library else None,
        saves=Path(args.saves).expanduser() if args.saves else None,
        dest=args.dest,
        app_path=args.app_path,
        sync_mode=args.sync_mode,
        set_clock=False if args.no_clock else None,
        state=Path(args.state).expanduser() if args.state else None,
        port=args.port,
    )

    def log(msg):
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    if not cfg.any_library() and not any(cfg.console(k).saves for k in CONSOLES):
        consoles = "/".join(f"[{k}]" for k in CONSOLES)
        raise ValueError(
            f"nothing to do: set a library or saves folder under {consoles} (or [library] path) in the config,"
            " or pass --library"
        )
    log(f"config: {cfg.source or 'defaults (no config file)'}")
    app = f"/{ops.norm(cfg.app_path)}" if cfg.app_path else "off"
    for key in CONSOLES:
        c = cfg.console(key)
        states = "" if c.states == c.saves else f", save states {c.states or 'off'}"
        log(
            f"{key}: library {c.library or 'none'} -> cart /{ops.norm(c.dest)}, sync mode {c.sync_mode}, "
            f"saves {c.saves or 'off'}{states}"
        )
    log(f"app {app}. Ctrl-C stops.")
    from . import n8pro

    n8pro.allow_writes()
    previous_term = signal.signal(signal.SIGTERM, _interrupt)  # systemctl stop: the same as Ctrl-C
    try:
        if args.sim:
            from .sim import FakeN8Pro

            carts = iter([FakeN8Pro(args.sim, in_game=args.sim_in_game)])

            def open_cart(log):
                cart = next(carts, None)
                if cart is None:
                    raise KeyboardInterrupt  # the simulated cart is gone for good
                return cart

            Box(cfg, open_cart, log=log).run()
        elif cfg.port:
            Box(cfg, serial_carts(cfg.port), log=log).run()
        else:
            Box(cfg, log=log, tag=True).serve_all(serial_devices, open_serial)
    except KeyboardInterrupt:
        _finish_writes(log)
        log("stopped")
    finally:
        signal.signal(signal.SIGTERM, previous_term)
    return 0


def _interrupt(*_):
    raise KeyboardInterrupt


STOP_WAIT = 60.0  # a write in progress finishes long before this; a stuck cart times out sooner


def _finish_writes(log):
    """Cart threads can't defer a stop (only the main thread gets signals): wait here for any
    payload in flight, ignoring further stop signals, since stopping one short wedges its cart."""
    from . import n8pro

    if n8pro.writes_in_flight():
        log("stopping after the write in progress")
    previous = {s: signal.signal(s, signal.SIG_IGN) for s in n8pro.STOP_SIGNALS}
    try:
        if not n8pro.stop_writes(STOP_WAIT):
            log("a write didn't finish; power-cycle that cart before using it again")
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


SERVICE = "syncforever"  # deploy/syncforever.service
FREE_WAIT = 10.0  # seconds for a stopped service to let go of the port


def service_active() -> bool:
    """The box service runs here (systemd)."""
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], check=False).returncode == 0
    except OSError:  # no systemctl (not Linux)
        return False


def _open_when_free(args):
    end = time.monotonic() + FREE_WAIT
    while True:
        try:
            return open_device(args)
        except PortBusy:
            if time.monotonic() > end:
                raise
            time.sleep(0.2)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--port", help="serial device (default: every EverDrive found; serve runs one session each)")
    src.add_argument("--sim", metavar="DIR", help="use a simulated cart whose SD card is DIR")
    ap.add_argument("--sim-in-game", action="store_true", help="simulated cart reports a running game")
    ap.add_argument("--x7", action="store_true", help="an EverDrive-64 X7: boot our app from its menu and use it")
    ap.add_argument("--fxpak", action="store_true", help="an FXPAK PRO (SNES): the cart's own USB file service")
    ap.add_argument(
        "--write-root",
        action="append",
        metavar="PATH",
        help=f"folder writes are allowed under (repeatable; default /{ops.DEFAULT_WRITE_ROOT})",
    )
    ap.add_argument("--allow-write-anywhere", action="store_true", help="disable the write-root guard")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list connected EverDrive serial ports")
    p = sub.add_parser("info", help="identify the cart and print firmware details")
    p.add_argument("--raw", action="store_true", help="also dump the raw SYS_INF bytes")
    p = sub.add_parser("ls", help="list a folder")
    p.add_argument("path", nargs="?", default="/")
    p = sub.add_parser("stat", help="show one file's metadata")
    p.add_argument("path")
    p = sub.add_parser("get", help="download a file and print its sha256")
    p.add_argument("remote")
    p.add_argument("local")
    p.add_argument("--overwrite", action="store_true", help="replace an existing local file")
    p = sub.add_parser("launch", help="start a ROM through the cart's menu")
    p.add_argument("path")
    p = sub.add_parser("put", help="upload a file, then read it back and compare sha256")
    p.add_argument("local")
    p.add_argument("remote")
    p.add_argument("--overwrite", action="store_true", help="replace an existing file on the cart")
    p.add_argument("--no-verify", action="store_true", help="skip the readback check")
    p = sub.add_parser("install", help="put the Sync Forever app on the cart, or update it")
    p.add_argument("--app-path", default="/Sync Forever.nes", help="where (the cart's extension replaces .nes)")
    p = sub.add_parser("mkdir", help="create a folder")
    p.add_argument("path")
    p = sub.add_parser("rm", help="delete a file or empty folder")
    p.add_argument("path")
    p = sub.add_parser("crc", help="a file's CRC computed on the cart (N8/Mega), to try it")
    p.add_argument("path")
    p.add_argument("--local", help="a copy on this computer to compare with")
    p = sub.add_parser("mv", help="rename a file (to try a cart's rename before syncs use it)")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--mode", type=lambda v: int(v, 0), default=None, help=argparse.SUPPRESS)  # F_FMV's byte
    p.add_argument("--raw", action="store_true", help=argparse.SUPPRESS)  # paths as typed
    p = sub.add_parser("cp", help="copy a file on the cart itself (N8/Mega: F_FCP), to try it")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--mode", type=lambda v: int(v, 0), default=None, help=argparse.SUPPRESS)
    p.add_argument("--raw", action="store_true", help=argparse.SUPPRESS)
    p = sub.add_parser("bench", help="upload, read back and delete files of several sizes")
    p.add_argument("--dir", default=f"{ops.DEFAULT_WRITE_ROOT}/bench")
    p.add_argument("--sizes", default="32K,256K,1M,4M")
    p.add_argument("--log", metavar="FILE", help="append JSON-lines results here")
    p.add_argument("--keep", action="store_true", help="leave the files on the cart")
    p.add_argument(
        "--gap",
        type=float,
        default=None,
        metavar="MS",
        help="FXPAK: pause after each 512-byte block uploaded (default 1; 0 sends back to back)",
    )
    p = sub.add_parser("clock", help="show the cart clock; --set copies this computer's time to it")
    p.add_argument("--set", action="store_true")
    p = sub.add_parser(
        "backup", help="back up every game's save files (battery saves, save states) to versioned files (reads only)"
    )
    p.add_argument("--store", required=True, metavar="DIR", help="backup folder (the save folder)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--recent", type=int, default=1, metavar="N", help="the N most recently played games (default 1)")
    g.add_argument("--all", action="store_true", help="every file of every game in the cart's gamedata folder")
    p = sub.add_parser("sync", help="FXPAK: sync the library to the cart (shows the plan; --yes carries it out)")
    p.add_argument("--config", metavar="FILE", help="settings file (default: $SFE_CONFIG, then the usual places)")
    p.add_argument("--library", metavar="DIR", help="folder of ROMs (config: [snes] library)")
    p.add_argument("--dest", help="cart folder the ROMs go to (config: [snes] dest)")
    p.add_argument("--mode", choices=("add", "update", "mirror", "strict"), help="config: [snes] sync_mode")
    p.add_argument("--state", metavar="DIR", help="where the sync records are (config: [box] state)")
    p.add_argument("--yes", action="store_true", help="carry out the plan without asking")
    p = sub.add_parser("setup", help="set up the box by question and answer: folders, sync mode, user (sudo)")
    p.add_argument("--config", metavar="FILE", help="settings file (default: $SFE_CONFIG, then the usual places)")
    p = sub.add_parser("doctor", help="check the box's folders and service as the service's user, with fixes")
    p.add_argument("--config", metavar="FILE", help="settings file (default: $SFE_CONFIG, then the usual places)")
    p.add_argument("--as-is", action="store_true", help=argparse.SUPPRESS)  # runuser's second pass
    p = sub.add_parser("serve", help="be the box: wait for a cart, set it up, back up saves, answer the app")
    p.add_argument("--config", metavar="FILE", help="settings file (default: $SFE_CONFIG, then the usual places)")
    p.add_argument("--library", metavar="DIR", help="folder of ROMs to offer (config: [library] path)")
    p.add_argument("--saves", metavar="DIR", help="back up saves here (config: [saves] path)")
    p.add_argument("--dest", help="cart folder library ROMs go to (config: [cart] dest, default /)")
    p.add_argument(
        "--app-path",
        help='where to install the Sync Forever app (the cart\'s extension replaces .nes); "" = never (config: [cart] app_path)',
    )
    p.add_argument("--sync-mode", choices=("add", "update", "mirror", "strict"))
    p.add_argument("--no-clock", action="store_true", help="don't set the cart clock")
    p.add_argument("--state", metavar="DIR", help="where the box keeps its sync records (config: [box] state)")
    return ap


# Commands outside the write-root guard. launch writes nothing itself; it asks the menu to start a ROM.
READ_COMMANDS = {
    "info": cmd_info,
    "ls": cmd_ls,
    "crc": cmd_crc,
    "stat": cmd_stat,
    "get": cmd_get,
    "launch": cmd_launch,
    "backup": cmd_backup,
    "clock": cmd_clock,  # --set writes the clock, not the SD card
}
WRITE_COMMANDS = {
    "put": cmd_put,
    "mkdir": cmd_mkdir,
    "rm": cmd_rm,
    "mv": cmd_mv,
    "cp": cmd_mv,
    "bench": cmd_bench,
    "install": cmd_install,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "ports":
        return cmd_ports(args)
    if args.cmd in ("setup", "doctor"):
        try:
            if args.cmd == "setup":
                from . import doctor, setup

                setup.main(args.config)
                return doctor.main(args.config)
            from . import doctor

            return doctor.main(args.config, as_is=args.as_is)
        except (OSError, ValueError) as e:
            print(f"sfe: {e}", file=sys.stderr)
            return 2
        except (KeyboardInterrupt, EOFError):
            print("\nsfe: setup stopped; nothing more written", file=sys.stderr)
            return 1
    if args.cmd == "serve":
        try:
            return cmd_serve(args)
        except (EverDriveError, OSError, ValueError) as e:
            print(f"sfe: {e}", file=sys.stderr)
            return 2
    policy = ops.WritePolicy(args.write_root or (ops.DEFAULT_WRITE_ROOT,), anywhere=args.allow_write_anywhere)
    paused = False
    try:
        try:
            dev = open_device(args)
        except PortBusy:
            if not service_active():
                raise
            if os.geteuid() != 0:
                raise EverDriveError(
                    f"the {SERVICE} service is using the cart: run this with sudo (it pauses the service"
                    f" for the command), or stop it first: sudo systemctl stop {SERVICE}"
                ) from None
            print(f"sfe: pausing the {SERVICE} service for this command", file=sys.stderr)
            subprocess.run(["systemctl", "stop", SERVICE], check=True)
            paused = True
            dev = _open_when_free(args)
        try:
            if args.cmd == "sync" or args.cmd in WRITE_COMMANDS:
                _fxpak_at_menu(dev)
            if args.cmd == "sync":  # its own write guard: the configured dest (box.policy_for)
                return cmd_sync(args, dev)
            if args.cmd in READ_COMMANDS:
                return READ_COMMANDS[args.cmd](args, dev) or 0
            return WRITE_COMMANDS[args.cmd](args, dev, policy) or 0
        finally:
            dev.t.close()
    except (EverDriveError, OSError, ValueError) as e:
        print(f"sfe: {e}", file=sys.stderr)
        return 2
    finally:
        if paused:
            subprocess.run(["systemctl", "start", SERVICE], check=False)
            print(f"sfe: {SERVICE} service started again", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
