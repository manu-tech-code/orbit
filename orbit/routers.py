"""Device lists from routers and Wi-Fi controllers other than Starlink's.

Orbit's collector works from the Starlink router's client list (wifi_get_clients). Each source here reads
another brand and returns the same shape, so devices, usage, allowances, alerts and the map work unchanged.
It's for homes where the Starlink router is in bypass mode and another router runs the network.

  MikroTik     RouterOS 7 REST API with a read-only user: DHCP leases and the address table (who's there),
               the Wi-Fi registration table (signal, when the MikroTik runs the Wi-Fi), Kid Control (bytes per device).
  Grandstream  GWN.Cloud or GWN Manager API (App ID and Secret Key): Wi-Fi clients with signal, access point and bytes.
  SNMP         Any brand of firewall, router or switch (Sophos, Cisco, Fortinet, HP…), v1, v2c or v3: the address table
               (devices on every network section), switch ports, and Cisco Wi-Fi controllers' clients with signal.

Several sources can run at once, for example MikroTik for every device and its data, and Grandstream for Wi-Fi
signal. Their lists are merged by MAC address. Each device's byte counters always come from one source, so a
counter never jumps between two of them.

Every request here only reads. Settings live in data/routers.json (see load_config); passwords never leave it.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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
    reports_wifi = True
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


# ---------- SNMP: firewalls, switches and Cisco Wi-Fi controllers of any brand ----------

# Standard MIBs, plus Cisco's Wi-Fi controller table (AIRESPACE-WIRELESS-MIB, served by AireOS and Catalyst 9800 / EWC).
OID = {
    "sysDescr": "1.3.6.1.2.1.1.1.0", "sysObjectID": "1.3.6.1.2.1.1.2.0", "sysUpTime": "1.3.6.1.2.1.1.3.0", "sysName": "1.3.6.1.2.1.1.5.0",
    "ipNetToPhysicalPhysAddress": "1.3.6.1.2.1.4.35.1.4", "ipNetToMediaPhysAddress": "1.3.6.1.2.1.4.22.1.2",
    "dot1dBasePortIfIndex": "1.3.6.1.2.1.17.1.4.1.2", "dot1dTpFdbPort": "1.3.6.1.2.1.17.4.3.1.2", "dot1qTpFdbPort": "1.3.6.1.2.1.17.7.1.2.2.1.2",
    "vtpVlanState": "1.3.6.1.4.1.9.9.46.1.3.1.1.2",
    "ifType": "1.3.6.1.2.1.2.2.1.3", "ifName": "1.3.6.1.2.1.31.1.1.1.1", "ifHCInOctets": "1.3.6.1.2.1.31.1.1.1.6", "ifHCOutOctets": "1.3.6.1.2.1.31.1.1.1.10",
    "wlcClientIp": "1.3.6.1.4.1.14179.2.1.4.1.2", "wlcClientAp": "1.3.6.1.4.1.14179.2.1.4.1.4", "wlcClientSsid": "1.3.6.1.4.1.14179.2.1.4.1.7",
    "wlcClientProtocol": "1.3.6.1.4.1.14179.2.1.4.1.25", "wlcClientRssi": "1.3.6.1.4.1.14179.2.1.6.1.1",
    "wlcClientBytesIn": "1.3.6.1.4.1.14179.2.1.6.1.2", "wlcClientBytesOut": "1.3.6.1.4.1.14179.2.1.6.1.3",
    "wlcClientSnr": "1.3.6.1.4.1.14179.2.1.6.1.26", "wlcApName": "1.3.6.1.4.1.14179.2.2.1.1.3",
}
PHYSICAL_PORT = {6, 62, 69, 117}  # Ethernet interface types
# bsnMobileStationProtocol -> band
WLC_BAND = {1: "RF_5GHZ", 2: "RF_2GHZ", 3: "RF_2GHZ", 6: "RF_2GHZ", 7: "RF_5GHZ", 10: "RF_5GHZ", 11: "RF_5GHZ", 12: "RF_2GHZ", 13: "RF_6GHZ"}
SNMP_ERRORS = {
    "RequestTimedOut": "No answer. Check the address, that SNMP is on and that this computer is allowed to ask (SNMP stays silent when the community or user is wrong).",
    "WrongDigest": "The device refused the SNMP v3 password (or the authentication type: SHA or SHA256).",
    "UnknownUserName": "The device doesn't know that SNMP v3 user.",
    "DecryptionError": "The SNMP v3 privacy (encryption) password looks wrong.",
    "UnsupportedSecLevel": "The device wants a different SNMP v3 security level (authentication and privacy).",
    "NotInTimeWindow": "The device's SNMP v3 clock check failed; it usually settles on the next read.",
}


def _mac_of(raw):
    return ":".join(f"{x:02X}" for x in raw) if isinstance(raw, (bytes, bytearray)) and len(raw) == 6 else None


def _mac_in_index(parts):
    return ":".join(f"{int(x):02X}" for x in parts) if len(parts) == 6 else None


def _ignored_mac(mac):
    """Multicast and router-redundancy (VRRP/HSRP) addresses aren't devices."""
    return not mac or int(mac[:2], 16) & 1 or mac.startswith(("00:00:5E:00:01", "00:00:5E:00:02", "00:00:0C:07:AC", "00:00:0C:9F:F"))


