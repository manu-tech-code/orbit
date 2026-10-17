"""Tests the SNMP source (orbit/routers.py): how it merges a firewall's address table, a switch's ports and a Cisco
Wi-Fi controller's clients into Orbit's device list. The merging is checked with made-up device replies, so it needs
no network. If net-snmp's snmpd is installed, it also reads a throwaway local agent over v2c and v3 (SHA + AES).

Usage:  .venv/bin/python scripts/test_snmp.py
"""
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orbit import routers as R  # noqa: E402

failures = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


print("merging what each device knows (made-up replies)")
FW = {"arp": {"192.168.21.80": "AA:BB:CC:00:00:11", "192.168.24.50": "AA:BB:CC:00:00:22", "192.168.21.99": "AA:BB:CC:00:00:99",
              "192.168.21.253": "00:00:5E:00:01:01", "8.8.8.8": "AA:BB:CC:00:00:77"}, "fdb": {}, "ifaces": {}, "wifi": {}}
SW = {"arp": {}, "ifaces": {5: {"name": "Gi1/0/5", "physical": True, "in": 1000, "out": 50000},
                            1: {"name": "Te1/1/1", "physical": True, "in": 9, "out": 9}},
      "fdb": {"AA:BB:CC:00:00:22": 5, "AA:BB:CC:00:00:11": 1, "F0:D8:05:DA:B0:38": 1}, "wifi": {}}
WLC = {"arp": {}, "fdb": {}, "ifaces": {}, "wifi": {"AA:BB:CC:00:00:11": {"ap": "AP-2F-East", "ip": "192.168.21.80", "ssid": "Staff",
                                                                         "band": "RF_5GHZ", "rssi": -61, "snr": 34, "down": 90000, "up": 4000}}}
s = R.Snmp([{"host": "fw", "version": "3", "user": "u", "auth": "SHA256", "auth_password": "x" * 12, "priv": "AES", "priv_password": "y" * 12},
            {"host": "sw", "community": "c"}, {"host": "wlc", "community": "c"}])
replies = {"fw": FW, "sw": SW, "wlc": WLC}


async def fake_read(hl, dev):
    dev.name, dev.uptime = f"{dev.host}-name", 86400.0
    return replies[dev.host]
s._read_device = fake_read
s._hostnames = lambda ips: {ip: ("laptop-07.amalitech.local" if ip == "192.168.24.50" else None) for ip in ips}
rows = {r["mac_address"]: r for r in s.read()}
phone, pc = rows.get("AA:BB:CC:00:00:11"), rows.get("AA:BB:CC:00:00:22")
check(phone and phone["signal_strength"] == -61 and phone["ap_name"] == "AP-2F-East" and phone["iface"] == "RF_5GHZ", "Wi-Fi client: signal, access point, band")
check(phone["rx_stats"]["bytes"] == 90000 and phone["tx_stats"]["bytes"] == 4000, "Wi-Fi client: bytes sent to it are its download")
check(phone["ip_address"] == "192.168.21.80", "IP address from the firewall's address table")
check(pc and pc["iface"] == "ETH" and pc["port"] == "sw-name · Gi1/0/5", "a device alone on a switch port is wired, on that port")
check(pc["rx_stats"]["bytes"] == 50000 and pc["usage_time_s"] == 86400.0, "its port's out counter is its download; switch uptime spots restarts")
check(pc["name"] == "laptop-07" and pc["ip_address"] == "192.168.24.50", "name from reverse DNS, on another network section")
check("AA:BB:CC:00:00:99" not in rows, "an address-table entry no switch or controller has seen is left out (stale)")
check("00:00:5E:00:01:01" not in rows and "AA:BB:CC:00:00:77" not in rows, "router-redundancy addresses and public IPs are ignored")
check("F0:D8:05:DA:B0:38" in rows and "rx_stats" not in rows["F0:D8:05:DA:B0:38"], "the access point is listed, but its uplink port isn't counted as its usage")
check(s.reports_wifi and s.usage, "it reports Wi-Fi and data per device")
check(len(s.out_devices()) == 3 and all(d["ok"] for d in s.out_devices()), "each SNMP device has its own status line")

print("one device not answering")
s2 = R.Snmp([{"host": "fw", "community": "c"}, {"host": "down", "community": "c"}])


async def half(hl, dev):
    if dev.host == "down":
        raise R.SourceError("No answer.")
    dev.name, dev.uptime = "fw", 10.0
    return FW
