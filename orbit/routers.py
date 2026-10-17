"""Device lists from routers and Wi-Fi controllers other than Starlink's.

Orbit's collector works from the Starlink router's client list (wifi_get_clients). Each source here reads
another brand and returns the same shape, so devices, usage, allowances, alerts and the map work unchanged.
It's for homes where the Starlink router is in bypass mode and another router runs the network.

  MikroTik     RouterOS 7 REST API with a read-only user: DHCP leases and the address table (who's there),
               the Wi-Fi registration table (signal, when the MikroTik runs the Wi-Fi), Kid Control (bytes per device).
  Grandstream  GWN.Cloud or GWN Manager API (App ID and Secret Key): Wi-Fi clients with signal, access point and bytes.

Several sources can run at once, for example MikroTik for every device and its data, and Grandstream for Wi-Fi
signal. Their lists are merged by MAC address. Each device's byte counters always come from one source, so a
counter never jumps between two of them.

Every request here only reads. Settings live in data/routers.json (see load_config); passwords never leave it.
"""

import base64
import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 8


class SourceError(Exception):
    """A plain-words reason a source couldn't be read."""


def client_id_for(mac):
    """A stable numeric client_id from a MAC address, the same in every source (Orbit's IDs are digits)."""
    h = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    return str(int(h, 16)) if len(h) == 12 else None


def norm_mac(mac):
    h = re.sub(r"[^0-9a-fA-F]", "", mac or "").upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _duration_s(v):
    """RouterOS "1w2d3h4m5s" or GWN "1d 02:03:04" / "02:03:04" -> seconds."""
    if v is None:
        return None
    s = str(v).strip()
    total = 0.0
    m = re.match(r"^(?:(\d+)d\s*)?(\d+):(\d{2}):(\d{2})$", s)
    if m:
        d, hh, mm, ss = (int(x or 0) for x in m.groups())
        return d * 86400 + hh * 3600 + mm * 60 + ss
    for n, unit in re.findall(r"(\d+(?:\.\d+)?)(w|d|h|ms|m|s)", s):
        total += float(n) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return total or _num(s)


def _iface_for(band):
    """A band name from any brand ("5ghz-ax", "2.4G", 5) -> the Starlink-style interface Orbit understands."""
    b = str(band or "").lower()
    if b.startswith("6"):
        return "RF_6GHZ"
    if b.startswith("5"):
        return "RF_5GHZ"
    if b.startswith("2"):
        return "RF_2GHZ"
    return None


class _Http:
    def __init__(self, base, insecure_tls):
        self.base = base if re.match(r"^https?://", base) else f"https://{base}"
        self.ctx = ssl.create_default_context()
        if insecure_tls:  # the device's own self-made certificate; only for equipment you manage
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def request(self, method, path, headers=None, body=None, query=None):
        url = urllib.parse.urljoin(self.base + "/", path.lstrip("/"))
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, data=body.encode() if isinstance(body, str) else body, method=method,
                                     headers={"Accept": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self.ctx) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise SourceError("It uses its own security certificate. Set \"insecure_tls\": true if it's your equipment.")
            if isinstance(reason, ConnectionRefusedError):
                raise SourceError("It refused the connection. Check the address and that its API is switched on.")
            raise SourceError(f"It didn't answer ({reason}).")
        except TimeoutError:
            raise SourceError("It didn't answer in time.")


# ---------- MikroTik ----------

