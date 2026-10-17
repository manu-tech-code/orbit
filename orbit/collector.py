"""Polls the dish and router, keeps the live picture in memory, and saves history to the store.

Three loops run in background threads:
  dish    every second: status (speed, latency, outages, alerts)
  router  every 2 seconds: the device list with per-device byte counters
  slow    history and dropouts every minute; obstruction map and router info every 10 minutes
"""

import ipaddress
import json
import math
import os
import random
import re
import statistics
import threading
import time
import traceback
from collections import deque
from datetime import date, timedelta

import grpc

from . import __version__, starlink, tripwire as tripwire_mod
from .starlink import Endpoint, gps_ns_to_unix

# Devices are keyed by the router's client_id. The router hides the second half of every
# MAC address ("3A:51:DE:XX:XX:XX"), so MACs can't tell two devices from the same maker apart.
ID_RE = re.compile(r"^[0-9]{1,20}$")
IFACE_BAND = {"RF_2GHZ": "2.4", "RF_5GHZ": "5", "RF_5GHZ_HIGH": "5", "RF_6GHZ": "6", "ETH": "eth"}
TYPES = {"phone", "laptop", "desktop", "tablet", "watch", "tv", "console", "speaker", "camera", "thermostat",
         "lights", "vacuum", "plug", "ereader", "printer", "unknown"}
# Data allowances: Orbit warns when a device or person has used 80% and 100% of theirs. It can't cut anyone off.
PERIODS = {"day": ("a day", "daily", "today"), "month": ("a month", "monthly", "this month")}
ALLOWANCE_LEVELS = (100, 80)
MIN_ALLOWANCE, MAX_ALLOWANCE = 1e6, 1e14  # 1 MB to 100 TB

OUTAGE_TEXT = {
    "BOOTING": "Starlink was starting up",
    "STOWED": "The dish was stowed",
    "THERMAL_SHUTDOWN": "The dish got too hot and paused",
    "NO_SCHEDULE": "No satellite time was available",
    "NO_SATS": "No satellites were in view",
    "OBSTRUCTED": "Something blocked the dish's view",
    "NO_DOWNLINK": "The signal from the satellite was lost",
    "NO_PINGS": "The connection briefly stopped responding",
    "ACTUATOR_ACTIVITY": "The dish was moving",
    "CABLE_TEST": "The cable was being tested",
    "SLEEPING": "The sleep schedule turned it off",
    "UNKNOWN": "Unknown cause",
}
# Dish alert -> (plain text, tone). "info" alerts are shown but don't go in the activity feed.
ALERT_TEXT = {
    "motors_stuck": ("The dish's motors are stuck", "warn"),
    "thermal_throttle": ("The dish is hot, so it's slowing down to cool off", "warn"),
    "thermal_shutdown": ("The dish got too hot and shut down", "crit"),
    "mast_not_near_vertical": ("The dish's mast isn't upright", "warn"),
    "unexpected_location": ("The dish is somewhere your plan doesn't cover", "warn"),
    "slow_ethernet_speeds": ("The cable to the dish is running slowly", "warn"),
    "slow_ethernet_speeds_100": ("The cable to the dish is limited to 100 Mbps", "warn"),
    "install_pending": ("A software update installs soon, which restarts Starlink", "info"),
    "is_heating": ("The dish is heating itself to melt snow", "info"),
    "power_supply_thermal_throttle": ("The power supply is hot and slowing down", "warn"),
    "is_power_save_idle": ("Starlink is asleep (sleep schedule)", "info"),
    "dbf_telem_stale": ("The dish's internal readings are out of date", "warn"),
    "low_motor_current": ("The dish's motors are drawing too little power", "warn"),
    "lower_signal_than_predicted": ("The signal is weaker than expected", "warn"),
    "obstruction_map_reset": ("The dish is relearning what blocks its view", "info"),
    "dish_water_detected": ("Water was detected in the dish", "crit"),
    "router_water_detected": ("Water was detected in the router", "crit"),
    "upsu_router_port_slow": ("The router port on the power supply is slow", "warn"),
    "no_ethernet_link": ("There's no cable connection between the dish and router", "crit"),
}
SPEEDTEST_TEXT = {
    "SPEEDTEST_ERROR_TOKEN": "Starlink couldn't authorize the test. Try again in a minute.",
    "SPEEDTEST_ERROR_OFFLINE": "Starlink was offline, so the test couldn't run.",
    "SPEEDTEST_ERROR_NO_RESULT": "The test finished without a result. Try again.",
    "refused": "The router didn't allow the test.",
    "timeout": "The test didn't finish within 90 seconds.",
}
TYPE_RULES = [  # first match wins, so the specific ones come first
    ("watch", r"watch"),
    ("tablet", r"ipad|\btab\b|tablet|galaxy-?tab|kindle-?fire|fire-?hd"),
    ("ereader", r"kindle|kobo|e-?reader"),
    ("tv", r"\btv\b|roku|chromecast|fire-?tv|firestick|bravia|webos|tizen|androidtv|shield"),
    ("console", r"playstation|\bps[345]\b|xbox|nintendo|\bswitch\b|\bconsole\b"),
    ("speaker", r"echo|alexa|homepod|nest-?(mini|audio|hub)|google-?home|sonos|speaker"),
    ("camera", r"\bcam\b|camera|\bring\b|doorbell|wyze|arlo|eufy|blink"),
    ("printer", r"printer|epson|canon|brother|officejet|laserjet|deskjet"),
    ("laptop", r"macbook|laptop|notebook|thinkpad|chromebook|surface|ideapad|zenbook|vivobook|inspiron|elitebook"),
    ("desktop", r"imac|mac-?mini|mac-?studio|desktop|\bpc\b|optiplex|workstation"),
    ("phone", r"iphone|android|galaxy|samsung|pixel|redmi|xiaomi|oneplus|huawei|tecno|infinix|itel|nokia|oppo|"
              r"vivo|realme|\bmoto|\bsm-|\b[ams]\d{2}[a-z]?\b"),
]


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def g(d, path):
    for p in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def ms(t):
    return int(t * 1000) if t else None


def local_day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def day_start(ts):
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def start_of_day(day):
    return int(time.mktime(time.strptime(day, "%Y-%m-%d")))


def fmt_secs(s):
    s = round(s)
    if s < 60:
        return f"{s} second{'' if s == 1 else 's'}"
    if s < 3600:
        m = round(s / 60)
        return f"{m} minute{'' if m == 1 else 's'}"
    h, m = divmod(round(s / 60), 60)
    return f"{h} h {m} min" if m else f"{h} h"


def guess_type(*names):
    text = " ".join(n for n in names if n).lower()
    for kind, pattern in TYPE_RULES:
        if text and re.search(pattern, text):
            return kind
    return "unknown"


def display_name(d):
    return (d.get("custom_name") or d.get("router_name") or d.get("hostname")
            or f"Unnamed device · {str(d['id'])[-4:]}")