def _private(ip):
    p = [int(x) for x in ip.split(".")] if ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) else []
    return bool(p) and (p[0] == 10 or (p[0] == 172 and 16 <= p[1] <= 31) or (p[0] == 192 and p[1] == 168))


class SnmpDevice:
    """One device to read over SNMP, with its own login. Settings: host, version ("2c" or "3"), community (v2c),
    or user, auth (SHA, SHA256, …), auth_password, priv (AES, AES256 or none) and priv_password (v3)."""

    def __init__(self, cfg):
        self.host = cfg["host"]
        self.port = int(cfg.get("port") or 161)
        self.version = str(cfg.get("version") or "2c")
        self.community = cfg.get("community") or "public"
        self.user, self.auth, self.priv = cfg.get("user"), (cfg.get("auth") or "SHA").upper(), (cfg.get("priv") or "AES").upper()
        self.auth_password, self.priv_password = cfg.get("auth_password"), cfg.get("priv_password")
        self.name = None
        self.model = None
        self.uptime = 0.0
        self.status = {"ok": None, "error": None, "updated": None, "clients": 0}
        self._target = None

    def login(self, hl, context=None):
        """pysnmp's login for this device. context: a Cisco VLAN, which v2c adds to the community and v3 asks as a context."""
        if self.version == "3":
            auth = {"MD5": hl.usmHMACMD5AuthProtocol, "SHA": hl.usmHMACSHAAuthProtocol, "SHA1": hl.usmHMACSHAAuthProtocol,
                    "SHA224": hl.usmHMAC128SHA224AuthProtocol, "SHA256": hl.usmHMAC192SHA256AuthProtocol,
                    "SHA384": hl.usmHMAC256SHA384AuthProtocol, "SHA512": hl.usmHMAC384SHA512AuthProtocol}.get(self.auth.replace("-", ""))
            priv = {"AES": hl.usmAesCfb128Protocol, "AES128": hl.usmAesCfb128Protocol, "AES192": hl.usmAesCfb192Protocol,
                    "AES256": hl.usmAesCfb256Protocol, "NONE": hl.usmNoPrivProtocol}.get(self.priv.replace("-", ""))
            if auth is None or priv is None:
                raise SourceError(f"Unsupported SNMP v3 setting for {self.host}: auth {self.auth}, priv {self.priv}.")
            data = hl.UsmUserData(self.user, self.auth_password, self.priv_password if priv is not hl.usmNoPrivProtocol else None,
                                  authProtocol=auth, privProtocol=priv)
            return data, hl.ContextData(contextName=f"vlan-{context}" if context else "")
        community = f"{self.community}@{context}" if context else self.community
        return hl.CommunityData(community, mpModel=0 if self.version == "1" else 1), hl.ContextData()


