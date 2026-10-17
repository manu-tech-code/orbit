"""Tests the MikroTik and Grandstream sources (orbit/routers.py) against made-up replies from local mock servers,
then feeds their merged device list through Orbit's collector. It sends nothing to a real router, and uses a
throwaway database.

Usage:  .venv/bin/python scripts/test_routers_other.py
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time as real_time
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orbit import collector as C  # noqa: E402
from orbit import routers as R  # noqa: E402
from orbit.store import Store  # noqa: E402

NOW = [real_time.mktime((2026, 10, 15, 12, 0, 0, 0, 0, -1))]
fake = types.SimpleNamespace(**{k: getattr(real_time, k) for k in dir(real_time) if not k.startswith("__")})
fake.time = lambda: NOW[0]
C.time = fake

failures = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


# ---------- mock MikroTik (RouterOS 7 REST) ----------
MT = {"down": 1_000_000, "uptime": "3d4h5m6s", "auth": "Basic " + __import__("base64").b64encode(b"orbit:pw").decode()}


class MikroTikMock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != MT["auth"]:
            return self._send(401, {"error": 401, "message": "Unauthorized"})
        p = self.path
        if p == "/rest/system/resource":
            return self._send(200, {"board-name": "RB5009UG+S+", "version": "7.20.2", "uptime": MT["uptime"]})
        if p == "/rest/ip/dhcp-server/lease":
            return self._send(200, [
                {"address": "192.168.88.20", "mac-address": "AA:BB:CC:00:00:01", "host-name": "Kofis-iPhone", "status": "bound", "server": "dhcp-lan"},
                {"address": "192.168.88.21", "mac-address": "AA:BB:CC:00:00:02", "host-name": "office-pc", "comment": "Office PC", "status": "bound", "server": "dhcp-lan"},
                {"address": "192.168.88.30", "mac-address": "AA:BB:CC:00:00:09", "host-name": "old-laptop", "status": "waiting"}])
        if p == "/rest/ip/arp":
            return self._send(200, [{"address": "192.168.88.20", "mac-address": "AA:BB:CC:00:00:01", "complete": "true"},
                                    {"address": "192.168.88.21", "mac-address": "AA:BB:CC:00:00:02", "complete": "true"}])
        if p == "/rest/ip/kid-control/device":
            return self._send(200, [
                {"mac-address": "AA:BB:CC:00:00:01", "bytes-down": str(MT["down"]), "bytes-up": "200000", "rate-down": "4.5Mbps", "rate-up": "300kbps"},
                {"mac-address": "AA:BB:CC:00:00:02", "bytes-down": "9000000", "bytes-up": "1000000", "rate-down": "0bps", "rate-up": "0bps"}])
        return self._send(404, {"error": 404, "message": "Not Found"})

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


# ---------- mock GWN.Cloud / GWN Manager ----------
SIGNED = [0]


class GwnMock(MikroTikMock):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/oauth/token":
            ok = q.get("client_id") == "app1" and q.get("client_secret") == "sec1"
            return self._send(200 if ok else 401, {"access_token": "T1", "expires_in": 7200} if ok else {"error": "invalid_client"})
        return self._send(404, {})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        h = lambda s: hashlib.sha256(s.encode()).hexdigest()
        if q.get("signature") != h(f"&access_token={q.get('access_token')}&appID={q.get('appID')}&secretKey=sec1&timestamp={q.get('timestamp')}&{h(body)}&"):
            return self._send(200, {"retCode": 50003, "msg": "signature"})
        SIGNED[0] += 1
        b = json.loads(body)
        if u.path == "/oapi/v1.0.0/network/list":
            return self._send(200, {"retCode": 0, "data": {"result": [{"id": 8575, "networkName": "Trassaco"}], "pages": 1}})
        if u.path == "/oapi/v1.0.0/ap/list":
            return self._send(200, {"retCode": 0, "data": {"result": [{"mac": "000B82AFD184", "name": "AP-1 Lounge"}], "totalPage": 1}})
        if u.path == "/oapi/v1.0.0/client/list" and b.get("networkId") == 8575:
            return self._send(200, {"retCode": 0, "data": {"totalPage": 1, "result": [
                {"clientId": "AA:BB:CC:00:00:01", "name": "Kofis-iPhone", "ipv4": "192.168.88.20", "channel": 36, "channelClass": 5,
                 "rssi": -63, "txRate": 433, "rxRate": 390, "apId": "00:0B:82:AF:D1:84", "ssid": "Home", "assoctime": "01:02:03",
                 "rxBytes": 777, "txBytes": 111}]}})
        return self._send(200, {"retCode": 1, "msg": "unexpected"})


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


mt_srv, mt_url = serve(MikroTikMock)
gw_srv, gw_url = serve(GwnMock)

print("small helpers")
check(R.client_id_for("AA:BB:CC:00:00:01") == str(0xAABBCC000001), "client_id is the MAC as digits")
check(R.client_id_for("aa-bb-cc-00-00-01") == R.client_id_for("AABBCC000001"), "same client_id from any MAC spelling")
check(R._duration_s("3d4h5m6s") == 3 * 86400 + 4 * 3600 + 5 * 60 + 6, "RouterOS durations")
check(R._duration_s("01:02:03") == 3723 and R._duration_s("2d 01:00:00") == 2 * 86400 + 3600, "GWN durations")

print("MikroTik on its own")
mt = R.MikroTik(mt_url, "orbit", "pw")
rows = {r["mac_address"]: r for r in mt.read()}
check(set(rows) == {"AA:BB:CC:00:00:01", "AA:BB:CC:00:00:02"}, "bound leases and the address table; a waiting lease is left out")
phone = rows["AA:BB:CC:00:00:01"]
check(phone["name"] == "Kofis-iPhone" and phone["ip_address"] == "192.168.88.20", "host name and IP")
check(rows["AA:BB:CC:00:00:02"]["given_name"] == "Office PC", "a lease comment becomes the name you gave it")
check(phone["rx_stats"]["bytes"] == 1_000_000 and phone["tx_stats"]["bytes"] == 200000, "Kid Control: rx is the download")
check(abs(phone["rx_stats"]["rate_mbps"] - 4.5) < 1e-9, "Kid Control rate in Mbps")
check(mt.model == "RB5009UG+S+" and mt.usage is True, "model and data per device known")
try:
    R.MikroTik(mt_url, "orbit", "wrong").read()
    check(False, "wrong password refused")
except R.SourceError as e:
    check("username or password" in str(e), "wrong password: plain-words error")

print("Grandstream on its own")
gw = R.Grandstream(gw_url, "app1", "sec1")
g = gw.read()
check(len(g) == 1 and g[0]["signal_strength"] == -63, "client with its signal")
check(g[0]["ap_name"] == "AP-1 Lounge" and g[0]["iface"] == "RF_5GHZ", "access point name and band")
check(g[0]["rx_stats"]["bytes"] == 777 and g[0]["associated_time_s"] == 3723, "bytes and connection time")
check(SIGNED[0] >= 3, "every request was signed correctly")
try:
    R.Grandstream(gw_url, "app1", "nope").read()
    check(False, "wrong key refused")
except R.SourceError as e:
    check("App ID or Secret Key" in str(e), "wrong key: plain-words error")

print("both together, through the collector")
src = R.Sources([R.MikroTik(mt_url, "orbit", "pw"), R.Grandstream(gw_url, "app1", "sec1")])
merged = {c["mac_address"]: c for c in src.poll(NOW[0])}
p = merged["AA:BB:CC:00:00:01"]
check(p["signal_strength"] == -63 and p["ap_name"] == "AP-1 Lounge", "Wi-Fi details come from Grandstream")
check(p["rx_stats"]["bytes"] == 1_000_000 and p["counter_source"] == "mikrotik", "byte counters stay with MikroTik")
check(merged["AA:BB:CC:00:00:02"].get("signal_strength") is None, "a wired PC has no signal")
check(merged["AA:BB:CC:00:00:02"]["iface"] == "ETH" and p["iface"] == "RF_5GHZ", "no access point reports the PC, so it's wired")

tmp = tempfile.mkdtemp()
store = Store(os.path.join(tmp, "test.db"))
c = C.Collector(store, sources=src)


def poll(step=5):
    NOW[0] += step
    src.due = {k: 0.0 for k in src.due}  # read the mocks every poll in the test
    c._on_clients(src.poll(NOW[0]), NOW[0])


poll()
pid = R.client_id_for("AA:BB:CC:00:00:01")
check(pid in c.devices and len(c.devices) == 2, "both devices known, keyed by client_id")
d = c.devices[pid]
check(d.get("signal") == -63 and d.get("ap") == "AP-1 Lounge", "signal and access point on the device")
check(c.dev_today.get(pid, [0, 0])[0] == 0, "first sight: earlier Kid Control totals aren't counted as today")
MT["down"] += 5_000_000
poll()
check(abs(c.dev_today[pid][0] - 5_000_000) < 1, "5 MB downloaded since the last read is counted")
MT["down"] = 2_000_000
MT["uptime"] = "2m"  # the router restarted: Kid Control counts again from zero
poll()
check(abs(c.dev_today[pid][0] - 7_000_000) < 1, "after a router restart, its new count is added")
out = c.state()
check(out["sources"] and out["sources"][0]["kind"] == "mikrotik" and out["sources"][0]["ok"], "the dashboard sees each source and its health")
check(all("password" not in json.dumps(s) and "sec1" not in json.dumps(s) for s in out["sources"]), "no passwords in what the dashboard sees")
dev = next(x for x in out["devices"] if x["id"] == pid)
check(dev["ap"] == "AP-1 Lounge" and dev["source"] == "mikrotik", "device output names its access point and counter source")

print("one source down")
gw_srv.shutdown()
gw_srv.server_close()
NOW[0] += 5
src.due = {k: 0.0 for k in src.due}
try:
    rows = src.poll(NOW[0])
    check(len(rows) == 2, "MikroTik still lists the devices")
except R.SourceError:
    check(False, "MikroTik still lists the devices")
check(any(s["ok"] is False and s["error"] for s in src.out()), "Grandstream reported as not answering, with a reason")

print("settings file")
cfg = os.path.join(tmp, "routers.json")
with open(cfg, "w") as f:
    json.dump({"mikrotik": {"host": mt_url, "username": "orbit", "password": "pw"}}, f)
os.chmod(cfg, 0o600)
s = R.load_config(cfg)
check(s and s.sources[0].kind == "mikrotik", "routers.json loads a MikroTik source")
check(R.load_config(os.path.join(tmp, "none.json")) is None, "no file: Orbit uses the Starlink router as before")

mt_srv.shutdown()
print("\n" + ("All passed." if not failures else f"{len(failures)} failed:\n  " + "\n  ".join(failures)))
sys.exit(1 if failures else 0)