class MikroTik:
    """RouterOS 7 REST API (https://<router>/rest). Use a user in a read-only group:
    /user group add name=orbit-ro policy=read,api,rest-api,!write,!policy
    /user add name=orbit group=orbit-ro password=…
    For data per device, add the devices to Kid Control (IP › Kid Control) under a profile with no limits."""

    kind = "mikrotik"
    every = 5  # seconds between reads: gentle on the router

    def __init__(self, host, username, password, insecure_tls=False):
        self.host = host
        self.http = _Http(host, insecure_tls)
        self.auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.model = None
        self.usage = None  # whether Kid Control counts bytes per device

    def _get(self, path, optional=False):
        status, text = self.http.request("GET", "/rest" + path, headers={"Authorization": self.auth})
        if status == 401:
            raise SourceError("The router refused the username or password.")
        if status == 404 and optional:
            return None
        if status >= 300 or not text.lstrip().startswith(("[", "{")):
            raise SourceError("This doesn't look like a MikroTik REST API. Check the address, and that www-ssl is on (IP › Services)."
                              if status < 400 or status == 404 else f"The router answered with error {status}.")
        return json.loads(text)

    def read(self):
        res = self._get("/system/resource")
        self.model = res.get("board-name")
        uptime = _duration_s(res.get("uptime")) or 0
        leases = self._get("/ip/dhcp-server/lease") or []
        arp = self._get("/ip/arp") or []
        wifi = []
        for path in ("/interface/wifi/registration-table", "/interface/wireless/registration-table", "/caps-man/registration-table"):
            try:
                rows = self._get(path, optional=True)
            except SourceError:
                rows = None
            if rows:
                wifi = rows
                break
        try:
            kids = self._get("/ip/kid-control/device", optional=True)
        except SourceError:
            kids = None
        self.usage = bool(kids)

        out = {}

        def entry(mac):
            m = norm_mac(mac)
            cid = client_id_for(m)
            if not cid:
                return None
            return out.setdefault(cid, {"client_id": cid, "mac_address": m, "given_name": "", "name": "", "iface": None,
                                        "domain": None, "ip_address": None, "blocked": False, "source": self.kind})

        for a in arp:
            if a.get("complete") == "false" or a.get("disabled") == "true" or not a.get("mac-address"):
                continue
            e = entry(a["mac-address"])
            if e:
                e["ip_address"] = a.get("address")
        online = set(out)
        for le in leases:
            if le.get("status") != "bound":
                continue
            e = entry(le.get("active-mac-address") or le.get("mac-address"))
            if not e:
                continue
            e.update(ip_address=le.get("active-address") or le.get("address") or e["ip_address"],
                     name=le.get("host-name") or "", given_name=le.get("comment") or "", domain=le.get("server"))
            online.add(e["client_id"])
        for w in wifi:
            e = entry(w.get("mac-address"))
            if not e:
                continue
            sig = re.match(r"-?\d+", str(w.get("signal") or w.get("signal-strength") or ""))
            e.update(iface=_iface_for(w.get("band")) or "RF_5GHZ", signal_strength=float(sig.group()) if sig else None,
                     ap_name=w.get("interface"), associated_time_s=_duration_s(w.get("uptime")))
            online.add(e["client_id"])
        for k in kids or []:
            e = entry(k.get("mac-address"))
            if not e:
                continue
            # Kid Control counts since the router started; Orbit spots a restart when this time goes backwards.
            e.update(rx_stats={"bytes": _num(k.get("bytes-down")) or 0, "rate_mbps": (_rate(k.get("rate-down")) or 0) / 1e6},
                     tx_stats={"bytes": _num(k.get("bytes-up")) or 0, "rate_mbps": (_rate(k.get("rate-up")) or 0) / 1e6},
                     usage_time_s=uptime)
            if not e["ip_address"] and k.get("ip-address"):
                e["ip_address"] = k["ip-address"]
        return [e for cid, e in out.items() if cid in online]


def _rate(v):
    m = re.match(r"([\d.]+)\s*([kmg]?)bps", str(v or ""), re.I)
    if not m:
        return _num(v)
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]


# ---------- Grandstream GWN ----------

def _sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