class Snmp:
    """Reads one or more devices over SNMP and merges what they know:
      - firewalls and routers (any brand: Sophos, Fortinet, Cisco, MikroTik…): the address table, so every device on
        every network section (VLAN) they route is listed, with its IP address
      - switches: which port each device is on; a device alone on a port is wired, and that port's byte counters are its usage
      - Cisco Wi-Fi controllers (AireOS, Catalyst 9800, Embedded Wireless Controller): each Wi-Fi device's signal, noise,
        access point, network name, band and bytes
    Names come from the network's reverse DNS, since SNMP doesn't carry them."""

    kind = "snmp"
    every = 30  # big tables on a busy switch: twice a minute is plenty

    def __init__(self, devices):
        self.devices = [SnmpDevice(d) for d in devices]
        self.host = ", ".join(d.host for d in self.devices)
        self.model = None
        self.usage = None
        self.reports_wifi = False
        self.loop = None
        self.engine = None
        self.names = {}  # ip -> (name, when looked up)

    # pysnmp is asynchronous; Orbit's loops are threads, so this source keeps an event loop of its own.
    def _run(self, coro):
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
        return self.loop.run_until_complete(coro)

    async def _get(self, hl, dev, oids):
        if dev._target is None:
            dev._target = await hl.UdpTransportTarget.create((dev.host, dev.port), timeout=2.5, retries=1)
        auth, ctx = dev.login(hl)
        err, status, _, binds = await hl.get_cmd(self.engine, auth, dev._target, ctx, *[hl.ObjectType(hl.ObjectIdentity(o)) for o in oids])
        if err:
            raise SourceError(SNMP_ERRORS.get(type(err).__name__, f"SNMP error: {err}"))
        if status:
            raise SourceError(f"SNMP error: {status.prettyPrint()}")
        return {str(o): v for o, v in binds}

    async def _walk(self, hl, dev, base, context=None, limit=20000):
        """[(index after base, value)] for one table column. Returns [] if the device doesn't have it."""
        from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject
        auth, ctx = dev.login(hl, context)
        rows = []
        try:
            async for err, status, _, binds in hl.bulk_walk_cmd(self.engine, auth, dev._target, ctx, 0, 25,
                                                                hl.ObjectType(hl.ObjectIdentity(base)), lexicographicMode=False):
                if err or status:
                    break
                for o, v in binds:
                    if isinstance(v, (EndOfMibView, NoSuchInstance, NoSuchObject)):
                        continue
                    rows.append((str(o)[len(base) + 1:], v))
                if len(rows) >= limit:
                    break
        except Exception:
            return rows
        return rows

    async def _read_device(self, hl, dev):
        sysv = await self._get(hl, dev, [OID["sysName"], OID["sysDescr"], OID["sysObjectID"], OID["sysUpTime"]])
        dev.name = bytes(sysv[OID["sysName"]].asOctets()).decode("utf-8", "replace").strip() or dev.host
        descr = bytes(sysv[OID["sysDescr"]].asOctets()).decode("utf-8", "replace")
        dev.model = descr.splitlines()[0][:60] if descr else None
        dev.uptime = int(sysv[OID["sysUpTime"]]) / 100
        cisco = str(sysv[OID["sysObjectID"]]).startswith("1.3.6.1.4.1.9.")
        r = {"arp": {}, "fdb": {}, "ifaces": {}, "wifi": {}}

        # Address table: IP -> MAC (the newer table first, then the classic one)
        for idx, v in await self._walk(hl, dev, OID["ipNetToPhysicalPhysAddress"]):
            p = idx.split(".")
            if len(p) == 7 and p[1] == "1" and p[2] == "4":
                r["arp"][".".join(p[3:])] = _mac_of(v.asOctets())
        if not r["arp"]:
            for idx, v in await self._walk(hl, dev, OID["ipNetToMediaPhysAddress"]):
                p = idx.split(".")
                if len(p) == 5:
                    r["arp"][".".join(p[1:])] = _mac_of(v.asOctets())

        # Switch forwarding table: MAC -> interface. Cisco keeps one table per VLAN, asked with a VLAN context.
        async def fdb(context=None):
            ports = {k: int(v) for k, v in await self._walk(hl, dev, OID["dot1dBasePortIfIndex"], context)}
            out = {}
            for idx, v in await self._walk(hl, dev, OID["dot1qTpFdbPort"], context):
                mac = _mac_in_index(idx.split(".")[1:])
                if mac and int(v):
                    out[mac] = ports.get(str(int(v)), int(v))
            if not out:
                for idx, v in await self._walk(hl, dev, OID["dot1dTpFdbPort"], context):
                    mac = _mac_in_index(idx.split("."))
                    if mac and int(v):
                        out[mac] = ports.get(str(int(v)), int(v))
            return out
        r["fdb"] = await fdb()
        if not r["fdb"] and cisco:
            vlans = [int(idx.split(".")[-1]) for idx, v in await self._walk(hl, dev, OID["vtpVlanState"]) if int(v) == 1]
            for vlan in [v for v in vlans if not 1002 <= v <= 1005][:64]:
                r["fdb"].update(await fdb(vlan))
        if r["fdb"]:
            names = {k: bytes(v.asOctets()).decode("utf-8", "replace") for k, v in await self._walk(hl, dev, OID["ifName"])}
            types = {k: int(v) for k, v in await self._walk(hl, dev, OID["ifType"])}
            hin = {k: int(v) for k, v in await self._walk(hl, dev, OID["ifHCInOctets"])}
            hout = {k: int(v) for k, v in await self._walk(hl, dev, OID["ifHCOutOctets"])}
            for k in names:
                r["ifaces"][int(k)] = {"name": names[k], "physical": types.get(k) in PHYSICAL_PORT, "in": hin.get(k), "out": hout.get(k)}

        # Cisco Wi-Fi controller: clients, indexed by their MAC
        aps = {}
        ap_rows = await self._walk(hl, dev, OID["wlcClientAp"])
        if ap_rows:
            for idx, v in await self._walk(hl, dev, OID["wlcApName"]):
                aps[_mac_in_index(idx.split("."))] = bytes(v.asOctets()).decode("utf-8", "replace")
            cols = {}
            for col in ("wlcClientIp", "wlcClientSsid", "wlcClientProtocol", "wlcClientRssi", "wlcClientBytesIn", "wlcClientBytesOut", "wlcClientSnr"):
                cols[col] = dict(await self._walk(hl, dev, OID[col]))
            for idx, v in ap_rows:
                mac = _mac_in_index(idx.split("."))
                if not mac:
                    continue
                ip = cols["wlcClientIp"].get(idx)
                r["wifi"][mac] = {
                    "ap": aps.get(_mac_of(v.asOctets())) or _mac_of(v.asOctets()),
                    "ip": ".".join(str(x) for x in ip.asOctets()) if ip is not None else None,
                    "ssid": bytes(cols["wlcClientSsid"][idx].asOctets()).decode("utf-8", "replace") if idx in cols["wlcClientSsid"] else None,
                    "band": WLC_BAND.get(int(cols["wlcClientProtocol"][idx])) if idx in cols["wlcClientProtocol"] else None,
                    "rssi": int(cols["wlcClientRssi"][idx]) if idx in cols["wlcClientRssi"] else None,
                    "snr": int(cols["wlcClientSnr"][idx]) if idx in cols["wlcClientSnr"] else None,
                    # Counted at the controller: bytes it sent the device are the device's download
                    "down": int(cols["wlcClientBytesOut"][idx]) if idx in cols["wlcClientBytesOut"] else None,
                    "up": int(cols["wlcClientBytesIn"][idx]) if idx in cols["wlcClientBytesIn"] else None,
                }
        return r

    async def _read_all(self, hl):
        if self.engine is None:
            self.engine = hl.SnmpEngine()
        results = await asyncio.gather(*[self._read_device(hl, d) for d in self.devices], return_exceptions=True)
        return results

    def _hostnames(self, ips):
        """Reverse DNS names, looked up a few at a time and kept for an hour."""
        now = time.time()
        todo = [ip for ip in ips if ip not in self.names or now - self.names[ip][1] > 3600][:64]
        if todo:
            def look(ip):
                try:
                    return ip, socket.gethostbyaddr(ip)[0]
                except OSError:
                    return ip, None
            with ThreadPoolExecutor(max_workers=16) as pool:
                futures = [pool.submit(look, ip) for ip in todo]
                for f in futures:
                    try:
                        ip, name = f.result(timeout=3)
                        self.names[ip] = (name, now)
                    except Exception:
                        pass
        return {ip: (self.names.get(ip) or (None,))[0] for ip in ips}

    def read(self):
        try:
            from pysnmp.hlapi.v3arch import asyncio as hl
        except ImportError:
            raise SourceError("SNMP needs the pysnmp library: .venv/bin/pip install -r requirements.txt")
        results = self._run(self._read_all(hl))
        now = time.time()
        good = []
        for dev, res in zip(self.devices, results):
            if isinstance(res, Exception):
                dev.status = {**dev.status, "ok": False, "error": str(res) if isinstance(res, SourceError) else f"Couldn't read it ({res})."}
            else:
                good.append((dev, res))
        if not good:
            raise SourceError("; ".join(f"{d.host}: {d.status['error']}" for d in self.devices))

        arp, wifi, ports = {}, {}, {}  # ip -> mac; mac -> wifi details; mac -> (device, interface)
        seen_on = {}  # mac -> set of (device, interface) where switches saw it
        for dev, r in good:
            for ip, mac in r["arp"].items():
                if mac and _private(ip) and not _ignored_mac(mac):
                    arp[ip] = mac
            for mac, w in r["wifi"].items():
                wifi[mac] = {**w, "uptime": dev.uptime}
            for mac, ifindex in r["fdb"].items():
                seen_on.setdefault(mac, set()).add((id(dev), ifindex))
        per_port = {}
        for mac, places in seen_on.items():
            for place in places:
                per_port.setdefault(place, set()).add(mac)
        devs = {id(dev): (dev, r) for dev, r in good}
        for mac, places in seen_on.items():
            for place in places:
                dev, r = devs[place[0]]
                iface = r["ifaces"].get(place[1])
                if iface and iface["physical"] and len(per_port[place]) == 1:
                    ports[mac] = (dev, iface)  # alone on a physical port: plugged in there
        ip_of = {mac: ip for ip, mac in arp.items()}
        for mac, w in wifi.items():
            if w.get("ip"):
                ip_of.setdefault(mac, w["ip"])
        # A switch or controller that has seen a device recently is the best sign it's there; the address table alone
        # can remember a device for hours.
        recent = set(seen_on) | set(wifi)
        online = recent if recent else set(ip_of)
        names = self._hostnames([ip_of[m] for m in online if m in ip_of])
        out = []
        for mac in online:
            if _ignored_mac(mac):
                continue
            cid = client_id_for(mac)
            ip = ip_of.get(mac)
            c = {"client_id": cid, "mac_address": mac, "given_name": "", "name": (names.get(ip) or "").split(".")[0], "iface": None,
                 "domain": None, "ip_address": ip, "blocked": False, "source": self.kind}
            w = wifi.get(mac)
            if w:
                c.update(iface=w["band"] or "RF_5GHZ", signal_strength=w["rssi"], snr=w["snr"], ap_name=w["ap"], ssid=w["ssid"])
                if w["down"] is not None and w["up"] is not None:
                    c.update(rx_stats={"bytes": w["down"]}, tx_stats={"bytes": w["up"]}, usage_time_s=w["uptime"])
            elif mac in ports:
                dev, iface = ports[mac]
                c.update(iface="ETH", port=f"{dev.name} · {iface['name']}")
                if iface["in"] is not None and iface["out"] is not None:
                    # The switch's "out" is what it sent the device: the device's download
                    c.update(rx_stats={"bytes": iface["out"]}, tx_stats={"bytes": iface["in"]}, usage_time_s=dev.uptime)
            out.append(c)
        counts = {}
        for c in out:
            counts[c.get("source")] = counts.get(c.get("source"), 0) + 1
        for dev, r in good:
            n = len({m for m in r["fdb"]} | set(r["wifi"]) | {m for m in r["arp"].values() if m})
            dev.status = {"ok": True, "error": None, "updated": now, "clients": n}
        self.reports_wifi = bool(wifi)
        self.usage = any("rx_stats" in c for c in out)
        self.model = ", ".join(d.name for d, _ in good)
        return out

    def close(self):
        if self.engine is not None and self.loop is not None:
            try:
                self.engine.close_dispatcher()
                pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
                for t in pending:
                    t.cancel()
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
        if self.loop is not None:
            self.loop.close()
        self.engine = self.loop = None

    def out_devices(self):
        return [{"kind": "snmp", "host": d.host, "model": " · ".join(x for x in (d.name, d.model) if x) or None, "counts_usage": None,
                 **{k: (v * 1000 if k == "updated" and v else v) for k, v in d.status.items()}} for d in self.devices]


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
        self.rates = {}  # client_id -> (rx, tx, read time, down Mbps, up Mbps): live speed from each source's own reads
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
            read_at = {id(s): self.status[id(s)]["updated"] for s in self.sources}
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
                        m["live_mbps"] = self._rate(cid, c, read_at.get(id(s)))
        # With a Wi-Fi controller answering, a device none of its access points reports is on a cable.
        wifi_seen = any(getattr(s, "reports_wifi", False) and self.status[id(s)]["ok"] for s in self.sources)
        for m in merged.values():
            if wifi_seen and m.get("signal_strength") is None and not m.get("iface"):
                m["iface"] = "ETH"
            m.setdefault("rx_stats", {"bytes": 0})
            m.setdefault("tx_stats", {"bytes": 0})
            if m.get("associated_time_s") is None:
                m["associated_time_s"] = 0
        return list(merged.values())

    def _rate(self, cid, c, t):
        """Live speed from two reads of the same source. Sources refresh every 5 to 30 seconds while Orbit polls every 2,
        so working it out per poll would show a burst whenever new counts arrive."""
        rx, tx = _num(c["rx_stats"].get("bytes")) or 0.0, _num(c["tx_stats"].get("bytes")) or 0.0
        prev = self.rates.get(cid)
        if t is None:
            return prev[3:] if prev else None
        if prev is None or prev[2] == t:
            if prev is None:
                self.rates[cid] = (rx, tx, t, 0.0, 0.0)
            return (prev or (0, 0, 0, 0.0, 0.0))[3:]
        dt = t - prev[2]
        if rx < prev[0] or tx < prev[1] or dt <= 0:  # counters restarted
            down = up = 0.0
        else:
            down, up = (rx - prev[0]) * 8 / 1e6 / dt, (tx - prev[1]) * 8 / 1e6 / dt
        self.rates[cid] = (rx, tx, t, down, up)
        return down, up

    def close(self):
        for s in self.sources:
            if hasattr(s, "close"):
                s.close()

    def out(self):
        """For the dashboard: each source (each SNMP device on its own), whether it answered, and what it found. No passwords."""
        rows = []
        with self.lock:
            for s in self.sources:
                if hasattr(s, "out_devices"):
                    rows += s.out_devices()
                    continue
                rows.append({"kind": s.kind, "host": s.host, "model": getattr(s, "model", None), "counts_usage": getattr(s, "usage", None),
                             **{k: (v * 1000 if k == "updated" and v else v) for k, v in self.status[id(s)].items()}})
        return rows


# ---------- settings ----------

def load_config(path):
    """data/routers.json, for example:
    {
      "mikrotik":    {"host": "192.168.88.1", "username": "orbit", "password": "…", "insecure_tls": true},
      "grandstream": {"url": "https://www.gwn.cloud", "app_id": "…", "secret_key": "…", "network": "Home"},
      "snmp": [
        {"host": "192.168.1.1", "version": "3", "user": "monitor", "auth": "SHA256", "auth_password": "…", "priv": "AES", "priv_password": "…"},
        {"host": "192.168.1.2", "version": "2c", "community": "…"}
      ]
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
    sn = cfg.get("snmp")
    if sn:
        sources.append(Snmp(sn if isinstance(sn, list) else [sn]))
    return Sources(sources) if sources else None
