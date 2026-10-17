# Orbit

A home network dashboard for Starlink. It shows how your connection is doing, which devices are online, and who's using the internet. It keeps a history Starlink itself doesn't keep: the dish only remembers the last 15 minutes.

Orbit runs on a computer at home and you open it in a browser. It reads from your dish and router over their local connection, so it keeps working when the internet is down.

`Orbit Home Network.html` is the original design prototype, which runs on simulated data. The real app is in `orbit/` (the server) and `web/` (the page).

## What you need

- A Starlink dish and the Starlink router. Tested with the Standard kit and Router Gen 3.
- A computer on your Starlink Wi-Fi with Python 3.10 or newer. A Mac that sleeps works, but history has gaps while it's asleep. A Raspberry Pi or other always-on computer is better.

If your Starlink router is in bypass mode, Orbit still sees the dish. To see your devices, connect your own router: see [Your own router](#your-own-router-bypass-mode).

## Install

Paste this into Terminal on the Mac that stays home:

```sh
curl -fsSL https://raw.githubusercontent.com/manu-tech-code/orbit/HEAD/scripts/install.sh | bash
```

It downloads the latest release, checks its SHA-256, makes a Python environment, and sets up the background service. Run it again any time to update; your history in `data/` is kept.

| Option (before the command) | What it does |
|---|---|
| `ORBIT_LAN=1` | Lets phones and laptops on your Wi-Fi open the dashboard. |
| `ORBIT_VERSION=v1.0.1` | Installs that release instead of the latest. |
| `ORBIT_INSTALL_DIR=~/Orbit` | Installs somewhere else (default `~/Applications/Orbit`). |
| `ORBIT_NO_SERVICE=1` | Installs the code but doesn't start the background service. |
| `ORBIT_NO_APP=1` | Skips the Mac app (`/Applications/Orbit.app`). |

You can also download the tarball from [Releases](https://github.com/manu-tech-code/orbit/releases/latest) and follow **Set up and run** below.

## Set up and run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m orbit
```

Then open http://localhost:8787. Press Ctrl+C to stop; history is saved as it goes.

Options:

| Option | What it does |
|---|---|
| `--lan` | Lets phones and laptops on your Wi-Fi open the dashboard. Each one signs in once with an access code, which Orbit prints when it starts. This computer never needs the code. |
| `--new-code` | Makes a new access code and signs every other device out. |
| `--port 8787` | Uses a different port. |
| `--db data/orbit.db` | Keeps history somewhere else. |
| `--dish 192.168.100.1:9200`, `--router 192.168.1.1:9000` | Uses different dish or router addresses. |

### Keep it running in the background (Mac)

History is only as complete as the time Orbit spends running. To run it as a background service:

```bash
scripts/service.sh install
```

The service:
- does not start when you log in: you start it with `scripts/service.sh start` or from the Orbit Mac app (add `--login` to the install command to make it start at login too);
- restarts within seconds if it crashes;
- keeps the Mac awake while it's plugged in. On battery the Mac sleeps as usual, and closing the lid always makes it sleep, so leave it plugged in with the lid open.

Add `--lan` to the install command to let phones open it. The access code then appears in `scripts/service.sh status`.

| Command | What it does |
|---|---|
| `scripts/service.sh status` | Shows whether it's running and recording. |
| `scripts/service.sh logs` | Shows its log. |
| `scripts/service.sh restart` | Restarts it. Do this after changing Orbit's code. |
| `scripts/service.sh stop` / `start` | Stops it until you start it again, and starts it again. |
| `scripts/service.sh uninstall` | Removes the service. Your history stays. |

Only one copy of Orbit can use a history at a time, because two would count every device's usage twice. If the service is running, `python -m orbit` in a terminal says so and quits.

After the Mac sleeps:
- **Whole-home usage:** Orbit fills in the last 15 minutes from the dish's own history.
- **Per-device usage:** it carries across the gap for devices that stayed connected, because the router's counters kept running.

To check what your own dish and router report, run the read-only probe. It writes a redacted report to `scripts/probe_report.json`.

```bash
.venv/bin/python scripts/probe_starlink.py
```

## Mac app

`mac/` holds a native Mac app, built in SwiftUI. It has a window with the same pages as the web version, including the Overview's live map of your devices, and a menu bar icon that shows Starlink's latency. Click the icon for a quick look and a speed test button. On the map, point at a device for its details, or click it to open it on the Devices page.

The app shows what the Orbit server on this Mac records, so the server needs to be running (`scripts/service.sh status`). The web version keeps working alongside it.

**Start and stop the server from the app.** The power button in the window's toolbar (and the menu bar panel and Settings) starts, restarts and stops the server, which runs as a background service for your account (see [Keep it running in the background](#keep-it-running-in-the-background-mac)). When the server isn't running, the app shows "Orbit is stopped" with a **Start Orbit** button. Stopping asks first, because while Orbit is stopped it records nothing: no per-device usage, dropouts or alerts, and the tripwire stops watching. Stopping takes a few seconds while the server saves what it has.
- It does the same thing as `scripts/service.sh start`, `stop` and `restart`, so the two always agree.
- It works only when the app is set to a server on this Mac and the service has been set up once with `scripts/service.sh install`.
- The service doesn't start by itself when you log in, so after a restart of the Mac the app shows "Orbit is stopped" until you press **Start Orbit**. (Its launch file lives in `~/Library/Application Support/Orbit`, not in `LaunchAgents`, which macOS would start at login. `scripts/service.sh install --login` puts it in `LaunchAgents` instead.)
- A server you started yourself in a terminal isn't managed by the app. Stop that one in the terminal.

The app sends a notification when a device or person uses up most or all of their data allowance. Notifications come from the app, so they appear only while it runs; its menu bar icon keeps it running after you close the window.

Build it and install it in Applications:

```bash
mac/build.sh --install
```

Run this again after changing the app's code. If Orbit is open, it quits and reopens on the new version.

To try a change without installing it, run `mac/build.sh` on its own. It builds `mac/build/Orbit.app`.

In the app's settings (⌘,) you can make it open when you log in.

## Your own router (bypass mode)

When the Starlink router is in bypass mode, another router runs your network and the Starlink router can't see your devices. Orbit can read the device list from that router instead. The dish is still read as usual.

| Brand | What Orbit reads | What you need |
|---|---|---|
| **MikroTik** (RouterOS 7) | Every device (DHCP and the address table), Wi-Fi signal if the MikroTik runs the Wi-Fi, data per device from Kid Control | The REST API (IP › Services › www-ssl) and a read-only user |
| **Grandstream** (GWN.Cloud or GWN Manager) | Wi-Fi devices with their signal, access point and data | API Developer mode: an App ID and Secret Key |
| **SNMP**: any firewall, router or switch (Sophos, Cisco, Fortinet, HP, Ubiquiti…), and Cisco Wi-Fi controllers | Every device the firewall or router knows, on every network section; which switch port a wired device is on and its data; Wi-Fi devices' signal, access point and data from a Cisco controller (AireOS, Catalyst 9800, Embedded Wireless Controller) | SNMP v2c (a community) or v3 (user, SHA or SHA-256, AES), with this computer allowed to ask |

Use both together, for example a MikroTik router with Grandstream access points: Orbit merges them by MAC address, takes each device's data from the MikroTik, and Wi-Fi signal and access point from Grandstream. A device no access point reports shows as wired.

1. On the MikroTik, make a read-only user:
   ```
   /user group add name=orbit-ro policy=read,api,rest-api,!write,!policy
   /user add name=orbit group=orbit-ro password=<a long password>
   ```
   For data per device, open **IP › Kid Control**, add a profile with no time limits, and add your devices to it. Kid Control only counts; it blocks nothing without limits.
2. In GWN.Cloud or GWN Manager, open your account menu › **API Developer**, switch it on and copy the App ID and Secret Key.
3. For SNMP, ask whoever runs the network for read-only SNMP access for this computer: the v3 user and its two passwords (or a v2c community), and to add this computer's address to the devices' SNMP access lists. Devices that only accept SNMP from a monitoring server stay silent otherwise.
4. Put the settings in `data/routers.json` next to your history, and make it readable by you only (`chmod 600 data/routers.json`), because it holds passwords:
   ```json
   {
     "mikrotik":    {"host": "192.168.88.1", "username": "orbit", "password": "…", "insecure_tls": true},
     "grandstream": {"url": "https://www.gwn.cloud", "app_id": "…", "secret_key": "…", "network": "Home"},
     "snmp": [
       {"host": "192.168.1.1", "version": "3", "user": "monitor", "auth": "SHA256", "auth_password": "…", "priv": "AES", "priv_password": "…"},
       {"host": "192.168.1.2", "version": "3", "user": "monitor", "auth": "SHA", "auth_password": "…", "priv": "AES", "priv_password": "…"},
       {"host": "192.168.1.3", "version": "2c", "community": "…"}
     ]
   }
   ```
   Leave out what you don't have. List each SNMP device separately (the firewall, each switch, the Wi-Fi controller); `auth` is MD5, SHA, SHA224, SHA256, SHA384 or SHA512, `priv` is AES, AES192, AES256 or none. `insecure_tls` trusts the router's own self-made certificate; only set it for your own router. Europe GWN accounts use `https://eu.gwn.cloud`; a GWN Manager uses its own address. `network` picks one GWN network; leave it out for all.
5. Restart Orbit (`scripts/service.sh restart`). It prints where devices come from, and the Starlink page shows each one's status under **Your router and Wi-Fi**. `--routers <file>` uses a settings file somewhere else.

What changes:
- Orbit reads the MikroTik every 5 seconds, and Grandstream and SNMP devices every 30 seconds, rather than the Starlink router every 2 seconds. Live speeds are worked out between those reads.
- With SNMP, the firewall's address table lists devices on every network section, not only yours. A device counts as connected while a switch or the Wi-Fi controller has seen it recently; the address table alone can remember a device for hours. A wired device alone on a switch port gets that port's data counters as its usage; devices behind another switch or access point don't. Names come from your network's reverse DNS.
- Devices are identified by their MAC address. Merged devices, owners, allowances and hiding work as before.
- Kid Control counts since the router last started; Orbit notices a restart and carries on. Like the Starlink router, it includes traffic inside your home.
- Speed tests, block schedules and the router's Wi-Fi security settings come from the Starlink router, so they aren't available in bypass mode.
- The dish still has to be reachable at 192.168.100.1: on a MikroTik, add a route to it through the internet port if Orbit can't reach the dish.

`scripts/test_routers_other.py` checks the MikroTik and Grandstream readers against made-up replies, and `scripts/test_snmp.py` checks the SNMP merging the same way (and reads a throwaway local agent over v2c and v3 if net-snmp's `snmpd` is installed), without touching the network.

## What it shows

- **Overview:** a health headline, latency, current use, data used today, dropouts, and a live map of devices by signal strength. It also makes suggestions, such as weak signals, unnamed devices, restarts and big downloads.
- **Devices:** every device the router has seen, with live speed, data used today, band and signal.
  - Open a device to rename it, set its type, choose its owner, add notes or give it a data allowance.
  - **Select** lets you change several devices at once: owner, type, mark as known, hide, or forget the offline ones. On the Mac, select several rows, or right-click.
  - **Hide** a device you don't care about, such as a smart plug. It leaves the device list, the Overview map and suggestions, and its comings and goings leave Activity. Its data still counts everywhere. Find it under **Hidden**, or by searching.
  - **Merge** two entries for the same device. See [Merged devices](#merged-devices).
- **People:** devices grouped by who uses them. A person can have a data allowance for all their devices together.
- **Usage:** data used per hour or day, average use and latency over time, and breakdowns by person and by device, over 24 hours, 7 days or 30 days.
- **Starlink:** the dish's own obstruction map, dropouts with their cause, dish and router details, and speed tests. It also shows what the router and dish report about themselves:
  - **Connection chain:** latency and drops from the router to the dish, to Starlink's gateway and on to the internet, so you can tell a home problem from a Starlink one. Also how long your public address has stayed the same, and the power feed to the router.
  - **Dish pointing:** the gap between where the dish points and where it wants to point, GPS satellites, and the dish's self-test.
  - **Updates:** the running version and update state of the dish and router.
- **Speed test:** the router measures download, then upload, for about 30 seconds. It uses a few hundred MB of data. Results are kept, and "In use now" is measured against the last one.
- **Security:** a map of who can reach whom, and a tripwire. See [Security](#security).
- **Floor plan:** draw your walls, place your devices, and see which ones are weaker than your walls explain. See [Floor plan](#floor-plan).
- **Activity:** new devices, devices connecting and disconnecting, allowances running out, dropouts, software updates, and the changes you made.

### Data allowances

Give a device or a person an amount of data a day or a month. A person's allowance counts all their devices together.

- Orbit tells you once when 80% is used, and once when all of it is. The alert goes to Activity, shows as a toast on any open dashboard, and appears as a notification from the Mac app while it runs. The first time, macOS asks whether Orbit may send notifications.
- What was already used when you set an allowance doesn't alert, because you see it as you set it.
- Days and months are calendar days and months on this computer.
- Orbit can't cut anyone off: your router only accepts that from the Starlink app. To pause a device, use the app.
- Per-device data comes from the router, so it includes traffic inside your home, such as casting to a TV.

### Merged devices

Phones make up a private address for each Wi-Fi network, and now and then a new one. The router then gives the phone a new client ID, and Orbit sees a new device. That also happens when a phone moves between your main and guest Wi-Fi.

- When a new device has the same name as one that isn't connected, Orbit asks whether it's the same device.
- Merging makes the two entries one device, with their usage history added together. Either connection then counts as that device, and its coming back doesn't raise a new-device alert.
- The merged device keeps the name and owner of the entry Orbit knew first. It keeps the ID of the one connected most recently, so live readings carry on.
- In a device's details you can separate a merged entry again. It becomes its own device from then on. Its past use stays with the device it was merged into.
- Two entries that are both connected at once are two devices, so Orbit won't merge them.

## Security

The **Security** page answers two questions: which devices can talk to each other, and is anything knocking on this Mac?

**Who can reach whom** is worked out from your router's settings. The map draws each Wi-Fi network as a zone around the router, with walls between them.
- On the main network every device can reach every other one. That is normal.
- A guest network with *client isolation* on keeps each guest apart from the others, and from your main network. Each network also has its own address range, so a guest can't reach your devices at all.
- A network with no password (an open one) is flagged. Orbit warns you in Activity, and on the Mac with a notification, if client isolation is turned off on a guest network, if a network loses its password, or if a new open network appears.
- Click a device to see what it can reach.
- **Router settings:** what the router is set to, read-only: each network's protection (WPA2 or none), DNS servers, secure DNS, bypass mode, channels. Orbit warns you in Activity if the DNS servers change, secure DNS is turned off, the router goes into bypass mode, the router loses its link to the dish, or its power feed reports a fault.

**The tripwire** is a set of decoy doors on this Mac (ports such as 445 file sharing, 3389 remote desktop, 5900 screen sharing, 21 and 23, plus three of your own choosing: 1034, 7765 and 4967). Nothing at home should connect to them, so any connection is a probe. The exception: devices on your main Wi-Fi that you've marked as known are allowed to talk to this Mac, so they never raise an alert. Mark a device as known on the Devices page; an unknown or guest-side device is still flagged. Orbit notes who knocked and hangs up, without reading or answering. Three different doors within two minutes is reported as a scan.
- It names the device (or the address, if the device isn't known) and draws the probe on the map. Orbit tells you once per source every 10 minutes, so a busy scanner doesn't flood Activity.
- It needs no administrator rights. If the macOS firewall is on, it may ask once whether to allow incoming connections for Python. Choose Allow.
- If something on the Mac already uses a door (for example Remote Login on port 22), Orbit can't watch that one and says so.
- **To test it:** from a phone on your Wi-Fi, open `http://<this Mac's address>:1034`. The Security page shows the address and a door to try.
- Turn it off with the switch on the Security page, or start Orbit with `ORBIT_TRIPWIRE=0`. `ORBIT_TRIPWIRE_PORTS=8445,8022` picks other doors, which a second copy for development needs.

**What it can't see:**
- Connections to other devices. The router doesn't report them, so only this Mac is watched.
- Half-open scans. A scan that never finishes the connection (nmap's default) doesn't reach a decoy door.
- Anything inside a guest network. Isolation drops it before anyone can see it. Guests also can't reach this Mac, so they never trigger the tripwire.
- The router's firewall log. The router refuses to share it.

`scripts/test_tripwire.py` and `scripts/test_router.py` check the tripwire and the router alerts without touching the network.

## Floor plan

Your router can't see walls, only how strong each device's signal is. The **Floor plan** page lets you draw the walls and shows where something is in the way.

- Draw your outline and walls, choose what each is made of (plasterboard, glass, brick, concrete, metal or mirror), put the router where it is, and drag each device to where it really is. Add room names if you like.
- For each device, Orbit predicts the signal from the distance and the walls between it and the router, then tunes that guess using all your devices (it needs at least 3 placed). The gap between what a device measures and what the plan predicts is shown: a device much weaker than the walls explain usually has something in the way, such as a metal cabinet, a mirror or a thicker wall than you drew.
- **Coverage** shows the predicted signal room by room, for 2.4 or 5 GHz.
- It's an estimate, good to about 6 dB. Wall losses are typical values, not measurements of your walls.
- The plan is kept in `data/orbit.db` (as a single saved setting) and shared by the web and Mac apps. Both write it through `/api/plan`. `scripts/test_plan.py` checks what is accepted and refused.

## How it measures

**How hard Orbit works the router.** The router is a small computer that also carries your Wi-Fi, so Orbit is gentle with it. It reads the client list every 2 seconds and the router's full status only every 10 minutes (`ORBIT_ROUTER_STATUS_EVERY`, in seconds). On 5 October 2026 the router began restarting itself intermittently within an hour of a build that asked for its full status every 30 seconds and for radio statistics and the router's game-server pings every minute. Orbit no longer asks for the radio statistics or the pings at all.

- **Whole home:** the dish reports download and upload speed every second. Orbit saves each reading and adds them up for daily totals.
- **Per device:** the router reports a running byte count for each device. Orbit reads it every 2 seconds and records the difference.
  - The counter restarts when a device reconnects, and Orbit accounts for that.
  - The counter includes traffic that stays inside your home, such as casting to a TV. So per-device totals can add up to more than the whole-home total.
- **Live speed per device:** worked out from those counters over the last 6 seconds.
- **Dropouts:** come from the dish, with its own reason for each one.

## What Orbit can and can't change

Orbit can send only the requests listed in `orbit/starlink.py`. Most are reads (`READ_ONLY`). The only action (`ACTIONS`) is `start_speedtest`, which asks the router to run its own speed test.

When tested on a Starlink Router Gen 3 (October 2026), the router allowed every read and the speed test, but refused settings changes with `PERMISSION_DENIED`. Those need Starlink's signing keys, so only the Starlink app can make them. That rules out, from Orbit:
- pausing a device
- block schedules, such as bedtime
- speed limits
- renaming devices on the router itself

Orbit does show what you set in the app. A device that's paused or inside a schedule shows as **Blocked**, and its details say whether it has a schedule.

Orbit's own settings stay in Orbit: the names, owners, notes, hidden devices, merges and data allowances you set live in `data/orbit.db`, not on the router. Allowances only warn.

Orbit never restarts, stows or reconfigures the dish or router.

## Privacy and security

- **What's stored:** history, and the names, notes and allowances you set, live in `data/orbit.db` on this computer. Tripwire knocks (time, address, door) are kept for 30 days. Orbit never stores or shows your Wi-Fi password.
- **Which addresses work:** the server answers only on this computer's own addresses. That blocks other websites from reading it through DNS rebinding. Changes must come from the dashboard page itself.
- **Signing in:** with `--lan`, other devices sign in with the access code. A signed-in device keeps a session cookie for a year, which page scripts can't read. Orbit stores only a hash of each session. After 5 wrong codes from one address, that address waits 10 minutes.
- **MAC addresses:** the router hides the second half of every device's MAC address, so Orbit identifies devices by the router's client ID.
- **How long things are kept:**
  - Per-second readings: 2 days.
  - Per-minute summaries: 400 days.
  - Per-device minutes: 35 days.
  - Per-device daily totals: forever.

## Releases

The version is written once, in `orbit/__init__.py`. Merging into `main` runs `.github/workflows/release.yml`, which tags `v<version>` and drafts a GitHub release; from a Mac, `scripts/release.sh --upload` builds the tarball and the Mac app, attaches them with their checksums, and publishes. Bump `__version__` in a pull request to release again. `scripts/install.sh` reads the latest release, so it always installs what was published.

## Layout

```
orbit/            the server (Python, standard library plus the Starlink gRPC library)
  starlink.py     access to the dish and router (reads, plus the speed test)
  routers.py      device lists from your own router in bypass mode (MikroTik, Grandstream, SNMP)
  collector.py    polls them, works out usage, runs speed tests, keeps the live picture
  tripwire.py     the decoy doors on this Mac that catch probes
  store.py        SQLite history
  server.py       the web server, JSON API and live stream
  auth.py         sign-in for other devices when running with --lan
web/              the dashboard (React and htm, no build step; libraries in web/vendor)
mac/              the native Mac app (SwiftUI and Swift Charts), built with mac/build.sh
scripts/          the probe, install.sh (installs a release), release.sh (builds one),
                  service.sh for running Orbit in the background on a Mac,
                  test_devices.py (device management), test_tripwire.py (the tripwire), test_router.py (router alerts),
                  test_plan.py (floor plan saving), test_routers_other.py (MikroTik and Grandstream) and test_snmp.py (SNMP), which check without touching the network
```

## License

MIT: use it, change it, share it. See [LICENSE](LICENSE).
