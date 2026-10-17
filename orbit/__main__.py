"""Run Orbit:  .venv/bin/python -m orbit [--lan] [--port 8787]"""

import argparse
import fcntl
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import __version__, routers, starlink
from .auth import Auth
from .collector import Collector
from .server import lan_address, make_server
from .store import Store

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "orbit.db"


def _stop(*_):
    raise KeyboardInterrupt


def single_instance(db, wait):
    """Two copies writing one history would count every device's usage twice, so only one may run.
    The background service waits for its turn; a copy started by hand stops with a message."""
    lock = open(Path(db).with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not wait:
            print("Orbit is already running with this history, probably in the background.\n"
                  "Open http://localhost:8787, or stop the background copy first:  scripts/service.sh stop")
            sys.exit(1)
        print("Another copy of Orbit is running. Waiting for it to stop…", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
    return lock  # held until this process exits


def keep_awake():
    """On a Mac: no sleeping while plugged in, for as long as Orbit runs. On battery it sleeps as usual."""
    caffeinate = shutil.which("caffeinate")
    if caffeinate:
        subprocess.Popen([caffeinate, "-s", "-w", str(os.getpid())])  # ends by itself when Orbit does
        print("Keeping this Mac awake while it's plugged in (closing the lid still makes it sleep)")


def main():
    p = argparse.ArgumentParser(prog="python -m orbit", description="Orbit, a home network dashboard for Starlink.")
    p.add_argument("--port", type=int, default=8787, help="port for the dashboard (default 8787)")
    p.add_argument("--lan", action="store_true",
                   help="let phones and laptops on your Wi-Fi open the dashboard; they sign in with an access code")
    p.add_argument("--new-code", action="store_true",
                   help="make a new access code for --lan, which signs every other device out")
    p.add_argument("--db", default=str(DEFAULT_DB), help="where to keep history (default data/orbit.db)")
    p.add_argument("--dish", default=starlink.DISH, help=f"dish address (default {starlink.DISH})")
    p.add_argument("--router", default=starlink.ROUTER, help=f"router address (default {starlink.ROUTER})")
    p.add_argument("--routers", help="settings for a MikroTik router or Grandstream access points, used instead of the "
                                     "Starlink router for the device list (default: routers.json next to the history)")
    p.add_argument("--keep-awake", action="store_true", help="on a Mac, don't sleep while plugged in")
    p.add_argument("--wait", action="store_true", help="if another copy is running, wait for it instead of quitting")
    args = p.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    lock = single_instance(args.db, args.wait)  # noqa: F841 (kept open on purpose)
    if args.keep_awake:
        keep_awake()
    store = Store(args.db)
    auth = Auth(store, required=args.lan)
    if args.new_code:
        auth.reset_code()
        print("Made a new access code. Every other device has to sign in again.")
    routers_path = args.routers or str(Path(args.db).parent / "routers.json")
    try:
        sources = routers.load_config(routers_path)
    except (ValueError, KeyError) as e:
        print(f"Couldn't read {routers_path}: {e}")
        sys.exit(1)
    if sources:
        print("Devices come from: " + ", ".join(f"{s.kind} at {s.host}" for s in sources.sources))
    collector = Collector(store, args.dish, args.router, sources)
    httpd = make_server(collector, auth, args.port, args.lan)
    collector.start()

    print(f"Orbit {__version__} is running at http://localhost:{args.port}")
    if args.lan:
        ip = lan_address()
        print(f"On your Wi-Fi, open http://{ip or '<this computer’s IP>'}:{args.port}")
        print(f"Access code for other devices: {auth.code}  (this computer doesn't need it)")
    how_to_stop = "stop it with: scripts/service.sh stop" if args.wait else "press Ctrl+C to stop"  # --wait: the background service
    print("History is saved in", args.db, "·", how_to_stop, flush=True)

    signal.signal(signal.SIGTERM, _stop)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.stopping = True
        httpd.server_close()
        collector.stop()
        store.close()
        print("\nOrbit stopped. History saved.")


if __name__ == "__main__":
    main()