s2._read_device = half
s2._hostnames = lambda ips: {}
rows2 = s2.read()
check(len(rows2) == 3, "the firewall's devices still list (address table only: nothing fresher to go on)")
st = {d["host"]: d for d in s2.out_devices()}
check(st["down"]["ok"] is False and st["down"]["error"] == "No answer." and st["fw"]["ok"], "the silent device is reported with its reason")
s3 = R.Snmp([{"host": "down", "community": "c"}])
s3._read_device = half
try:
    s3.read()
    check(False, "every device silent raises an error")
except R.SourceError as e:
    check("down: No answer." in str(e), "every device silent raises an error")

print("settings file")
cfg = os.path.join(tempfile.mkdtemp(), "routers.json")
with open(cfg, "w") as f:
    f.write('{"snmp": [{"host": "192.168.21.1", "version": "3", "user": "u", "auth": "SHA256", "auth_password": "x", "priv": "AES", "priv_password": "y"}]}')
os.chmod(cfg, 0o600)
src = R.load_config(cfg)
check(src and src.sources[0].kind == "snmp" and src.sources[0].devices[0].auth == "SHA256", "routers.json loads SNMP devices")

snmpd = shutil.which("snmpd") or ("/usr/sbin/snmpd" if os.path.exists("/usr/sbin/snmpd") else None)
if not snmpd:
    print("live agent: skipped (net-snmp's snmpd isn't installed)")
else:
    print("live agent (net-snmp snmpd on this computer)")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    d = tempfile.mkdtemp()
    conf = os.path.join(d, "snmpd.conf")
    with open(conf, "w") as f:
        f.write(f"""agentaddress udp:127.0.0.1:{port}
rocommunity orbittest 127.0.0.1
createUser orbitv3 SHA testauth-1234 AES testpriv-1234
rouser orbitv3 priv
sysName test-switch
override 1.3.6.1.2.1.4.22.1.2.4.192.168.21.80 octet_str 0xAABBCC000011
override 1.3.6.1.4.1.14179.2.1.4.1.4.170.187.204.0.0.17 octet_str 0xF0D805DAB038
override 1.3.6.1.4.1.14179.2.1.4.1.7.170.187.204.0.0.17 octet_str "Staff"
override 1.3.6.1.4.1.14179.2.1.4.1.25.170.187.204.0.0.17 integer 12
override 1.3.6.1.4.1.14179.2.1.6.1.1.170.187.204.0.0.17 integer -67
override 1.3.6.1.4.1.14179.2.1.6.1.2.170.187.204.0.0.17 counter 4000
override 1.3.6.1.4.1.14179.2.1.6.1.3.170.187.204.0.0.17 counter 90000
override 1.3.6.1.4.1.14179.2.2.1.1.3.240.216.5.218.176.56 octet_str "AP-Lobby"
""")
    agent = subprocess.Popen([snmpd, "-f", "-Lo", "-C", "-c", conf, "-p", os.path.join(d, "pid"), "-M", d],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    try:
        for name, dev in [("v2c", {"version": "2c", "community": "orbittest"}),
                          ("v3 SHA + AES", {"version": "3", "user": "orbitv3", "auth": "SHA", "auth_password": "testauth-1234",
                                            "priv": "AES", "priv_password": "testpriv-1234"})]:
            live = R.Snmp([{"host": "127.0.0.1", "port": port, **dev}])
            live._hostnames = lambda ips: {}
            got = {r["mac_address"]: r for r in live.read()}
            c = got.get("AA:BB:CC:00:00:11") or {}
            check(c.get("signal_strength") == -67 and c.get("ap_name") == "AP-Lobby" and c.get("iface") == "RF_2GHZ"
                  and c.get("rx_stats", {}).get("bytes") == 90000, f"{name}: Wi-Fi client read from the agent")
            live.close()
        bad = R.Snmp([{"host": "127.0.0.1", "port": port, "version": "3", "user": "orbitv3", "auth": "SHA",
                       "auth_password": "not-the-password", "priv": "AES", "priv_password": "testpriv-1234"}])
        try:
            bad.read()
            check(False, "v3 with a wrong password is refused")
        except R.SourceError as e:
            check("refused the SNMP v3 password" in str(e), "v3 with a wrong password is refused, in plain words")
        bad.close()
    finally:
        agent.terminate()
        agent.wait(5)

print("\n" + ("All passed." if not failures else f"{len(failures)} failed:\n  " + "\n  ".join(failures)))
sys.exit(1 if failures else 0)