class Grandstream:
    """GWN.Cloud (https://www.gwn.cloud, Europe: https://eu.gwn.cloud) or a GWN Manager's own address.
    Switch on API Developer mode in its account menu to get the App ID and Secret Key."""

    kind = "grandstream"
    every = 30  # the cloud API is shared by many users; once every 30 seconds is plenty for signal

    def __init__(self, url, app_id, secret_key, network=None, insecure_tls=False):
        self.host = url or "https://www.gwn.cloud"
        self.http = _Http(self.host, insecure_tls)
        self.app_id, self.secret = str(app_id).strip(), str(secret_key).strip()
        self.network = (network or "").strip()
        self.token = None
        self.token_until = 0
        self.names = None  # (when, [networks], {ap mac: name})
        self.model = "GWN.Cloud" if "gwn.cloud" in self.host else "GWN Manager"
        self.usage = True

    def _token(self):
        if self.token and time.time() < self.token_until:
            return self.token
        q = {"grant_type": "client_credentials", "client_id": self.app_id, "client_secret": self.secret}
        status, text = self.http.request("GET", "/oauth/token", query=q)
        if status == 404:
            status, text = self.http.request("GET", "/oapi/oauth/token", query=q)
        try:
            j = json.loads(text)
        except ValueError:
            raise SourceError("This doesn't look like GWN.Cloud or GWN Manager. Check the address.")
        if not j.get("access_token"):
            raise SourceError("GWN refused the App ID or Secret Key.")
        self.token, self.token_until = j["access_token"], time.time() + max(60, (j.get("expires_in") or 3600) - 120)
        return self.token

    def _call(self, path, body, retry=True):
        """Signed request: signature = sha256("&access_token=…&appID=…&secretKey=…&timestamp=…&" + sha256(body) + "&")."""
        token, ts = self._token(), int(time.time() * 1000)
        raw = json.dumps(body, separators=(",", ":"))
        sig = _sha256(f"&access_token={token}&appID={self.app_id}&secretKey={self.secret}&timestamp={ts}&{_sha256(raw)}&")
        status, text = self.http.request("POST", f"/oapi/v1.0.0/{path}", headers={"Content-Type": "application/json"}, body=raw,
                                         query={"access_token": token, "appID": self.app_id, "timestamp": ts, "signature": sig})
        try:
            j = json.loads(text)
        except ValueError:
            raise SourceError(f"GWN answered with error {status}.")
        code = int(_num(j.get("retCode")) or 0)
        if code != 0:
            if code == 50003 and retry:
                self.token = None
                return self._call(path, body, False)
            raise SourceError("GWN refused the App ID or Secret Key." if code in (19006, 50003)
                              else "GWN refused the request time. Check this computer's clock." if code == 19001
                              else f"GWN error {code}: {j.get('msg') or ''}".strip())
        return j.get("data") or {}

    def _pages(self, path, body):
        rows = []
        for page in range(1, 51):
            d = self._call(path, {**body, "pageNum": page, "pageSize": 100})
            rows += d.get("result") or []
            if page >= int(d.get("totalPage") or d.get("pages") or 1):
                break
        return rows

    def _lookup(self):
        if self.names and time.time() - self.names[0] < 600:
            return self.names
        nets = [n for n in self._pages("network/list", {"order": "id", "type": "asc", "search": ""})
                if not self.network or self.network.lower() in (str(n.get("id")), str(n.get("networkName", "")).lower())]
        if not nets:
            raise SourceError(f"There's no GWN network called \"{self.network}\"." if self.network else "This GWN account has no networks.")
        aps = {}
        for n in nets:
            for a in self._pages("ap/list", {"networkId": n["id"]}):
                if norm_mac(a.get("mac")):
                    aps[norm_mac(a["mac"])] = a.get("name") or a["mac"]
        self.names = (time.time(), nets, aps)
        return self.names

    def read(self):
        _, nets, aps = self._lookup()
        out = []
        for n in nets:
            for c in self._pages("client/list", {"networkId": n["id"], "order": "", "search": "", "filter": {"ssidId": "all_ssid"}}):
                mac = norm_mac(c.get("clientId"))
                cid = client_id_for(mac)
                if not cid:
                    continue
                band = {2: "RF_2GHZ", 5: "RF_5GHZ", 6: "RF_6GHZ"}.get(int(_num(c.get("channelClass")) or 0))
                out.append({
                    "client_id": cid, "mac_address": mac, "given_name": "", "name": c.get("name") or "", "iface": band or "RF_5GHZ",
                    "domain": c.get("ssid"), "ip_address": c.get("ipv4"), "signal_strength": _num(c.get("rssi")), "blocked": False,
                    "ap_name": aps.get(norm_mac(c.get("apId")) or "", c.get("apId")), "ssid": c.get("ssid"),
                    "associated_time_s": _duration_s(c.get("assoctime")),
                    "rx_stats": {"bytes": _num(c.get("rxBytes")) or 0, "rate_mbps": _num(c.get("rxRate"))},  # the device's download
                    "tx_stats": {"bytes": _num(c.get("txBytes")) or 0, "rate_mbps": _num(c.get("txRate"))},
                    "usage_time_s": _duration_s(c.get("assoctime")), "source": self.kind,
                })
        return out


# ---------- several sources together ----------

WIFI_FIELDS = ("iface", "signal_strength", "snr", "ap_name", "ssid", "channel_width")