def clean_text(v, limit):
    s = re.sub(r"[\x00-\x1f\x7f]", "", str(v or ""))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def clean_note(v, limit=500):
    """Like clean_text, but keeps line breaks (at most one blank line in a row)."""
    lines = [clean_text(line, limit) for line in str(v or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:limit]


def fmt_bytes(b):
    """Data amounts, written like the web page does."""
    mb = (b or 0) / 1e6
    if mb <= 0:
        return "0 MB"
    if mb < 1:
        return "<1 MB"
    if mb < 1000:
        return f"{round(mb)} MB"
    gb = mb / 1000
    if gb < 1000:
        return f"{gb:.1f} GB" if gb < 100 else f"{round(gb)} GB"
    return f"{gb / 1000:.2f} TB"


def fmt_limit(b):
    """An allowance you chose, like "2 GB" rather than "2.0 GB"."""
    return re.sub(r"\.0+ ", " ", fmt_bytes(b))


def period_key(period, ts):
    """Which allowance period ts falls in: the local day, or the month."""
    day = local_day(ts)
    return day if period == "day" else day[:7]


def allowance_level(used, limit):
    return next((lv for lv in ALLOWANCE_LEVELS if limit and used >= limit * lv / 100), 0)


def private_mac(mac):
    """Phones make up a random MAC address per network for privacy; those have this bit set."""
    try:
        return bool(int(str(mac)[:2], 16) & 2)
    except ValueError:
        return False


def typical(samples):
    """The usual speed during a test: the median, skipping the first readings while it ramps up."""
    v = [x for x in samples if x]
    if len(v) > 6:
        v = v[2:]
    return statistics.median(v) if v else None


# How often Orbit asks the router for its full status (settings, link health). Every 30 seconds was tried and
# the router started restarting itself intermittently, so it is back to the old every-10-minutes.
ROUTER_STATUS_EVERY = int(os.environ.get("ORBIT_ROUTER_STATUS_EVERY", "600"))


class Collector:
    def __init__(self, store, dish=starlink.DISH, router=starlink.ROUTER, sources=None):
        self.store = store
        # Another router or Wi-Fi controller (orbit/routers.py) supplies the device list instead of the Starlink
        # router, for homes where the Starlink router is in bypass mode. The dish is still read as usual.
        self.sources = sources
        # The fast loops and the slow loop each get their own connections, so a big history
        # download never delays the once-a-second reading.
        self.dish = Endpoint(dish)
        self.router = Endpoint(router, borrow=self.dish)
        self.dish_slow = Endpoint(dish)
        self.router_slow = Endpoint(router, borrow=self.dish_slow)
        self.router_test = Endpoint(router, borrow=self.dish_slow)  # speed tests poll once a second on their own line
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.threads = []
        self.started = time.time()

        self.dish_live = {"reachable": None, "fails": 0, "online": None}
        self.dish_info = {}
        self.power_w = None
        self.obstruction = None
        self.live = deque(maxlen=900)  # (ts, down_bps, up_bps, latency_ms, drop_rate, online), last 15 minutes
        self.pending = []
        self.sample_minute = None
        self.last_sample = None
        self.fastest_bps = 0.0
        self.alerts = None
        self.software = store.meta("dish_software")
        self.backfilled = False
        self.gap_before = None  # first reading after a gap (the Mac slept): fill in before it from the dish
        # Orbit's dropout records start at the dish's boot before Orbit first ran: its history covers since boot.
        known = store.meta("known_since")
        self.known_since = int(known) if known else None

        self.router_live = {"reachable": None, "fails": 0}
        self.router_info = {}
        self.networks = {}
        self.router_settings, self.router_link, self.router_update = None, None, None
        self.dish_check = None
        self.schedules = {}  # client_id -> number of block-schedule ranges set in the Starlink app
        self.probes = deque(maxlen=5000)  # (ts, ip, device id, name, port) knocks on the tripwire, last 24 hours
        self.probe_alerts = {}  # ip -> {"told": ts of the last 'tried to connect' event, "scan": ts of the last 'scanning' event}
        self.host_ip = None
        self.tripwire_on = store.meta("tripwire") != "0" and os.environ.get("ORBIT_TRIPWIRE") != "0"
        decoys = tripwire_mod.DECOYS
        if os.environ.get("ORBIT_TRIPWIRE_PORTS"):  # development: other ports, so a second copy never fights the real one
            decoys = [(int(p), tripwire_mod.label_for(int(p))) for p in os.environ["ORBIT_TRIPWIRE_PORTS"].split(",") if p]
        self.tripwire = tripwire_mod.Tripwire(self._on_probe, decoys)
        self.speedtest = {"running": False}
        self.tests = [self._test_out(r) for r in store.speedtests(10)]
        self.fake_speedtest = os.environ.get("ORBIT_FAKE_SPEEDTEST") == "1"  # development: simulate, use no data
        self.devices = {}   # device id (its first client_id) -> device dict (saved fields plus live ones)
        self.aliases = {}   # client_id -> device id, for the other client_ids of a device you merged
        self.alias_rows = {}  # client_id -> that connection's saved details: names, and counters when Orbit last saw it
        self.counters = {}  # client_id -> (rx_bytes, tx_bytes, associated_s, poll_ts)
        self.recent = {}    # client_id -> deque of (poll_ts, down_bytes, up_bytes) for the live speed
        self.acc = {}       # device id -> [down, up, max_down_mbps, max_up_mbps, signal_sum, signal_n] this minute
        self.acc_minute = None
        # Held while a minute of device usage is written, so a merge never leaves some of it on the old entry.
        self.flush_lock = threading.Lock()
        self._month_cache = (None, {})  # ((day, 5-minute slot), {device id: bytes this month before today})
        self._allowances_checked = 0.0
        init = store.meta("initialized")
        self.initialized = init is not None
        self.watching_since = int(init) if init else None

        now = time.time()
        self.day = local_day(now)
        self.home_today = [0.0, 0.0]
        self.dev_today = {}
        self.events = deque(maxlen=40)
        self._outage_cache = (0.0, [])
        self._people_cache = None
        self._state_cache = (0.0, b"")
        self._load(now)

    # ---------- lifecycle ----------
    def _load(self, now):
        for key, row in self.store.devices().items():
            d = dict(row)
            d.update(online=False, ip=row.get("last_ip"), iface=row.get("last_iface"), domain=row.get("last_domain"),
                     type_guess=guess_type(row.get("router_name"), row.get("hostname")))
            self.devices[key] = d
        for cid, row in self.store.aliases().items():
            if row["device"] in self.devices:
                self.aliases[cid] = row["device"]
                self.alias_rows[cid] = dict(row)
        self.home_today = list(self.store.home_bytes(day_start(now), now))
        self.dev_today = {m: list(v) for m, v in self.store.device_day_totals(self.day).items()}
        for r in self.store.probes_since(now - 86400):
            self.probes.append((r["ts"], r["ip"], r["device"], r["name"], r["port"], bool(r.get("known"))))
        for e in reversed(self.store.events(40)):
            self.events.appendleft(self._event_out(e))
        self.fastest_bps = self.store.fastest_down(now - 7 * 86400)
        for r in self.store.samples_since(int(now) - 900):
            self.live.append((r["ts"], r["down_bps"], r["up_bps"], r["latency_ms"], r["drop_rate"], r["online"]))
        if self.live:
            self.last_sample = self.live[-1][0]
            self.sample_minute = self.last_sample // 60 * 60

    def start(self):
        for fn in (self._dish_loop, self._router_loop, self._slow_loop):
            t = threading.Thread(target=self._guard(fn), name=fn.__name__, daemon=True)
            t.start()
            self.threads.append(t)
        if self.tripwire_on:
            self.tripwire.start()

    def _guard(self, fn):
        def run():
            while not self.stopping.is_set():
                try:
                    fn()
                    return
                except Exception:
                    traceback.print_exc()
                    self.stopping.wait(5)
        return run

    def stop(self):
        self.stopping.set()
        self.tripwire.stop()
        for t in self.threads:
            t.join(timeout=8)
        self._flush_samples()
        if self.sample_minute is not None:
            self.store.rollup_minutes(self.sample_minute, self.sample_minute + 60)
        self._flush_devices()
        for e in (self.dish, self.router, self.dish_slow, self.router_slow):
            e.close()

    # ---------- activity ----------
    @staticmethod
    def _event_out(e):
        return {"id": e["id"], "ts": e["ts"] * 1000, "cat": e["cat"], "icon": e["icon"], "title": e["title"],
                "detail": e.get("detail"), "device": e.get("device"), "tone": e.get("tone")}

    def event(self, cat, icon, title, detail=None, device=None, tone=None, ts=None):
        ts = int(ts or time.time())
        eid = self.store.add_event(ts, cat, icon, title, detail, device, tone)
        with self.lock:
            self.events.append(self._event_out({"id": eid, "ts": ts, "cat": cat, "icon": icon, "title": title,
                                                "detail": detail, "device": device, "tone": tone}))
            self.events = deque(sorted(self.events, key=lambda x: (-x["ts"], -x["id"]))[:40], maxlen=40)

    # ---------- dish ----------
    def _dish_loop(self):
        while not self.stopping.is_set():
            t = time.time()
            try:
                sample = self._on_dish_status(self.dish.read("get_status"), t)
            except Exception:
                sample = self._on_dish_error(t)
            self._add_sample(sample)
            self.stopping.wait(1.02 - time.time() % 1)

    def _on_dish_status(self, st, t):
        outage = st.get("outage") or None
        online = outage is None
        lat = num(st.get("pop_ping_latency_ms")) if online else None
        drop = num(st.get("pop_ping_drop_rate"))
        down = num(st.get("downlink_throughput_bps")) or 0.0
        up = num(st.get("uplink_throughput_bps")) or 0.0
        info, obs, cfg = st.get("device_info") or {}, st.get("obstruction_stats") or {}, st.get("config") or {}
        al, gps = st.get("alignment_stats") or {}, st.get("gps_stats") or {}
        alerts = sorted(k for k, v in (st.get("alerts") or {}).items() if v is True)
        out = None
        if outage:
            since = num(outage.get("start_timestamp_ns"))
            out = {"cause": outage.get("cause") or "UNKNOWN", "since": ms(gps_ns_to_unix(since)) if since else None}
        with self.lock:
            back = self.dish_live.get("reachable") is False
            self.dish_live = {
                "reachable": True, "fails": 0, "online": online, "outage": out, "latency": lat, "drop": drop,
                "down": down, "up": up, "uptime": num(g(st, "device_state.uptime_s")),
                "obstructed": num(obs.get("fraction_obstructed")) or 0.0,
                "obstructed_now": bool(obs.get("currently_obstructed")), "obstruction_valid_s": num(obs.get("valid_s")),
                "alerts": alerts,
            }
            self.dish_info = {
                "hardware": info.get("hardware_version"), "software": info.get("software_version"),
                "actuated": st.get("has_actuators") == "HAS_ACTUATORS_YES", "eth_mbps": num(st.get("eth_speed_mbps")),
                "update_state": st.get("software_update_state"), "service": st.get("class_of_service"),
                "dl_limit": st.get("dl_bandwidth_restricted_reason"), "ul_limit": st.get("ul_bandwidth_restricted_reason"),
                "snow_melt": cfg.get("snow_melt_mode"), "update_hour": num(cfg.get("swupdate_reboot_hour")),
                "update_ready": bool(st.get("swupdate_reboot_ready")), "update_wait_s": num(st.get("seconds_until_swupdate_reboot_possible")),
                "deferral": bool(cfg.get("swupdate_three_day_deferral_enabled")),
                "reboot_reason": (st.get("reboot_reason") or "").replace("REBOOT_REASON_", "") or None,
                "pointing": {"az": num(al.get("boresight_azimuth_deg")), "el": num(al.get("boresight_elevation_deg")),
                             "want_az": num(al.get("desired_boresight_azimuth_deg")), "want_el": num(al.get("desired_boresight_elevation_deg")),
                             "tilt": num(al.get("tilt_angle_deg")), "uncertainty": num(al.get("attitude_uncertainty_deg")),
                             "settled": al.get("attitude_estimation_state") == "FILTER_CONVERGED"},
                "gps": {"valid": bool(gps.get("gps_valid")), "sats": int(num(gps.get("gps_sats")) or 0)},
                "sleep": {"on": bool(cfg.get("power_save_mode")), "start_min": num(cfg.get("power_save_start_minutes")),
                          "duration_min": num(cfg.get("power_save_duration_minutes"))},
            }
        if back:
            self.event("network", "satellite-dish", "Orbit can reach your dish again", tone="good")
        self._check_software(info.get("software_version"))
        self._check_alerts(alerts)
        return (int(t), down, up, lat, drop, 1 if online else 0)

    def _on_dish_error(self, t):
        with self.lock:
            fails = self.dish_live["fails"] = self.dish_live.get("fails", 0) + 1
            lost = fails == 3 and self.dish_live.get("reachable") is not False
            if fails >= 3:
                self.dish_live.update(reachable=False, online=None, latency=None, down=0.0, up=0.0, outage=None)
        if lost:
            self.event("network", "satellite-dish", "Orbit can't reach your dish",
                       "It may be restarting, or a cable may be unplugged.", tone="warn")
        return (int(t), 0.0, 0.0, None, None, None)

    def _check_software(self, version):
        if version and version != self.software:
            old, self.software = self.software, version
            self.store.set_meta("dish_software", version)
            if old:
                self.event("network", "rotate-cw", "Starlink software updated", f"Now on version {version}.")

    def _check_alerts(self, alerts):
        now_on = set(alerts)
        if self.alerts is not None:
            for key in sorted(now_on - self.alerts):
                text, tone = ALERT_TEXT.get(key, (key.replace("_", " ").capitalize(), "warn"))
                if tone != "info":
                    self.event("network", "triangle-alert", text, "Reported by the dish.", tone=tone)
        self.alerts = now_on

    def _add_sample(self, sample):
        ts = sample[0]
        self._check_day(ts)
        with self.lock:
            last = self.last_sample
            if last is not None and ts <= last:
                return
            # If the loop fell behind by a few seconds, repeat this reading so usage isn't undercounted.
            rows = [(s,) + sample[1:] for s in range(last + 1, ts)] if last is not None and ts - last <= 10 else []
            if last is not None and ts - last > 10:  # a gap, usually the Mac sleeping: backfill on the next history pull
                self.gap_before, self.backfilled = ts, False
            rows.append(sample)
            for r in rows:
                self.live.append(r)
                self.pending.append(r)
                self.home_today[0] += r[1] / 8
                self.home_today[1] += r[2] / 8
                self.fastest_bps = max(self.fastest_bps, r[1])
            self.last_sample = ts
            minute = ts // 60 * 60
            new_minute = self.sample_minute is not None and minute != self.sample_minute
            prev_minute, self.sample_minute = self.sample_minute, minute
        if new_minute or len(self.pending) >= 10:
            self._flush_samples()
        if new_minute:
            self.store.rollup_minutes(prev_minute, minute)

    def _flush_samples(self):
        with self.lock:
            rows, self.pending = self.pending, []
        self.store.add_samples(rows)

    def _check_day(self, ts):
        day = local_day(ts)
        with self.lock:
            if day != self.day:
                self.day = day
                self.home_today = [0.0, 0.0]
                self.dev_today = {}

    # ---------- router ----------
    def _router_loop(self):
        while not self.stopping.is_set():
            t = time.time()
            try:
                clients = self.sources.poll(t) if self.sources else self.router.read("wifi_get_clients").get("clients") or []
                self._on_clients(clients, t)
            except Exception:
                self._on_router_error()
            if t - self._allowances_checked >= 10:
                self._allowances_checked = t
                try:
                    self._check_allowances(t)
                except Exception:
                    traceback.print_exc()
            self.stopping.wait(max(0.2, 2 - (time.time() - t)))

    def _on_clients(self, clients, t):
        self._check_day(int(t))
        minute = int(t) // 60 * 60
        if self.acc_minute is not None and minute != self.acc_minute:
            self._flush_devices()
        self.acc_minute = minute
        first_look = not self.initialized
        seen, joined, came, went, flipped = set(), [], [], [], []
        with self.lock:
            back = self.router_live.get("reachable") is False
            self.router_live.update(reachable=True, fails=0, updated=t)
            # A merged device can have more than one client_id. Its own comes first, so its live details win
            # if both are connected at once (a laptop on Wi-Fi and a cable).
            clients = sorted(clients, key=lambda c: str(c.get("client_id") or "") in self.aliases)
            for c in clients:
                if c.get("role") == "CONTROLLER":  # the router itself
                    continue
                cid = str(c.get("client_id") or "")
                if not ID_RE.match(cid) or cid == "0":
                    continue
                key = self.aliases.get(cid, cid)
                again = key in seen  # a second connection of a device already counted in this poll
                seen.add(key)
                mac = str(c.get("mac_address") or "").upper()
                given, host = clean_text(c.get("given_name"), 60), clean_text(c.get("name"), 60)
                d = self.devices.get(key)
                if d is None:
                    d = self._new_device(key, mac, t, given, host, acknowledged=first_look)
                    if not first_look:
                        joined.append(d)
                iface = c.get("iface") or None
                wired = iface == "ETH"
                rx, tx = num(g(c, "rx_stats.bytes")) or 0.0, num(g(c, "tx_stats.bytes")) or 0.0
                assoc = num(c.get("associated_time_s")) or 0.0
                prev = self.counters.get(cid)
                down, up = self._deltas(cid, d if cid == key else self.alias_rows.get(cid) or {}, rx, tx, assoc, t)
                # A normal poll, not the first one after Orbit restarted or the device reconnected
                steady = prev is not None and rx >= prev[0] and tx >= prev[1] and assoc + 5 >= prev[2]
                down_mbps, up_mbps = self._speed(cid, down if steady else 0.0, up if steady else 0.0, t, fresh=not steady)
                signal = None if wired else num(c.get("signal_strength"))
                a = self.acc.setdefault(key, [0.0, 0.0, 0.0, 0.0, 0.0, 0])
                a[0] += down
                a[1] += up
                a[2], a[3] = max(a[2], down_mbps), max(a[3], up_mbps)
                if signal is not None and not again:
                    a[4] += signal
                    a[5] += 1
                today = self.dev_today.setdefault(key, [0.0, 0.0])
                today[0] += down
                today[1] += up
                if cid != key:  # what the merged connection calls itself now, for its details and for saving
                    r = self.alias_rows.setdefault(cid, {"cid": cid, "device": key})
                    r.update(mac=mac or r.get("mac"), router_name=given or None, hostname=host or None, last_seen=int(t))
                if again:  # its live speed adds up; the other details stay those of the first connection
                    d["down_mbps"] = (d.get("down_mbps") or 0.0) + down_mbps
                    d["up_mbps"] = (d.get("up_mbps") or 0.0) + up_mbps
                    continue
                was_online, offline_since = d.get("online"), d.get("offline_since")
                blocked = bool(c.get("blocked"))  # set from the Starlink app: paused, or inside a block schedule
                if was_online and blocked != bool(d.get("blocked")):
                    flipped.append((d, blocked))
                if given != (d.get("router_name") or "") or host != (d.get("hostname") or ""):
                    d["type_guess"] = guess_type(given, host)
                d.update(online=True, last_seen=t, mac=mac or d.get("mac"), router_name=given or None, blocked=blocked,
                         hostname=host or None, ip=c.get("ip_address") or d.get("ip"), iface=iface,
                         domain=c.get("domain") or None, signal=signal, snr=None if wired else num(c.get("snr")),
                         down_mbps=down_mbps, up_mbps=up_mbps, rate_down=num(g(c, "rx_stats.rate_mbps")),
                         rate_up=num(g(c, "tx_stats.rate_mbps")), width=num(c.get("channel_width")), offline_since=None,
                         ap=c.get("ap_name"), wifi_name=c.get("ssid"), source=c.get("counter_source") or c.get("source"),
                         since=(t - assoc) if assoc else (d.get("since") if was_online else t))
                if not was_online and offline_since and t - offline_since > 300 and d not in joined:
                    came.append(d)
            for key, d in self.devices.items():
                if d.get("online") and key not in seen and t - (d.get("last_seen") or 0) > 30:
                    d.update(online=False, offline_since=t, down_mbps=0.0, up_mbps=0.0)
                    if t - (d.get("since") or t) > 300:
                        went.append(d)
        if back:
            self.event("network", "router", "Orbit can reach your router again", tone="good")
        if first_look:
            self.initialized = True
            self.watching_since = int(t)
            self.store.set_meta("initialized", int(t))
            self.event("network", "radar", "Orbit started watching your network",
                       f"Found {len(seen)} device{'' if len(seen) == 1 else 's'} on your Wi-Fi.")
        for d in joined:
            with self.lock:
                same = self.devices.get(self._same_as(d, seen) or "")
            hint = f". It may be {display_name(same)} with a new address." if same else ""
            self.event("security", "shield-alert", "A new device joined your Wi-Fi",
                       f"{display_name(d)} · {self._net_label(d)}{hint}", device=d["id"], tone="warn")
        # Hidden devices still count, but their comings and goings stay out of the activity feed.
        for d in came:
            if not d.get("hidden"):
                self.event("devices", "wifi", f"{display_name(d)} connected", self._net_label(d), device=d["id"])
        for d in went:
            if not d.get("hidden"):
                self.event("devices", "wifi-off", f"{display_name(d)} disconnected", device=d["id"])
        for d, blocked in flipped:
            if d.get("hidden"):
                continue
            if blocked:
                self.event("devices", "ban", f"{display_name(d)} lost internet access", "Blocked from the Starlink app.", device=d["id"])
            else:
                self.event("devices", "play", f"{display_name(d)} has internet access again", device=d["id"])

    def _on_router_error(self):
        with self.lock:
            fails = self.router_live["fails"] = self.router_live.get("fails", 0) + 1
            lost = fails == 3 and self.router_live.get("reachable") is not False
            if fails >= 3:
                self.router_live["reachable"] = False
        if lost:
            self.event("network", "router", "Orbit can't reach your router",
                       "Device details may be out of date until it's back.", tone="warn")

    def _new_device(self, key, mac, t, given, host, acknowledged):
        d = {"id": key, "mac": mac, "first_seen": int(t), "last_seen": t, "router_name": given or None,
             "hostname": host or None, "custom_name": None, "owner": None, "type": None,
             "acknowledged": 1 if acknowledged else 0, "online": False, "type_guess": guess_type(given, host)}
        self.store.add_device(key, mac, t, given or None, host or None, d["acknowledged"])
        self.devices[key] = d
        return d

    SPEED_WINDOW_S = 6

    def _speed(self, key, down, up, t, fresh=False):
        """Live Mbps from the byte counters over the last few seconds. The router's own figure is a
        15-second average, which lags; one poll alone is too jumpy, because video arrives in bursts."""
        q = self.recent.setdefault(key, deque())
        if fresh:
            q.clear()
        q.append((t, down, up))
        while q and t - q[0][0] > self.SPEED_WINDOW_S:
            q.popleft()
        span = max(t - q[0][0], 2.0) if len(q) > 1 else 2.0
        moved_down = sum(x[1] for x in list(q)[1:]) if len(q) > 1 else down
        moved_up = sum(x[2] for x in list(q)[1:]) if len(q) > 1 else up
        return moved_down * 8 / 1e6 / span, moved_up * 8 / 1e6 / span

    def _deltas(self, cid, saved, rx, tx, assoc, t):
        """Bytes downloaded and uploaded since the last poll. The router's counters restart when a device reconnects.
        saved: the counters Orbit saved for this client_id, for the first poll after Orbit starts."""
        prev = self.counters.get(cid)
        self.counters[cid] = (rx, tx, assoc, t)
        if prev is None:
            last_poll, last_rx, last_tx = saved.get("last_poll"), saved.get("last_rx"), saved.get("last_tx")
            if last_poll and last_rx is not None and last_tx is not None:
                gap = t - last_poll
                if assoc >= (saved.get("last_assoc_s") or 0) + gap - 60 and rx >= last_rx and tx >= last_tx:
                    return self._sane(rx - last_rx, tx - last_tx, gap)  # stayed connected while Orbit was off
                if assoc < gap:
                    return self._sane(rx, tx, assoc)  # reconnected while Orbit was off
                return 0.0, 0.0
            return self._sane(rx, tx, assoc) if assoc < 300 else (0.0, 0.0)  # it just joined
        prx, ptx, passoc, pt = prev
        if rx < prx or tx < ptx or assoc + 5 < passoc:  # counters restarted
            return self._sane(rx, tx, max(assoc, 1))
        return self._sane(rx - prx, tx - ptx, t - pt)

    @staticmethod
    def _sane(down, up, seconds):
        cap = 1.25e9 * max(seconds, 1)  # 10 Gbps; anything faster is a bad reading
        return (down if 0 <= down <= cap else 0.0), (up if 0 <= up <= cap else 0.0)

    def _flush_devices(self):
        with self.flush_lock:
            with self.lock:
                minute, acc, self.acc = self.acc_minute, self.acc, {}
                if minute is None:
                    return
                day = local_day(minute)
                minutes = [(key, minute, a[0], a[1], a[2], a[3], (a[4] / a[5]) if a[5] else None)
                           for key, a in acc.items() if a[0] or a[1] or a[5]]
                days = [(key, day, a[0], a[1]) for key, a in acc.items() if a[0] or a[1]]
                counters, alias_counters = [], []
                for cid, (rx, tx, assoc, pt) in self.counters.items():
                    r = self.alias_rows.get(cid) if cid in self.aliases else None
                    if r is not None:
                        alias_counters.append((rx, tx, assoc, pt, int(r.get("last_seen") or pt), r.get("mac"),
                                               r.get("router_name"), r.get("hostname"), cid))
                        continue
                    d = self.devices.get(cid)
                    if d:
                        counters.append((rx, tx, assoc, pt, int(d.get("last_seen") or pt), d.get("mac"), d.get("router_name"),
                                         d.get("hostname"), d.get("ip"), d.get("iface"), d.get("domain"), cid))
            self.store.add_device_minutes(minutes)
            self.store.add_device_days(days)
            self.store.save_counters(counters)
            self.store.save_alias_counters(alias_counters)

    def _net_label(self, d):
        net = self.networks.get(d.get("domain") or "")
        if d.get("iface") == "ETH":
            return "Wired"
        if net:
            return "Guest Wi-Fi" if net.get("guest") else "Main Wi-Fi"
        return "Wi-Fi"

    # ---------- slow loop ----------
    def _slow_loop(self):
        due = {"history": 0.0, "info": 0.0, "router": 0.0, "extras": 0.0, "prune": time.time() + 120}
        while not self.stopping.is_set():
            t = time.time()
            if t >= due["history"]:
                due["history"] = t + (60 if self._history() else 15)
            if t >= due["info"]:
                due["info"] = t + (600 if self._info() else 60)
            # The router is a small computer that also carries your Wi-Fi. Orbit asks it for its full status only
            # every 10 minutes (as it did before the Security and Starlink pages were added).
            if t >= due["router"]:
                due["router"] = t + (ROUTER_STATUS_EVERY if self._router_status() else 60)
            if t >= due["extras"]:
                due["extras"] = t + (600 if self._dish_check_read() else 120)
            if t >= due["prune"]:
                self.store.prune(t)
                with self.lock:
                    self.fastest_bps = max(self.store.fastest_down(t - 7 * 86400),
                                           max((r[1] for r in self.live), default=0.0))
                due["prune"] = t + 3600
            self.stopping.wait(2)

    def _history(self):
        try:
            h = self.dish_slow.read("get_history")
        except Exception:
            return False
        t = time.time()
        cur = int(num(h.get("current")) or 0)

        def recent(key, count):
            a = h.get(key) or []
            k = min(count, len(a), cur)
            return [num(a[(cur - k + i) % len(a)]) for i in range(k)]

        power = [v for v in recent("power_in", 60) if v]
        with self.lock:
            self.power_w = sum(power) / len(power) if power else None
            uptime = self.dish_live.get("uptime")
        if self.known_since is None and uptime:
            self.known_since = int(t - uptime)
            self.store.set_meta("known_since", self.known_since)
        if not self.backfilled:
            self._backfill(h, cur, t)
            self.backfilled = True
        rows = []
        for o in h.get("outages") or []:
            start = num(o.get("start_timestamp_ns"))
            if not start:
                continue
            rows.append((int(gps_ns_to_unix(start) * 1000), (num(o.get("duration_ns")) or 0) / 1e9,
                         o.get("cause") or "UNKNOWN", 1 if o.get("did_switch") else 0))
        for start_ms, dur, cause, _ in self.store.add_outages(rows):
            if dur < 2:  # sub-second blips still count in the stats, but don't fill the activity feed
                continue
            if cause == "BOOTING":
                self.event("network", "rotate-cw", "Starlink restarted", f"Back online after {fmt_secs(dur)}.",
                           ts=start_ms / 1000)
            else:
                self.event("network", "satellite-dish", f"Starlink dropped for {fmt_secs(dur)}",
                           OUTAGE_TEXT.get(cause, cause.replace("_", " ").capitalize()) + ".",
                           tone="warn" if dur >= 10 else None, ts=start_ms / 1000)
        with self.lock:
            self._outage_cache = (0.0, [])
        return True

    def _backfill(self, h, cur, t):
        """Fill in the last 15 minutes from the dish's own history, so charts aren't empty after a restart."""
        down, up = h.get("downlink_throughput_bps") or [], h.get("uplink_throughput_bps") or []
        lat, drop = h.get("pop_ping_latency_ms") or [], h.get("pop_ping_drop_rate") or []
        n = min(len(down), len(up), len(lat), len(drop))
        k = min(n, cur, 900)
        now_s = int(t)
        rows = []
        for i in range(k):  # i = 0 is the newest sample
            idx = (cur - 1 - i) % n
            dr = num(drop[idx])
            online = 0 if dr is not None and dr >= 1 else 1
            rows.append((now_s - 1 - i, num(down[idx]) or 0.0, num(up[idx]) or 0.0,
                         num(lat[idx]) if online else None, dr, online))
        rows.reverse()
        with self.lock:
            before, self.gap_before = self.gap_before or int(self.started), None
            have = {r[0] for r in self.live}
            missing = [r for r in rows if r[0] not in have and r[0] < before]
            if not missing:
                return
            merged = sorted({r[0]: r for r in missing + list(self.live)}.values())
            self.live = deque(merged[-900:], maxlen=900)
            pending, self.pending = self.pending, []
        self.store.add_samples(missing + pending)
        self.store.rollup_minutes(missing[0][0] // 60 * 60, now_s // 60 * 60)
        with self.lock:
            self.home_today = list(self.store.home_bytes(day_start(t), t))

    def _info(self):
        ok = True
        try:
            om = self.dish_slow.read("dish_get_obstruction_map")
            snr = []
            for v in om.get("snr") or []:
                f = num(v)
                snr.append(-1 if f is None or f < 0 else round(f, 2))
            with self.lock:
                self.obstruction = {"rows": int(num(om.get("num_rows")) or 0), "cols": int(num(om.get("num_cols")) or 0),
                                    "snr": snr, "min_elevation_deg": num(om.get("min_elevation_deg")),
                                    "max_theta_deg": num(om.get("max_theta_deg")),
                                    "frame": om.get("map_reference_frame"), "updated": ms(time.time())}
        except Exception:
            ok = False
        return ok

    # ---------- the router's own status: networks, settings, link health ----------

    @staticmethod
    def _security_of(bss):
        """How a network is protected, from the name of the auth block. Never the password itself."""
        for b in bss:
            for k in b:
                if k.startswith("auth_"):
                    return {"auth_open": "open", "auth_wpa2": "wpa2", "auth_wpa3": "wpa3", "auth_wpa": "wpa",
                            "auth_wpa2_wpa3": "wpa2/wpa3"}.get(k, k[5:] or "other")
        return None

    def _router_status(self):
        """Read the router's get_status once. It holds the Wi-Fi passwords in its config section,
        so only the fields below are kept and the rest is dropped right away."""
        try:
            rs = self.router_slow.read("get_status")
        except Exception:
            return False
        try:
            c = rs.get("config") or {}
            nets = {}
            for n in c.get("networks") or []:
                ssids = {b.get("band"): b.get("ssid") for b in n.get("basic_service_sets") or [] if b.get("ssid")}
                bss = n.get("basic_service_sets") or []
                nets[n.get("domain") or ""] = {"guest": bool(n.get("guest")), "isolated": bool(n.get("client_isolation")),
                                               "subnet": n.get("ipv4"), "ssids": ssids,
                                               "open": any("auth_open" in b for b in bss),
                                               "security": self._security_of(bss),
                                               "hidden": any(b.get("hidden") for b in bss),
                                               "enabled": any(not b.get("disable") for b in bss)}
            info = rs.get("device_info") or {}
            router_info = {"hardware": info.get("hardware_version"), "software": info.get("software_version"),
                           "uptime_s": num(g(rs, "device_state.uptime_s")), "bypass": bool(c.get("bypass_mode"))}
            confs = c.get("client_configs")
            if confs is None:
                confs = g(self.router_slow.read("wifi_get_config"), "wifi_config.client_configs") or []
            schedules = {}
            for cc in confs:
                ranges = sum(len(s.get("block_ranges") or []) for s in cc.get("weekly_block_schedules") or [])
                if cc.get("client_id") and ranges:
                    schedules[str(cc["client_id"])] = ranges
            widths = lambda v: None if not v or str(v).endswith("DEFAULT") else str(v).split("_")[-1].replace("MHZ", " MHz")
            servers = [str(s) for s in c.get("nameservers") or []]
            settings = {
                "country": c.get("country_code"), "dns": {"servers": servers, "custom": bool(servers), "secure": bool(c.get("secure_dns"))},
                "bypass": bool(c.get("bypass_mode")), "ap_mode": bool(c.get("ap_mode")), "repeater": bool(c.get("is_repeater")),
                "dfs": bool(c.get("dfs_enabled")), "mesh_onboarding": not c.get("disable_mesh_onboarding"),
                "channels": {"2.4": int(num(c.get("channel_2ghz")) or 0), "5": int(num(c.get("channel_5ghz")) or 0),
                             "5_upper": int(num(c.get("channel_5ghz_high")) or 0)},  # 0 = chosen automatically
                "widths": {"5": widths(c.get("vht_bandwidth")), "5_upper": widths(c.get("vht_bandwidth_5ghz_high"))},
                "auto_speedtests": not c.get("disable_automated_speedtests"), "public_services": bool(c.get("use_public_services")),
            }
            poe = rs.get("poe_stats") or {}
            faults = int(sum(num(v) or 0 for k, v in poe.items() if k.startswith("poe_faults")))
            link = {
                "internet_ms": num(rs.get("ping_latency_ms")), "dish_ms": num(rs.get("dish_ping_latency_ms")),
                "pop_ms": num(rs.get("pop_ping_latency_ms")), "internet_drop_5m": num(rs.get("ping_drop_rate_5m")),
                "dish_drop_5m": num(rs.get("dish_ping_drop_rate_5m")), "pop_drop_5m": num(rs.get("pop_ping_drop_rate_5m")),
                "no_wan_link": bool(rs.get("no_wan_link")), "public_ip_age_s": num(rs.get("secs_since_last_public_ipv4_change")),
                "poe": {"on": poe.get("poe_state") == "POE_STATE_ON", "power_w": num(poe.get("poe_power")), "faults": faults},
            }
            sw = rs.get("software_update_stats") or {}
            update = {"state": sw.get("state"), "running": sw.get("running_version") or info.get("software_version"),
                      "pending": sw.get("version_in_progress") or None, "progress": num(sw.get("software_download_progress"))}
            del rs, confs
            with self.lock:
                before, was = self.networks, (self.router_settings, self.router_link)
                self.networks, self.router_info, self.schedules = nets, router_info, schedules
                self.router_settings, self.router_link, self.router_update = settings, link, update
            self._network_changes(before, nets)
            self._settings_changes(was[0], settings, was[1], link)
            return True
        except Exception:
            traceback.print_exc()
            return False

    def _settings_changes(self, old, new, old_link, link):
        """Tell the user about changes to the router's settings and link. The first reading is not a change."""
        if not old:
            return
        if old["dns"]["servers"] != new["dns"]["servers"]:
            now_s = ", ".join(new["dns"]["servers"]) or "the router's own"
            self.event("security", "shield-alert", "Your router's DNS servers changed",
                       f"Now using {now_s}. DNS decides where website names lead, so unexpected changes can send you to fake sites.", tone="warn")
        if old["dns"]["secure"] != new["dns"]["secure"]:
            self.event("security", "shield-check" if new["dns"]["secure"] else "shield-x",
                       "Secure DNS was turned " + ("on" if new["dns"]["secure"] else "off"), tone="good" if new["dns"]["secure"] else "warn")
        if new["bypass"] and not old["bypass"] and not self.sources:
            self.event("network", "router", "Your router went into bypass mode", "Orbit can't see devices while another router handles your Wi-Fi.", tone="warn")
        if old_link:
            age, was = link.get("public_ip_age_s"), old_link.get("public_ip_age_s")
            if age is not None and was is not None and age + 60 < was:
                self.event("network", "globe", "Your public internet address changed", "Starlink gave your home a new address.")
            if link["poe"]["faults"] > old_link["poe"]["faults"]:
                self.event("network", "plug-zap", "The router's power feed reported a fault", "Check the cable between the dish and router.", tone="warn")
            if link["no_wan_link"] and not old_link["no_wan_link"]:
                self.event("network", "unlink", "The router lost its link to the dish", tone="crit")

    def _dish_check_read(self):
        """The dish's own hardware self-test. It talks to the dish, not the router."""
        ok = True
        try:
            d = self.dish_slow.read("get_diagnostics")
            with self.lock:
                self.dish_check = {"self_test": d.get("hardware_self_test"), "codes": [str(x) for x in d.get("hardware_self_test_codes") or []],
                                   "disablement": d.get("disablement_code"), "stowed": bool(d.get("stowed")),
                                   "updated": ms(time.time())}
        except Exception:
            ok = False
        return ok

    def _network_changes(self, before, now_nets):
        """Tell the user when a Wi-Fi network's protection changes. The first reading after a start is not a change."""
        if not before:
            return
        for dom, n in now_nets.items():
            old = before.get(dom)
            name = next(iter((n.get("ssids") or {}).values()), None) or ("Guest Wi-Fi" if n.get("guest") else "Main Wi-Fi")
            if old is None:
                if n.get("open") and n.get("enabled"):
                    self.event("security", "shield-alert", f"A Wi-Fi network without a password appeared: {name}",
                               "Anyone nearby can join it. Guests are kept apart only while client isolation is on.", tone="warn")
                continue
            if n.get("guest") and old.get("isolated") and not n.get("isolated"):
                self.event("security", "shield-x", f"Client isolation was turned off on {name}",
                           "Devices on this network can now reach each other." + (" It has no password, so strangers can join." if n.get("open") else ""),
                           tone="crit")
            elif n.get("guest") and not old.get("isolated") and n.get("isolated"):
                self.event("security", "shield-check", f"Client isolation is back on for {name}",
                           "Each device on this network is kept apart again.", tone="good")
            if n.get("open") and not old.get("open") and n.get("enabled"):
                self.event("security", "shield-alert", f"{name} no longer has a password", "Anyone nearby can join it.", tone="crit")
            elif old.get("open") and not n.get("open") and n.get("enabled"):
                self.event("security", "lock", f"{name} now has a password", tone="good")

    # ---------- floor plan ----------
    PLAN_MATERIALS = {"drywall", "glass", "brick", "concrete", "metal"}

    def get_plan(self):
        raw = self.store.meta("floorplan")
        return {"plan": json.loads(raw) if raw else None, "rev": int(self.store.meta("floorplan_rev") or 0)}

    def set_plan(self, body):
        """Keep the floor plan the user drew: walls, the router, room names and where each device sits.
        Everything is checked and rebuilt field by field, so only numbers and short words are ever stored."""
        def pt(v, w, h):
            if not (isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in v)):
                raise ValueError("A point needs two numbers")
            return [round(min(max(float(v[0]), 0), w), 3), round(min(max(float(v[1]), 0), h), 3)]
        def num_(v, lo, hi, what):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
                raise ValueError(f"{what} must be a number from {lo} to {hi}")
            return float(v)
        w, h = num_(body.get("w"), 4, 40, "The width"), num_(body.get("h"), 4, 40, "The depth")
        router = body.get("router")
        if not isinstance(router, dict):
            raise ValueError("The plan needs a router position")
        r = pt([router.get("x"), router.get("y")], w, h)
        walls, rooms, devices = [], [], {}
        for i, x in enumerate(body.get("walls") or []):
            if len(walls) >= 300:
                raise ValueError("Too many walls")
            if not isinstance(x, dict) or x.get("mat") not in self.PLAN_MATERIALS:
                raise ValueError("A wall needs a known material")
            walls.append({"id": str(x.get("id") or f"w{i}")[:20], "a": pt(x.get("a"), w, h), "b": pt(x.get("b"), w, h), "mat": x["mat"]})
        for i, x in enumerate(body.get("rooms") or []):
            if len(rooms) >= 40:
                raise ValueError("Too many room names")
            if not isinstance(x, dict) or not isinstance(x.get("name"), str) or not x["name"].strip():
                raise ValueError("A room needs a name")
            p = pt([x.get("x"), x.get("y")], w, h)
            rooms.append({"id": str(x.get("id") or f"r{i}")[:20], "name": x["name"].strip()[:30], "x": p[0], "y": p[1]})
        for did, x in (body.get("devices") or {}).items():
            if len(devices) >= 200:
                raise ValueError("Too many devices")
            if not isinstance(x, dict) or not re.fullmatch(r"[0-9]{1,20}", str(did)):
                raise ValueError("A device needs its id and a position")
            p = pt([x.get("x"), x.get("y")], w, h)
            devices[str(did)] = {"x": p[0], "y": p[1]}
        plan = {"v": 1, "w": w, "h": h, "router": {"x": r[0], "y": r[1]}, "walls": walls, "rooms": rooms, "devices": devices}
        rev = int(time.time() * 1000)
        self.store.set_meta("floorplan", json.dumps(plan, separators=(",", ":")))
        self.store.set_meta("floorplan_rev", rev)
        with self.lock:
            self._state_cache = (0.0, b"")
        return {"ok": True, "rev": rev}

    # ---------- tripwire ----------
    def _network_of(self, ip):
        """(kind, domain) for an address on one of the router's networks; ('outside', None) for anything else."""
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return "outside", None
        for dom, n in self.networks.items():
            try:
                if a in ipaddress.ip_interface(n.get("subnet") or "").network:
                    return ("guest" if n.get("guest") else "main"), dom
            except ValueError:
                continue
        return "outside", None

    def _device_at(self, ip):
        """The device using this address. Call with the lock held."""
        hits = [d for d in self.devices.values() if d.get("ip") == ip]
        hits.sort(key=lambda d: (not d.get("online"), -(d.get("last_seen") or 0)))
        return hits[0] if hits else None

    def _on_probe(self, ts, ip, port):
        """Someone connected to a decoy door on this computer. Runs on the tripwire's thread."""
        with self.lock:
            dev = self._device_at(ip)
            did, name = (dev["id"], display_name(dev)) if dev else (None, None)
            kind, _ = self._network_of(ip)
            # A device you've marked as known, on your main Wi-Fi, is allowed to talk to this Mac: it's listed, but raises no alert.
            known = bool(dev and kind == "main" and dev.get("acknowledged"))
            self.probes.append((ts, ip, did, name, port, known))
            if known:
                self._state_cache = (0.0, b"")
        if known:
            self.store.add_probe(ts, ip, did, name, port, True)
            return
        with self.lock:
            recent = {r[4] for r in self.probes if r[1] == ip and ts - r[0] <= 120 and not r[5]}
            told = self.probe_alerts.setdefault(ip, {})
            who = name or ip
            scan = len(recent) >= 3 and ts - told.get("scan", 0) > 1800
            touch = not scan and ts - max(told.get("told", 0), told.get("scan", 0)) > 600
            if scan:
                told["scan"] = ts
            elif touch:
                told["told"] = ts
            self._state_cache = (0.0, b"")
        self.store.add_probe(ts, ip, did, name, port)
        where = "" if kind != "outside" else " It isn’t on any of your Wi-Fi networks, which should not be possible."
        if scan:
            self.event("security", "radar", f"{who} is scanning this Mac",
                       f"It tried {len(recent)} different doors in 2 minutes, which is what a scanner does.{where}", device=did, tone="crit", ts=ts)
        elif touch:
            self.event("security", "shield-alert", f"{who} tried to connect to this Mac",
                       f"{tripwire_mod.label_for(port, self.tripwire.decoys)} (port {port}). Nothing at home should knock there.{where}",
                       device=did, tone="warn", ts=ts)

    def set_tripwire(self, body):
        on = body.get("on")
        if not isinstance(on, bool):
            raise ValueError("on must be true or false")
        self.store.set_meta("tripwire", "1" if on else "0")
        with self.lock:
            self.tripwire_on = on
            self._state_cache = (0.0, b"")
        (self.tripwire.start if on else self.tripwire.stop)()
        self.event("you", "shield-check" if on else "shield-x", "You turned the tripwire on" if on else "You turned the tripwire off",
                   "Orbit listens for anything knocking on this Mac." if on else "Orbit no longer watches for probes on this Mac.")
        return {"ok": True}

    def _security_out(self, now):
        """Call with the lock held."""
        by = {}
        for ts, ip, did, name, port, known in self.probes:
            if ts < now - 86400:
                continue
            s = by.setdefault(ip, {"ip": ip, "first": ts, "last": ts, "tries": 0, "ports": {}, "device": did, "name": name, "known": known})
            s["known"] = s["known"] and known
            s["last"], s["tries"] = max(s["last"], ts), s["tries"] + 1
            s["ports"][port] = s["ports"].get(port, 0) + 1
            if did:
                s["device"], s["name"] = did, name
        sources = []
        for s in sorted(by.values(), key=lambda x: -x["last"])[:20]:
            kind, dom = self._network_of(s["ip"])
            # Known is judged by who the device is now, so marking a device as known also settles its earlier knocks.
            dev = self.devices.get(s["device"]) if s["device"] else None
            s["known"] = bool(dev and kind == "main" and dev.get("acknowledged"))
            sources.append({"ip": s["ip"], "device": s["device"], "name": s["name"], "network": kind, "domain": dom,
                            "first": ms(s["first"]), "last": ms(s["last"]), "tries": s["tries"], "scan": len(s["ports"]) >= 3 and not s["known"],
                            "known": s["known"],
                            "ports": [{"port": p, "label": tripwire_mod.label_for(p, self.tripwire.decoys), "tries": n}
                                      for p, n in sorted(s["ports"].items(), key=lambda kv: -kv[1])]})
        if self.host_ip is None:
            self.host_ip = next((a for a in tripwire_mod.local_addresses() if a.startswith("192.168.")), "")
        host = self._device_at(self.host_ip) if self.host_ip else None
        lbl = lambda p: {"port": p, "label": tripwire_mod.label_for(p, self.tripwire.decoys)}
        return {"tripwire": {"on": self.tripwire_on, "running": self.tripwire.running, "since": ms(self.tripwire.since),
                             "listening": [lbl(p) for p in self.tripwire.listening], "busy": [lbl(p) for p in self.tripwire.busy],
                             "dropped": self.tripwire.dropped, "host_ip": self.host_ip, "host_device": host["id"] if host else None},
                "probes": sources}

    # ---------- speed tests ----------
    @staticmethod
    def _test_out(r):
        err = r.get("error")
        return {"ts": r["ts"] * 1000, "down": r.get("down_mbps"), "up": r.get("up_mbps"), "down_peak": r.get("down_peak"),
                "up_peak": r.get("up_peak"), "latency": r.get("latency_ms"), "error": err,
                "error_text": SPEEDTEST_TEXT.get(err, "The test failed. Try again in a minute.") if err else None}

    def start_speedtest(self):
        """The router tests download, then upload, for about 30 seconds in all. It uses a few hundred MB."""
        with self.lock:
            if self.speedtest.get("running"):
                raise ValueError("A speed test is already running")
            if self.tests and time.time() - self.tests[0]["ts"] / 1000 < 60:
                raise ValueError("Wait a minute between speed tests")
            if self.dish_live.get("online") is not True and not self.fake_speedtest:
                raise ValueError("Starlink is offline, so there’s nothing to test")
            self.speedtest = {"running": True, "phase": "starting", "started": time.time(), "down": [], "up": [],
                              "latency": self.dish_live.get("latency")}
            self._state_cache = (0.0, b"")
        threading.Thread(target=self._run_speedtest, name="speedtest", daemon=True).start()

    def _run_speedtest(self):
        error = None
        try:
            if self.fake_speedtest:
                self._fake_speedtest()
            else:
                before = g(self.router_test.read("get_speedtest_status"), "status.id")
                self.router_test.act("start_speedtest", duration_s=10, send_telemetry=False)
                t0, ours = time.time(), False
                while True:
                    if self.stopping.is_set() or time.time() - t0 > 90:
                        error = "timeout"
                        break
                    st = self.router_test.read("get_speedtest_status").get("status") or {}
                    ours = ours or st.get("id") != before or bool(st.get("running"))  # not the previous test's leftovers
                    if ours:
                        down = [num(v) for v in g(st, "down.throughputs_mbps") or []]
                        up = [num(v) for v in g(st, "up.throughputs_mbps") or []]
                        with self.lock:
                            self.speedtest.update(down=down, up=up, phase="up" if up else "down" if down else "starting")
                        errs = [e for e in (g(st, "down.err"), g(st, "up.err")) if e and e != "SPEEDTEST_ERROR_NONE"]
                        if errs:
                            error = errs[0]
                            break
                        if not st.get("running") and time.time() - t0 > 3:
                            break
                    self.stopping.wait(1)
        except grpc.RpcError as e:
            error = "refused" if e.code() == grpc.StatusCode.PERMISSION_DENIED else "SPEEDTEST_ERROR_UNKNOWN"
        except Exception:
            traceback.print_exc()
            error = "SPEEDTEST_ERROR_UNKNOWN"
        self._finish_speedtest(error)

    def _fake_speedtest(self):
        """Development only (ORBIT_FAKE_SPEEDTEST=1): pretend to test, without using any data."""
        for phase, base in (("down", 230.0), ("up", 12.0)):
            for _ in range(14):
                if self.stopping.is_set():
                    return
                with self.lock:
                    self.speedtest[phase] = self.speedtest.get(phase, []) + [base * random.uniform(0.4, 1.8) for _ in range(4)]
                    self.speedtest["phase"] = phase
                time.sleep(1)

    def _finish_speedtest(self, error=None):
        with self.lock:
            st = dict(self.speedtest)
        down_s, up_s = st.get("down") or [], st.get("up") or []
        down, up = typical(down_s), typical(up_s)
        if not error and not down:
            error = "SPEEDTEST_ERROR_NO_RESULT"
        ts = int(time.time())
        row = {"ts": ts, "down_mbps": down, "up_mbps": up, "down_peak": max([x for x in down_s if x] or [0]) or None,
               "up_peak": max([x for x in up_s if x] or [0]) or None, "latency_ms": st.get("latency"), "error": error}
        self.store.add_speedtest(ts, row["down_mbps"], row["up_mbps"], row["down_peak"], row["up_peak"], row["latency_ms"], error)
        result = self._test_out(row)
        with self.lock:
            self.speedtest = {"running": False, "result": result}
            self.tests = [result] + self.tests[:9]
            self._state_cache = (0.0, b"")
        if error:
            self.event("network", "gauge", "The speed test didn’t finish", result["error_text"], tone="warn")
        else:
            self.event("network", "gauge", f"Speed test: {round(down)} Mbps down, {round(up or 0)} Mbps up",
                       f"Latency {round(st['latency'])} ms" if st.get("latency") else None)

    def _speedtest_out(self):
        """What the page shows while a test runs: the phase and a smoothed live reading."""
        with self.lock:
            st = dict(self.speedtest)

        def now(v):
            v = [x for x in (v or [])[-8:] if x is not None]  # about the last 2 seconds; the router reports 4 a second
            return sum(v) / len(v) if v else None

        return {"running": bool(st.get("running")), "phase": st.get("phase"), "started": ms(st.get("started")),
                "down_now": now(st.get("down")), "up_now": now(st.get("up")), "down_n": len(st.get("down") or []),
                "up_n": len(st.get("up") or []), "result": st.get("result")}

    # ---------- what the web app sees ----------
    def _outages(self, now):
        with self.lock:
            at, rows = self._outage_cache
            uptime = self.dish_live.get("uptime")
        if now - at < 10:
            return rows
        since = min(now - 86400, now - uptime) if uptime else now - 86400
        rows = self.store.outages_since(int(since * 1000))
        with self.lock:
            self._outage_cache = (now, rows)
        return rows

    def _people(self):
        """People as saved: id, name, color, and their allowance."""
        if self._people_cache is None:
            self._people_cache = self.store.people()
        return self._people_cache

    def _invalidate(self):
        self._people_cache = None
        self._state_cache = (0.0, b"")

    # ---------- allowances ----------
    def _month_saved(self, now):
        """Per device, the data it used this month before today, from the saved daily totals."""
        day = local_day(now)
        slot = (day, int(now // 300))  # refreshed every 5 minutes, so yesterday's last minute is in by then
        with self.lock:
            at, saved = self._month_cache
        if at == slot:
            return saved
        first, last = day[:8] + "01", (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        rows = self.store.device_usage_days(first, last) if last >= first else {}
        saved = {k: v[0] + v[1] for k, v in rows.items()}
        with self.lock:
            self._month_cache = (slot, saved)
        return saved

    def _used(self, keys, period, month):
        """Data the devices in keys used this period, down and up together. Call with the lock held."""
        total = sum(sum(self.dev_today.get(k) or (0.0, 0.0)) for k in keys)
        if period == "month":
            total += sum(month.get(k, 0.0) for k in keys)
        return total

    def _used_now(self, keys, period, now):
        month = self._month_saved(now) if period == "month" else {}
        with self.lock:
            return self._used(keys, period, month)

    def _allowance_out(self, row, keys, month):
        """A device's or person's allowance with what they've used so far, or None. Call with the lock held."""
        limit = row.get("allowance_bytes")
        if not limit:
            return None
        period = row.get("allowance_period") or "day"
        used = self._used(keys, period, month)
        return {"bytes": limit, "period": period, "used": used, "level": allowance_level(used, limit)}

    def _owned(self, pid):
        return [k for k, d in self.devices.items() if d.get("owner") == pid]

    @staticmethod
    def _allowance_fields(value, used_of, now):
        """{"bytes": 2e9, "period": "day"} sets an allowance; None removes it. used_of(period) is what's used so far:
        that much doesn't alert, because you see it as you set the allowance."""
        if value is None:
            return {"allowance_bytes": None, "allowance_period": None, "allowance_alerted": None}
        if not isinstance(value, dict) or value.get("period") not in PERIODS:
            raise ValueError("An allowance needs an amount, and a period: a day or a month")
        b = value.get("bytes")
        if isinstance(b, bool) or not isinstance(b, (int, float)) or not math.isfinite(b) or not MIN_ALLOWANCE <= b <= MAX_ALLOWANCE:
            raise ValueError("Choose an allowance between 1 MB and 100 TB")
        period = value["period"]
        return {"allowance_bytes": float(b), "allowance_period": period,
                "allowance_alerted": f"{period_key(period, now)}|{allowance_level(used_of(period), b)}"}

    def _check_allowances(self, now):
        """Report each device and person that has used 80%, and then all, of their allowance. Once per period."""
        month = self._month_saved(now)
        people = self._people()
        found = []
        with self.lock:
            for key, d in self.devices.items():
                a = self._allowance_out(d, [key], month)
                if a:
                    found.append(("device", key, display_name(d), d, a))
            for p in people:
                a = self._allowance_out(p, self._owned(p["id"]), month)
                if a:
                    found.append(("person", p["id"], p["name"], p, a))
        for kind, ref, name, row, a in found:
            pk = period_key(a["period"], now)
            done_key, _, done = (row.get("allowance_alerted") or "").partition("|")
            if a["level"] <= (int(done) if done_key == pk and done.isdigit() else 0):
                continue
            mark = f"{pk}|{a['level']}"
            with self.lock:
                if (row.get("allowance_bytes"), row.get("allowance_period")) != (a["bytes"], a["period"]):
                    continue  # changed meanwhile
                row["allowance_alerted"] = mark
            if kind == "device":
                self.store.update_device(ref, allowance_alerted=mark)
            else:
                self.store.update_person(ref, allowance_alerted=mark)
                self._people_cache = None
            self._allowance_event(kind, ref, name, a)

    def _allowance_event(self, kind, ref, name, a):
        _, adj, when = PERIODS[a["period"]]
        device = kind == "device"
        their = "its" if device else "their"
        amounts = f"{fmt_bytes(a['used'])} of {fmt_limit(a['bytes'])} {when}."
        if a["level"] >= 100:
            self.event("usage", "bell-ring", f"{name} has used {their} {adj} allowance",
                       amounts + (" To pause it, use the Starlink app." if device else " To pause their devices, use the Starlink app."),
                       device=ref if device else None, tone="warn")
        else:
            pct = min(99, int(a["used"] * 100 / a["bytes"]))
            self.event("usage", "bell-ring", f"{name} has used {pct}% of {their} {adj} allowance", amounts,
                       device=ref if device else None)

    # ---------- merged devices ----------
    def _aliases_of(self, key):
        """[(client_id, saved details)] merged into a device. Call with the lock held."""
        return [(cid, self.alias_rows.get(cid) or {}) for cid, dev in self.aliases.items() if dev == key]

    def _same_as(self, d, seen=None):
        """For a new device: a known one, not connected now, with the same name. Probably the same device with a
        new address: phones make one up for each network, and now and then a new one. Call with the lock held.
        seen: the devices in the router's latest list, when that's fresher than their online flags."""
        if d.get("acknowledged"):
            return None
        names = {n.lower() for n in (d.get("router_name"), d.get("hostname")) if n}
        best = None
        for o in self.devices.values():
            if o is d or not o.get("acknowledged") or (o.get("online") and (seen is None or o["id"] in seen)):
                continue
            theirs = [o] + [r for _, r in self._aliases_of(o["id"])]
            if names & {n.lower() for x in theirs for n in (x.get("router_name"), x.get("hostname")) if n}:
                if best is None or (o.get("last_seen") or 0) > (best.get("last_seen") or 0):
                    best = o
        return best["id"] if best else None

    def _device_out(self, d, month):
        """What the page sees of one device. month: from _month_saved. Call with the lock held."""
        key = d["id"]
        online = bool(d.get("online"))
        today = self.dev_today.get(key) or (0.0, 0.0)
        net = self.networks.get(d.get("domain") or "")
        iface = d.get("iface")
        aliases = self._aliases_of(key)
        return {
            "id": d["id"], "mac": d.get("mac"), "name": display_name(d), "custom_name": d.get("custom_name"),
            "router_name": d.get("router_name"), "hostname": d.get("hostname"), "owner": d.get("owner"),
            "type": d.get("type") or d.get("type_guess") or "unknown", "type_set": bool(d.get("type")),
            "online": online, "since": ms(d.get("since")) if online else None, "first_seen": ms(d.get("first_seen")),
            "last_seen": ms(d.get("last_seen")), "ip": d.get("ip"), "iface": iface, "band": IFACE_BAND.get(iface),
            "domain": d.get("domain"),
            "upper_band": iface == "RF_5GHZ_HIGH",
            "network": ("guest" if net.get("guest") else "main") if net else ("wired" if iface == "ETH" else None),
            "ssid": (net or {}).get("ssids", {}).get(iface) or d.get("wifi_name"), "signal": d.get("signal"), "snr": d.get("snr"),
            "ap": d.get("ap"), "source": d.get("source"),
            "down_mbps": d.get("down_mbps") or 0.0 if online else 0.0, "up_mbps": d.get("up_mbps") or 0.0 if online else 0.0,
            "rate_down": d.get("rate_down"), "rate_up": d.get("rate_up"), "width": d.get("width"),
            "today_down": today[0], "today_up": today[1], "is_new": not d.get("acknowledged"),
            "private_mac": private_mac(d.get("mac")), "blocked": bool(d.get("blocked")) and online,
            "schedules": max(self.schedules.get(c, 0) for c in [key] + [cid for cid, _ in aliases]),
            "notes": d.get("notes"), "hidden": bool(d.get("hidden")), "allowance": self._allowance_out(d, [key], month),
            "aliases": [{"cid": cid, "name": r.get("router_name") or r.get("hostname"), "mac": r.get("mac"),
                         "last_seen": ms(r.get("last_seen"))} for cid, r in aliases],
            "maybe_same": self._same_as(d),
        }

    def _person_out(self, p, month):
        """Call with the lock held."""
        return {"id": p["id"], "name": p["name"], "color": p["color"],
                "allowance": self._allowance_out(p, self._owned(p["id"]), month)}

    @staticmethod
    def _point(r):
        return {"t": r[0] * 1000, "down": r[1] / 1e6, "up": r[2] / 1e6, "lat": r[3], "on": r[5]}

    def state(self, now=None):
        now = now or time.time()
        outages = self._outages(now)
        month, people = self._month_saved(now), self._people()
        with self.lock:
            dl, di, rl, ri = dict(self.dish_live), dict(self.dish_info), dict(self.router_live), dict(self.router_info)
            devices = [self._device_out(d, month) for d in self.devices.values()]
            people = [self._person_out(p, month) for p in people]
            points = [self._point(r) for r in list(self.live)[-5:]]
            events = list(self.events)
            home = {"day": self.day, "down": self.home_today[0], "up": self.home_today[1]}
            nets = [{"domain": k, **v} for k, v in self.networks.items()]
            fastest, power = self.fastest_bps, self.power_w
            security = self._security_out(now)
            plan_rev = int(self.store.meta("floorplan_rev") or 0)
            rsettings, rlink, rupdate = self.router_settings, self.router_link, self.router_update
            dcheck = self.dish_check
        devices.sort(key=lambda x: (not x["online"], -(x["down_mbps"] + x["up_mbps"]), x["name"].lower()))
        uptime = dl.get("uptime")
        boot = now - uptime if uptime else None
        ends = [o["start_ms"] / 1000 + o["duration_s"] for o in outages
                if o["duration_s"] >= 5 and boot and o["start_ms"] / 1000 >= boot - 5]
        online_since = max([boot] + ends) if boot and dl.get("online") else None
        outage = dl.get("outage")
        if outage:
            outage = {**outage, "text": OUTAGE_TEXT.get(outage["cause"], outage["cause"].replace("_", " ").capitalize())}
        return {
            "now": ms(now), "version": __version__, "started": ms(self.started), "known_since": ms(self.known_since),
            "watching_since": ms(self.watching_since),
            "dish": {
                "reachable": dl.get("reachable"), "online": dl.get("online"), "outage": outage,
                "latency_ms": dl.get("latency"), "drop_rate": dl.get("drop"),
                "down_mbps": (dl.get("down") or 0.0) / 1e6, "up_mbps": (dl.get("up") or 0.0) / 1e6,
                "uptime_s": uptime, "online_since": ms(online_since), "obstructed": dl.get("obstructed"),
                "obstructed_now": dl.get("obstructed_now"), "obstruction_valid_s": dl.get("obstruction_valid_s"),
                "alerts": [{"key": k, "text": ALERT_TEXT.get(k, (k.replace("_", " ").capitalize(), "warn"))[0],
                            "tone": ALERT_TEXT.get(k, ("", "warn"))[1]} for k in dl.get("alerts") or []],
                "power_w": power, "fastest_mbps": fastest / 1e6, **di, "check": dcheck,
            },
            "router": {"reachable": rl.get("reachable"), "updated": ms(rl.get("updated")), **ri, "networks": nets,
                       "settings": rsettings, "link": rlink, "update": rupdate},
            "sources": self.sources.out() if self.sources else None,
            "security": security, "plan_rev": plan_rev,
            "home": home, "points": points, "devices": devices, "people": people, "events": events,
            "speedtest": self._speedtest_out(), "tests": self.tests[:5],
            "outages": [{"start": o["start_ms"], "duration_s": o["duration_s"], "cause": o["cause"],
                         "text": OUTAGE_TEXT.get(o["cause"], o["cause"].replace("_", " ").capitalize())}
                        for o in outages if o["start_ms"] >= (now - 86400) * 1000],
        }

    def state_json(self):
        now = time.time()
        with self.lock:
            at, body = self._state_cache
        if now - at < 0.9 and body:
            return body
        body = json.dumps(self.state(now), separators=(",", ":")).encode()
        with self.lock:
            self._state_cache = (now, body)
        return body

    def live_points(self):
        with self.lock:
            return [self._point(r) for r in self.live]

    def _current_minute_bytes(self):
        with self.lock:
            m = self.sample_minute
            rows = [r for r in self.live if m is not None and r[0] >= m]
        return sum(r[1] for r in rows) / 8, sum(r[2] for r in rows) / 8

    def usage(self, rng):
        now = time.time()
        now_s = int(now)
        if rng == "24h":
            start = now_s // 3600 * 3600 - 23 * 3600
            keys = [start + i * 3600 for i in range(24)]
            by = {r["k"]: r for r in self.store.home_buckets(start, now_s + 1, "hour")}
            bars = [{"t": k * 1000, "down": (by.get(k) or {}).get("down") or 0.0, "up": (by.get(k) or {}).get("up") or 0.0}
                    for k in keys]
            bucket = 900
            devs = self.store.device_usage_minutes(start, now_s + 1)
            with self.lock:
                for key, a in self.acc.items():
                    v = devs.setdefault(key, [0.0, 0.0])
                    v[0] += a[0]
                    v[1] += a[1]
        elif rng in ("7d", "30d"):
            n = 7 if rng == "7d" else 30
            today = date.fromtimestamp(now)
            days = [(today - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
            start = start_of_day(days[0])
            by = {r["k"]: r for r in self.store.home_buckets(start, now_s + 1, "day")}
            bars = [{"t": start_of_day(d) * 1000, "day": d, "down": (by.get(d) or {}).get("down") or 0.0,
                     "up": (by.get(d) or {}).get("up") or 0.0} for d in days]
            with self.lock:
                bars[-1]["down"], bars[-1]["up"] = self.home_today
            bucket = 3600 if n == 7 else 6 * 3600
            devs = self.store.device_usage_days(days[0], days[-2]) if n > 1 else {}
            with self.lock:
                for key, (d0, u0) in self.dev_today.items():
                    v = devs.setdefault(key, [0.0, 0.0])
                    v[0] += d0
                    v[1] += u0
        else:
            raise ValueError("Unknown range")
        if rng == "24h":
            extra = self._current_minute_bytes()
            bars[-1]["down"] += extra[0]
            bars[-1]["up"] += extra[1]
        rows = {r["k"]: r for r in self.store.speed_series(start, now_s + 1, bucket)}
        speed = []
        for k in range(start // bucket * bucket, now_s + 1, bucket):
            r = rows.get(k)
            speed.append({"t": k * 1000, "down": r["down"] / 1e6 if r and r["down"] is not None else None,
                          "down_max": r["down_max"] / 1e6 if r and r["down_max"] is not None else None,
                          "up": r["up"] / 1e6 if r and r["up"] is not None else None,
                          "lat": r["lat"] if r else None})
        with self.lock:
            names = {key: (display_name(d), d.get("owner"), d.get("type") or d.get("type_guess"))
                     for key, d in self.devices.items()}
        devices = []
        for key, (d0, u0) in devs.items():
            if d0 + u0 <= 0:
                continue
            name, owner, kind = names.get(key, ("Forgotten device", None, "unknown"))
            devices.append({"id": key, "name": name, "owner": owner, "type": kind, "down": d0, "up": u0})
        devices.sort(key=lambda x: -(x["down"] + x["up"]))
        out = self.store.outage_stats(start * 1000, (now_s + 1) * 1000)
        return {
            "range": rng, "start": start * 1000, "end": ms(now), "bars": bars, "speed": speed, "bucket_s": bucket,
            "total": {"down": sum(b["down"] for b in bars), "up": sum(b["up"] for b in bars)},
            "devices": devices, "outages": {"count": out["n"], "seconds": out["s"]},
            "recorded_s": self.store.recorded_seconds(start, now_s + 1), "range_s": now_s - start,
        }

    def device_usage(self, did):
        key, _ = self._find(did)
        today = date.today()
        days = [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
        saved = self.store.device_days_for(key, days[0], days[-1])
        with self.lock:
            live = list(self.dev_today.get(key) or (0.0, 0.0))
        out = [{"day": d, "t": start_of_day(d) * 1000, "down": (saved.get(d) or (0.0, 0.0))[0],
                "up": (saved.get(d) or (0.0, 0.0))[1]} for d in days]
        out[-1]["down"], out[-1]["up"] = live
        return {"days": out}

    def events_list(self, cat=None, limit=200):
        return [self._event_out(e) for e in self.store.events(max(1, min(int(limit), 500)), cat or None)]

    def obstruction_map(self):
        with self.lock:
            if not self.obstruction:
                raise KeyError("obstruction map")
            return self.obstruction

    # ---------- changes from the web app ----------
    def _find(self, did):
        did = str(did or "")
        if not ID_RE.match(did):
            raise KeyError(did)
        with self.lock:
            d = self.devices.get(did)
        if d is None:
            raise KeyError(did)
        return did, d

    BULK_FIELDS = {"owner", "type", "hidden", "acknowledged"}

    def _device_fields(self, key, body, now, allowed=None):
        """The saved fields a change from the page sets. Raises ValueError for anything it can't accept.
        allowed: the only keys that may appear (besides "id"), for changes to several devices at once."""
        extra = set(body) - (allowed or set(body)) - {"id"}
        if extra:
            raise ValueError(f"Can't change {', '.join(sorted(extra))} for several devices at once")
        fields = {}
        if "name" in body:
            fields["custom_name"] = clean_text(body["name"], 40) or None
        if "owner" in body:
            owner = body["owner"]
            if owner is not None and owner not in {p["id"] for p in self._people()}:
                raise ValueError("That person doesn't exist")
            fields["owner"] = owner
        if "type" in body:
            if body["type"] is not None and body["type"] not in TYPES:
                raise ValueError("Unknown device type")
            fields["type"] = body["type"]
        if "notes" in body:
            fields["notes"] = clean_note(body["notes"]) or None
        if "hidden" in body:
            if not isinstance(body["hidden"], bool):
                raise ValueError("hidden is true or false")
            fields["hidden"] = 1 if body["hidden"] else 0
        if "allowance" in body:
            fields.update(self._allowance_fields(body["allowance"], lambda period: self._used_now([key], period, now), now))
        if fields or body.get("acknowledged") is True:
            fields["acknowledged"] = 1
        if not fields:
            raise ValueError("Nothing to change")
        return fields

    def update_device(self, did, body):
        key, d = self._find(did)
        now = time.time()
        fields = self._device_fields(key, body, now)
        with self.lock:
            before, was_hidden, had_allowance = display_name(d), bool(d.get("hidden")), bool(d.get("allowance_bytes"))
            d.update(fields)
            after = display_name(d)
        self.store.update_device(key, **fields)
        if "custom_name" in fields and after != before:
            self.event("you", "pencil", f"You renamed {before} to {after}", device=key)
        if "owner" in fields:
            person = next((p["name"] for p in self._people() if p["id"] == fields["owner"]), None)
            self.event("you", "tag", f"{after} now belongs to {person}" if person else f"{after} has no owner now", device=key)
        if "hidden" in fields and bool(fields["hidden"]) != was_hidden:
            if fields["hidden"]:
                self.event("you", "eye-off", f"You hid {after}", "Orbit still records what it uses.", device=key)
            else:
                self.event("you", "eye", f"You stopped hiding {after}", device=key)
        if fields.get("allowance_bytes"):
            self.event("you", "bell-ring", f"You gave {after} an allowance of {fmt_limit(fields['allowance_bytes'])} "
                       f"{PERIODS[fields['allowance_period']][0]}", device=key)
        elif "allowance_bytes" in fields and had_allowance:
            self.event("you", "bell-ring", f"You removed {after}'s allowance", device=key)
        self._invalidate()
        month = self._month_saved(now)
        with self.lock:
            return self._device_out(d, month)

    def bulk_update(self, body):
        """The same kind of change to several devices: {"changes": [{"id": "…", "owner": "me"}, …]}.
        Each change can set owner, type, hidden and acknowledged. One activity entry covers them all."""
        changes = body.get("changes")
        if not isinstance(changes, list) or not changes:
            raise ValueError("Pick at least one device")
        if len(changes) > 500:
            raise ValueError("Too many devices at once")
        now = time.time()
        plan = []
        for ch in changes:
            if not isinstance(ch, dict):
                raise ValueError("Send each change as an object")
            key, d = self._find(ch.get("id"))
            plan.append((key, d, self._device_fields(key, ch, now, allowed=self.BULK_FIELDS)))
        self.store.update_devices([(key, fields) for key, _, fields in plan])
        with self.lock:
            for _, d, fields in plan:
                d.update(fields)
            icon, title = self._bulk_words(plan)
        self.event("you", icon, title, device=plan[0][0] if len(plan) == 1 else None)
        self._invalidate()
        return {"ok": True, "changed": len(plan)}

    def _bulk_words(self, plan):
        """(icon, title) for the activity entry of one change to several devices. Call with the lock held."""
        n = len(plan)
        what = display_name(plan[0][1]) if n == 1 else f"{n} devices"
        kinds = set().union(*(f.keys() for _, _, f in plan)) - {"acknowledged"}
        same = {k: len({f.get(k) for _, _, f in plan}) == 1 for k in kinds}
        if kinds == {"owner"} and same["owner"]:
            person = next((p["name"] for p in self._people() if p["id"] == plan[0][2]["owner"]), None)
            if person:
                return "tag", f"{what} now {'belongs' if n == 1 else 'belong'} to {person}"
            return "tag", f"{what} {'has' if n == 1 else 'have'} no owner now"
        if kinds == {"hidden"} and same["hidden"]:
            return ("eye-off", f"You hid {what}") if plan[0][2]["hidden"] else ("eye", f"You stopped hiding {what}")
        if kinds == {"type"}:
            return "tag", f"You changed what kind of device {what} {'is' if n == 1 else 'are'}"
        if not kinds:
            return "shield-check", f"You marked {what} as known"
        return "pencil", f"You changed {what}"

    def forget_device(self, did):
        self.forget_devices({"ids": [did]})

    def forget_devices(self, body):
        """Forget offline devices: {"ids": […]}. Their usage history stays in your totals."""
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids:
            raise ValueError("Pick at least one device")
        keys = list(dict.fromkeys(self._find(i)[0] for i in ids))
        with self.flush_lock:
            with self.lock:
                busy = [self.devices[k] for k in keys if self.devices[k].get("online")]
                if busy:
                    raise ValueError(f"{display_name(busy[0])} is connected right now. Only offline devices can be forgotten.")
                names = []
                for key in keys:
                    names.append(display_name(self.devices.pop(key)))
                    for cid in [key] + [cid for cid, _ in self._aliases_of(key)]:
                        for table in (self.aliases, self.alias_rows, self.counters, self.recent):
                            table.pop(cid, None)
                    self.dev_today.pop(key, None)
                    self.acc.pop(key, None)
            for key in keys:
                self.store.delete_device(key)
        if len(names) == 1:
            self.event("you", "trash-2", f"You forgot {names[0]}", "If it connects again, it shows up as new.")
        else:
            listed = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
            self.event("you", "trash-2", f"You forgot {len(names)} devices", f"{listed}. If they connect again, they show up as new.")
        self._invalidate()
        return {"ok": True, "forgot": len(names)}

    def merge_devices(self, did, body):
        """Two entries that are the same device become one: {"other": "<id>"}. Returns the device as it is now.
        The one connected now, or seen last, keeps its id, so the live readings carry on. Its name, owner and
        other settings come from the entry Orbit knew first, filled in from the other one."""
        a_key, _ = self._find(did)
        b_key, _ = self._find(body.get("other"))
        if a_key == b_key:
            raise ValueError("Pick another device to merge it with")
        with self.flush_lock:
            with self.lock:
                a, b = self.devices.get(a_key), self.devices.get(b_key)
                if a is None or b is None:
                    raise KeyError(a_key if a is None else b_key)
                if a.get("online") and b.get("online"):
                    raise ValueError("Both are connected right now, so they're two different devices")
                a_first = a.get("online") or (not b.get("online") and (a.get("last_seen") or 0) >= (b.get("last_seen") or 0))
                keep, gone = (a, b) if a_first else (b, a)
                base, extra = (a, b) if (a.get("first_seen") or 0) <= (b.get("first_seen") or 0) else (b, a)
                fields = self._merged_fields(keep, base, extra)
                gone_name = display_name(gone)
            self.store.merge_devices(keep["id"], gone["id"], fields)
            with self.lock:
                keep_key, gone_key = keep["id"], gone["id"]
                self.devices.pop(gone_key, None)
                keep.update(fields)
                for cid, _ in self._aliases_of(gone_key):
                    self.aliases[cid] = keep_key
                    self.alias_rows.setdefault(cid, {})["device"] = keep_key
                self.aliases[gone_key] = keep_key
                self.alias_rows[gone_key] = {"cid": gone_key, "device": keep_key, **{k: gone.get(k) for k in (
                    "mac", "router_name", "hostname", "first_seen", "last_seen", "last_rx", "last_tx", "last_assoc_s", "last_poll")}}
                today = self.dev_today.pop(gone_key, None)
                if today:
                    t = self.dev_today.setdefault(keep_key, [0.0, 0.0])
                    t[0], t[1] = t[0] + today[0], t[1] + today[1]
                acc = self.acc.pop(gone_key, None)
                if acc:
                    k = self.acc.setdefault(keep_key, [0.0, 0.0, 0.0, 0.0, 0.0, 0])
                    k[:] = [k[0] + acc[0], k[1] + acc[1], max(k[2], acc[2]), max(k[3], acc[3]), k[4] + acc[4], k[5] + acc[5]]
                self._month_cache = (None, {})
                final = display_name(keep)
        title = f"You merged {gone_name} into {final}" if gone_name != final else f"You merged two entries for {final}"
        self.event("you", "link", title, "They're one device now, and their usage history is combined.", device=keep_key)
        self._invalidate()
        month = self._month_saved(time.time())
        with self.lock:
            return self._device_out(keep, month)

    @staticmethod
    def _merged_fields(keep, base, extra):
        own = lambda d: d.get("router_name") or d.get("hostname")  # noqa: E731 (what it's called without your name)
        name = base.get("custom_name") or extra.get("custom_name")
        if not name and own(base) and own(base) != own(keep):
            name = own(base)  # keep the name you know it by, when the new connection calls itself something else
        allowance = base if base.get("allowance_bytes") else extra
        notes = "\n".join(n for n in (base.get("notes"), extra.get("notes")) if n)[:500]
        return {"custom_name": name, "owner": base.get("owner") or extra.get("owner"),
                "type": base.get("type") or extra.get("type"), "notes": notes or None, "hidden": base.get("hidden") or 0,
                "acknowledged": 1, "first_seen": base.get("first_seen") or extra.get("first_seen"),
                **{k: allowance.get(k) for k in ("allowance_bytes", "allowance_period", "allowance_alerted")}}

    def separate_alias(self, did, cid):
        """A connection merged into a device becomes its own device again. What it used so far stays where it is."""
        key, d = self._find(did)
        cid = str(cid or "")
        now = int(time.time())
        with self.flush_lock:
            with self.lock:
                if self.aliases.get(cid) != key:
                    raise KeyError(cid)
            self.store.separate_alias(cid, now)
            with self.lock:
                self.aliases.pop(cid, None)
                r = self.alias_rows.pop(cid, None) or {}
                new = {k: r.get(k) for k in ("mac", "router_name", "hostname", "last_seen", "last_rx", "last_tx",
                                             "last_assoc_s", "last_poll")}
                new.update(id=cid, first_seen=r.get("first_seen") or now, custom_name=None, owner=None, type=None,
                           acknowledged=1, online=False, hidden=0, type_guess=guess_type(r.get("router_name"), r.get("hostname")))
                self.devices[cid] = new
                name, other = display_name(new), display_name(d)
        self.event("you", "unlink", f"You separated {name} from {other}",
                   f"It's its own device again. What it used so far stays with {other}.", device=cid)
        self._invalidate()

    def add_person(self, body):
        name = clean_text(body.get("name"), 24)
        if not name:
            raise ValueError("A name is needed")
        person = self.store.add_person(name)
        self._invalidate()
        moved = 0
        for did in body.get("devices") or []:
            try:
                key, d = self._find(did)
            except KeyError:
                continue
            with self.lock:
                d.update(owner=person["id"], acknowledged=1)
            self.store.update_device(key, owner=person["id"], acknowledged=1)
            moved += 1
        self.event("you", "user-plus", f"You added {name}", f"{moved} device{'' if moved == 1 else 's'}" if moved else None)
        self._invalidate()
        return person

    def update_person(self, pid, body):
        """Rename a person ({"name": …}), or set their allowance ({"allowance": {bytes, period} or null})."""
        people = {p["id"]: p for p in self._people()}
        if pid not in people:
            raise KeyError(pid)
        p, fields, now = people[pid], {}, time.time()
        if "name" in body:
            fields["name"] = clean_text(body.get("name"), 24)
            if not fields["name"]:
                raise ValueError("A name is needed")
        if "allowance" in body:
            with self.lock:
                keys = self._owned(pid)
            fields.update(self._allowance_fields(body["allowance"], lambda period: self._used_now(keys, period, now), now))
        if not fields:
            raise ValueError("Nothing to change")
        self.store.update_person(pid, **fields)
        name = fields.get("name", p["name"])
        if name != p["name"]:
            self.event("you", "pencil", f"You renamed {p['name']} to {name}")
        if fields.get("allowance_bytes"):
            self.event("you", "bell-ring", f"You gave {name} an allowance of {fmt_limit(fields['allowance_bytes'])} "
                       f"{PERIODS[fields['allowance_period']][0]}", "It counts all of their devices together.")
        elif "allowance_bytes" in fields and p.get("allowance_bytes"):
            self.event("you", "bell-ring", f"You removed {name}'s allowance")
        self._invalidate()

    def delete_person(self, pid):
        people = {p["id"]: p for p in self._people()}
        if pid not in people:
            raise KeyError(pid)
        self.store.delete_person(pid)
        with self.lock:
            for d in self.devices.values():
                if d.get("owner") == pid:
                    d["owner"] = None
        self.event("you", "user", f"You removed {people[pid]['name']}", "Their devices have no owner now.")
        self._invalidate()