class Sources:
    """Reads each source on its own schedule and merges the latest lists by MAC address.
    Wi-Fi details come from the source that reports a signal; byte counters stay with the first source that
    counted a device, so a device's counter never switches between two sources."""

    def __init__(self, sources):
        self.sources = sources
        self.latest = {id(s): [] for s in sources}
        self.status = {id(s): {"ok": None, "error": None, "updated": None, "clients": 0} for s in sources}
        self.due = {id(s): 0.0 for s in sources}
        self.counter_owner = {}  # client_id -> kind of the source whose byte counters Orbit follows
        self.lock = threading.Lock()

    def poll(self, t=None):
        """Re-read the sources that are due and return the merged client list. Raises SourceError if every source fails."""
        t = t or time.time()
        for s in self.sources:
            k = id(s)
            if t < self.due[k]:
                continue
            try:
                rows = s.read()
                with self.lock:
                    self.latest[k] = rows
                    self.status[k] = {"ok": True, "error": None, "updated": t, "clients": len(rows)}
                self.due[k] = t + s.every
            except Exception as e:
                with self.lock:
                    self.latest[k] = []
                    self.status[k] = {**self.status[k], "ok": False, "error": str(e) if isinstance(e, SourceError) else f"Couldn't read it ({e})."}
                self.due[k] = t + max(s.every, 30)
        if all(st["ok"] is False for st in self.status.values()):
            raise SourceError("; ".join(st["error"] for st in self.status.values()))
        return self.merge()

    def merge(self):
        merged = {}
        with self.lock:
            lists = [(s, self.latest[id(s)]) for s in self.sources]
        for s, rows in lists:
            for c in rows:
                cid = c["client_id"]
                m = merged.get(cid)
                if m is None:
                    merged[cid] = m = dict(c)
                else:
                    for k, v in c.items():
                        if m.get(k) in (None, "") and v not in (None, ""):
                            m[k] = v
                if c.get("signal_strength") is not None:  # this source knows the Wi-Fi side
                    for k in WIFI_FIELDS:
                        if c.get(k) is not None:
                            m[k] = c[k]
                if "rx_stats" in c:
                    owner = self.counter_owner.setdefault(cid, s.kind)
                    if owner == s.kind:
                        m["rx_stats"], m["tx_stats"] = c["rx_stats"], c["tx_stats"]
                        m["associated_time_s"] = c.get("usage_time_s") or c.get("associated_time_s") or 0
                        m["counter_source"] = s.kind
        # With a Wi-Fi controller answering, a device none of its access points reports is on a cable.
        wifi_seen = any(s.kind == "grandstream" and self.status[id(s)]["ok"] for s in self.sources)
        for m in merged.values():
            if wifi_seen and m.get("signal_strength") is None and not m.get("iface"):
                m["iface"] = "ETH"
            m.setdefault("rx_stats", {"bytes": 0})
            m.setdefault("tx_stats", {"bytes": 0})
            if m.get("associated_time_s") is None:
                m["associated_time_s"] = 0
        return list(merged.values())

    def out(self):
        """For the dashboard: each source, whether it answered, and what it found. No passwords."""
        with self.lock:
            return [{"kind": s.kind, "host": s.host, "model": getattr(s, "model", None), "counts_usage": getattr(s, "usage", None),
                     **{k: (v * 1000 if k == "updated" and v else v) for k, v in self.status[id(s)].items()}} for s in self.sources]


# ---------- settings ----------

def load_config(path):
    """data/routers.json, for example:
    {
      "mikrotik":    {"host": "192.168.88.1", "username": "orbit", "password": "…", "insecure_tls": true},
      "grandstream": {"url": "https://www.gwn.cloud", "app_id": "…", "secret_key": "…", "network": "Home"}
    }
    It holds passwords, so keep it readable by you only (chmod 600). Returns a Sources, or None when there's no file."""
    if not os.path.exists(path):
        return None
    if os.stat(path).st_mode & 0o077:
        print(f"Warning: {path} holds router passwords but other users on this computer can read it. Run: chmod 600 {path}")
    with open(path) as f:
        cfg = json.load(f)
    sources = []
    m = cfg.get("mikrotik")
    if m:
        sources.append(MikroTik(m["host"], m["username"], m["password"], bool(m.get("insecure_tls"))))
    gw = cfg.get("grandstream")
    if gw:
        sources.append(Grandstream(gw.get("url"), gw["app_id"], gw["secret_key"], gw.get("network"), bool(gw.get("insecure_tls"))))
    return Sources(sources) if sources else None
