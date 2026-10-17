/* Starlink: the link to space, what the dish sees, dropouts, and the dish and router details. */
import {
  html, useEffect, useRef, Fragment, sum, fmtMbps, fmtDur, fmtSecs, fmtClock, fmtAgo, clockOfMinutes, plural,
  Icon, Stat, Spark, OutageStrip,
} from './lib.js';
import { usePoll } from './data.js';
import { DishChip } from './overview.js';

export const routerGen = hw => (/^v(\d+)/.test(hw || '') ? 'Gen ' + hw.match(/^v(\d+)/)[1] : hw || 'Router');
export function kitName(d) {
  const hw = d.hardware || '';
  if (/mini/i.test(hw)) return 'Mini kit';
  if (/^hp/i.test(hw)) return 'High Performance kit';
  if (d.actuated) return 'Standard Actuated kit';
  if (/^rev4/i.test(hw)) return 'Standard kit';
  return 'Starlink dish';
}
const SNOW = { AUTO: 'Automatic', ALWAYS_ON: 'Pre-heat', ALWAYS_OFF: 'Off' };

function LinkPath({ s, x }) {
  const d = s.dish, r = s.router;
  const routerUp = r.reachable !== false, dishUp = d.reachable !== false, internet = d.online === true;
  const hops = [
    { icon: 'monitor-smartphone', name: 'Your devices', sub: `${x.conn.length} connected` },
    { icon: 'router', name: 'Router', sub: routerUp ? `${routerGen(r.hardware)} · Wi-Fi 6` : 'Not answering', down: !routerUp },
    { icon: 'satellite-dish', name: 'Dish', sub: !dishUp ? 'Not answering' : d.online === false ? 'Offline' : kitName(d), down: !dishUp || d.online === false },
    { icon: 'satellite', name: 'Satellite', sub: 'About 550 km up', sat: true, down: !internet },
    { icon: 'radar', name: 'Ground station', sub: 'Back down to earth', down: !internet },
    { icon: 'globe', name: 'Internet', sub: 'Websites and apps', down: !internet },
  ];
  const wires = [
    { label: 'Wi-Fi', d: '1.3s', down: !routerUp },
    { label: d.eth_mbps ? `Cable · ${d.eth_mbps >= 1000 ? d.eth_mbps / 1000 + ' Gbps' : d.eth_mbps + ' Mbps'}` : 'Cable', d: '1s', down: !dishUp },
    { label: 'Radio · ~550 km', d: '2.2s', down: !internet },
    { label: 'Radio · ~550 km', d: '2.2s', down: !internet },
    { label: 'Fiber', d: '1.2s', down: !internet },
  ];
  return html`<section className="sky sky-stars" aria-label="How your internet reaches you">
    <div style=${{ padding: '18px 24px 0', position: 'relative' }}><h2 className="sky-title">The trip your data takes</h2><div className="sky-sub">From your home to space and back, live</div></div>
    <div className="path">
      ${hops.map((h, i) => html`<${Fragment} key=${h.name}>
        <div className=${'hop' + (h.sat ? ' is-sat' : '') + (h.down ? ' is-down' : '')}>
          <span className="hop-ic"><${Icon} name=${h.icon} size=${22} /></span>
          <span className="hop-txt"><span className="hop-name">${h.name}</span><span className="hop-sub">${h.sub}</span></span>
        </div>
        ${i < wires.length ? html`<div className=${'wire' + (wires[i].down ? ' is-down' : '')} aria-hidden="true">
          <span className="wire-label">${wires[i].down ? 'No signal' : wires[i].label}</span>
          <span className="wire-line">${wires[i].down ? null : html`<i className="packet" style=${{ '--d': wires[i].d }} /><i className="packet p2" style=${{ '--d': wires[i].d }} />`}</span>
        </div>` : null}
      <//>`)}
    </div>
    <p className="path-foot">${internet && d.latency_ms != null
      ? html`Every tap travels up to a satellite about 550 km overhead, down to a ground station, and out to the internet. The round trip takes <b className="tnum">${Math.round(d.latency_ms)} ms</b> right now, about a tenth of the time it takes to blink.`
      : 'The link to space is down right now. Your devices can still reach each other over Wi-Fi.'}</p>
  </section>`;
}

/* The dish's own map of the sky: a grid of signal quality from 0 (blocked) to 1 (clear); -1 means not measured yet.
   The middle is straight up and the edge is the horizon at the map's lowest elevation. */
function SkyMap({ map, obstructed }) {
  const ref = useRef(null);
  const n = map ? map.rows : 0;
  useEffect(() => {
    const c = ref.current;
    if (!c || !map || !map.rows) return;
    const px = 3;
    c.width = map.cols * px;
    c.height = map.rows * px;
    const ctx = c.getContext('2d');
    const css = getComputedStyle(c);
    const col = { clear: css.getPropertyValue('--sky-accent').trim() || '#a59bff', part: css.getPropertyValue('--warn').trim() || '#fab219', blocked: css.getPropertyValue('--crit').trim() || '#d03b3b' };
    ctx.clearRect(0, 0, c.width, c.height);
    for (let r = 0; r < map.rows; r++) {
      for (let k = 0; k < map.cols; k++) {
        const v = map.snr[r * map.cols + k];
        if (v < 0) continue;
        ctx.globalAlpha = v >= 0.9 ? 0.55 : 0.95;
        ctx.fillStyle = v >= 0.9 ? col.clear : v >= 0.5 ? col.part : col.blocked;
        ctx.fillRect(k * px, r * px, px, px);
      }
    }
  }, [map]);
  if (!map) return html`<div className="empty" style=${{ color: 'var(--sky-ink-2)' }}>Loading the dish’s map…</div>`;
  const R = n / 2, maxTheta = map.max_theta_deg || 80;
  let inside = 0, seen = 0, low = 0;
  for (let r = 0; r < map.rows; r++) for (let k = 0; k < map.cols; k++) {
    if ((r + 0.5 - R) ** 2 + (k + 0.5 - R) ** 2 > R * R) continue;
    inside++;
    const v = map.snr[r * map.cols + k];
    if (v >= 0) { seen++; if (v < 0.5) low++; }
  }
  const ring = el => ((90 - el) / maxTheta) * 50;  // radius, as % of the map's width
  const minEl = Math.round(map.min_elevation_deg || 90 - maxTheta);
  return html`<${Fragment}>
    <div className="omap">
      <canvas ref=${ref} role="img" aria-label=${`What your dish sees: ${Math.round((seen / Math.max(1, inside)) * 100)}% of the sky measured so far, ${low ? low + ' blocked patches' : 'nothing blocked'}.`} />
      <svg viewBox="0 0 100 100" aria-hidden="true">
        <circle cx="50" cy="50" r="50" className="omap-ring" />
        <circle cx="50" cy="50" r=${ring(30)} className="omap-ring" />
        <circle cx="50" cy="50" r=${ring(60)} className="omap-ring" />
        <circle cx="50" cy="50" r="1.4" style=${{ fill: 'var(--sky-ink)' }} />
      </svg>
      <span className="omap-n">N</span><span className="omap-e">E</span><span className="omap-s">S</span><span className="omap-w">W</span>
    </div>
    <div className="orbit-legend" style=${{ justifyContent: 'center' }}>
      <span><span className="sw" style=${{ '--k': 'var(--sky-accent)' }} />Clear</span>
      <span><span className="sw" style=${{ '--k': 'var(--warn)' }} />Weak</span>
      <span><span className="sw" style=${{ '--k': 'var(--crit)' }} />Blocked</span>
      <span><span className="sw" style=${{ '--k': 'var(--sky-2)', boxShadow: 'inset 0 0 0 1px var(--sky-line-2)' }} />Not measured yet</span>
    </div>
    <p className="sky-sub" style=${{ textAlign: 'center', position: 'relative' }}>${Math.round((seen / Math.max(1, inside)) * 100)}% of the sky measured so far. The dish fills this in as satellites pass, so it’s patchy for a while after a restart. Edge = ${minEl}° above the horizon, middle = straight up.${obstructed > 0.0005 ? ` Blocked ${(obstructed * 100).toFixed(2)}% of the time.` : ' Nothing has blocked it.'}</p>
  <//>`;
}

function OutagesCard({ s }) {
  const out = [...s.outages].sort((a, b) => b.start - a.start);
  const secs = sum(out, o => o.duration_s);
  return html`<section className="card">
    <div className="card-head"><div><h2 className="card-title">Dropouts in the last 24 hours</h2><div className="card-sub tnum">${plural(out.length, 'dropout')} · ${fmtSecs(secs)} offline · ${(100 - (secs / 86400) * 100).toFixed(2)}% online</div></div></div>
    <${OutageStrip} outages=${s.outages} now=${s.now} knownSince=${s.known_since} />
    <div className="legend" style=${{ marginTop: '10px' }}>
      <span><span className="sw" style=${{ '--k': 'color-mix(in srgb, var(--good) 34%, var(--surface))' }} />Online</span>
      <span><span className="sw" style=${{ '--k': 'var(--serious)' }} />Dropped under 10 s</span>
      <span><span className="sw" style=${{ '--k': 'var(--crit)' }} />Dropped 10 s or more</span>
      ${s.known_since && s.now - s.known_since < 86400e3 ? html`<span><span className="sw nodata-sw" />Before Orbit’s records start</span>` : null}
    </div>
    <div style=${{ marginTop: '14px' }}>${out.slice(0, 12).map(o => html`<div className="act-row" key=${o.start}>
      <span className=${'act-ic ' + (o.duration_s >= 10 ? 'security' : 'warn')}><${Icon} name=${o.cause === 'BOOTING' ? 'rotate-cw' : 'triangle-alert'} size=${14} /></span>
      <div className="grow"><div className="act-title">${o.cause === 'BOOTING' ? `Restarted, back after ${fmtSecs(o.duration_s)}` : `Dropped for ${o.duration_s < 1 ? 'under a second' : fmtSecs(o.duration_s)}`}</div><div className="act-detail">${o.text}</div></div>
      <span className="act-time tnum">${fmtClock(o.start)}</span></div>`)}
      ${out.length > 12 ? html`<p className="muted" style=${{ fontSize: '13px', paddingTop: '6px' }}>And ${plural(out.length - 12, 'more')}.</p>` : null}
      ${out.length === 0 ? html`<div className="empty">No dropouts. Nice.</div>` : null}</div>
    <p className="muted" style=${{ fontSize: '13px', marginTop: '10px' }}>Dropouts under 10 seconds rarely matter, except on video calls and in online games.</p>
  </section>`;
}

/* Shared by the cards below. Latency words are for everyday use and games alike; drops are the share of test pings that got no answer. */
const latTone = ms => (ms == null ? '' : ms < 50 ? 'good' : ms < 100 ? 'warn' : 'crit');
const latWord = ms => (ms == null ? 'Not measured' : ms < 50 ? 'Great' : ms < 100 ? 'Fine' : 'Slow');
const dropTone = f => (f == null ? '' : f < 0.02 ? 'good' : f < 0.05 ? 'warn' : 'crit');
const pct = f => (f * 100).toFixed(f > 0 && f < 0.1 ? 1 : 0) + '%';
const fmtMs = ms => (ms == null ? '–' : ms < 10 ? ms.toFixed(1) : Math.round(ms)) + ' ms';
const noteStyle = { fontSize: '13px', marginTop: '12px' };

/* Software update, in plain words. state is the raw word from the dish or router; progress runs 0 to 1. */
function updateWords({ state, pending, progress, ready, waitS }) {
  if (ready) return 'Installed and waiting for a restart';
  const st = String(state || '');
  if (waitS != null && waitS > 0) return 'Ready, waiting for the overnight slot';
  if (pending && progress > 0 && progress < 1) return `Downloading ${Math.round(progress * 100)}%`;
  if (pending) return 'Update on its way';
  if (/FETCH|DOWNLOAD/i.test(st)) return progress > 0 && progress < 1 ? `Downloading ${Math.round(progress * 100)}%` : 'Downloading an update';
  if (/REBOOT|PENDING|READY|INSTALL/i.test(st)) return 'Ready to install';
  if (!st || /^(IDLE|NO_UPDATE_REQUIRED|UP_TO_DATE)$/i.test(st)) return 'Up to date';
  const w = st.replace(/_/g, ' ').toLowerCase();
  return w.charAt(0).toUpperCase() + w.slice(1);
}

function ChainStep({ n, name, sub, ms, drop }) {
  return html`<div className="setting"><div className="setting-text"><div className="setting-title">${n}. ${name}</div><div className="setting-sub">${sub}</div></div>
    <div className="chips">${ms == null ? html`<span className="chip chip-muted">Not measured</span>` : html`<span className=${'chip chip-' + latTone(ms)}>${latWord(ms)} · <span className="tnum">${fmtMs(ms)}</span></span>`}
      ${drop == null ? null : html`<span className=${'chip chip-' + dropTone(drop)}><span className="tnum">${pct(drop)}</span> lost, last 5 min</span>`}</div></div>`;
}

/* Router to dish, dish to Starlink's ground gateway, then the internet. Each is measured from the router, so each step includes the ones before it. */
function ConnectionChain({ s }) {
  const l = s.router.link;
  if (!l) return null;
  const poe = l.poe || {};
  return html`<section className="card">
    <div className="card-head"><div><h2 className="card-title">Connection chain</h2><div className="card-sub">Your router tests each step from your home to the internet</div></div></div>
    <${ChainStep} n=${1} name="Router to dish" sub="The cable on your roof. This part is at home." ms=${l.dish_ms} drop=${l.dish_drop_5m} />
    <${ChainStep} n=${2} name="To Starlink’s ground gateway" sub="Up to a satellite and back down to Starlink’s station." ms=${l.pop_ms} drop=${l.pop_drop_5m} />
    <${ChainStep} n=${3} name="To the internet" sub="All the way out to a test server." ms=${l.internet_ms} drop=${l.internet_drop_5m} />
    <p className="muted" style=${{ fontSize: '13px', margin: '4px 0 6px' }}>If the first step is slow, the problem is at home. If a later step is slow, it’s Starlink or beyond. Each step counts the time of the ones before it.</p>
    <div className="kv">
      <div>Same public address for</div><div>${l.public_ip_age_s != null ? fmtDur(l.public_ip_age_s * 1000) : '–'}<div className="muted" style=${{ fontSize: '13px' }}>Your address on the internet. It changes now and then, and Orbit tells you when it does.</div></div>
      <div>Power to the dish</div><div>${poe.on == null ? '–' : poe.on ? `On${poe.power_w ? `, ${poe.power_w.toFixed(1)} W` : ''}` : 'Off'}${poe.faults ? html` <span className="chip chip-crit">${plural(poe.faults, 'fault')}</span>` : null}</div>
      <div>Router to dish link</div><div>${l.no_wan_link ? html`<span className="chip chip-crit">No link. Check the cable</span>` : 'Connected'}</div>
    </div>
  </section>`;
}

function PointingCard({ s }) {
  const d = s.dish, p = d.pointing, gps = d.gps, chk = d.check;
  if (!p && !gps && !chk) return null;
  const side = p ? Math.abs(p.az - p.want_az) : null, up = p ? Math.abs(p.el - p.want_el) : null;
  const off = p ? Math.hypot(side, up) : 0;
  const test = chk && chk.self_test;
  return html`<section className="card">
    <div className="card-head"><div><h2 className="card-title">Dish pointing</h2><div className="card-sub">Where the dish aims compared with the best direction</div></div>
      ${p ? html`<span className=${'chip ' + (off > 15 ? 'chip-warn' : 'chip-good')}>${off > 15 ? 'Quite far off' : 'Close enough'}</span>` : null}</div>
    <div className="kv">
      ${p ? html`<${Fragment}><div>Sideways gap</div><div className="tnum">${side.toFixed(1)}°</div>
        <div>Up and down gap</div><div className="tnum">${up.toFixed(1)}°</div>
        <div>Position</div><div>${p.settled ? 'Settled' : 'Still settling'}</div><//>` : null}
      <div>GPS</div><div>${gps ? (gps.valid ? `Locked on ${plural(gps.sats || 0, 'satellite')}` : 'No lock yet') : '–'}</div>
      <div>Self-test</div><div>${test ? html`<span className=${'chip ' + (/PASS/i.test(test) ? 'chip-good' : 'chip-warn')}>${/PASS/i.test(test) ? 'Passed' : test.toLowerCase()}</span>` : '–'}${chk && chk.codes && chk.codes.length ? html`<div className="muted" style=${{ fontSize: '13px' }}>${chk.codes.join(', ').toLowerCase().replace(/_/g, ' ')}</div>` : null}</div>
    </div>
    <p className="muted" style=${noteStyle}>${d.actuated ? 'This dish has motors and adjusts itself, so the gap should be small.' : 'Standard dishes without motors can’t adjust, so some gap is common.'} It matters only if you see weak signal or dropouts.${off > 15 ? ' The gap is large, so check the sky view above for blocked patches.' : ''}</p>
  </section>`;
}

function DishCard({ s }) {
  const d = s.dish;
  const sleep = d.sleep || {};
  return html`<section className="card">
    <div className="card-head"><div><h2 className="card-title">Dish</h2><div className="card-sub">${kitName(d)}${d.hardware ? ` · ${d.hardware}` : ''}</div></div><${DishChip} dish=${d} /></div>
    <div className="kv">
      <div>Software</div><div className="mono">${d.software || '–'}</div>
      <div>Updates</div><div>${updateWords({ state: d.update_state, ready: d.update_ready, waitS: d.update_wait_s })}${d.update_ready ? html` <span className="chip chip-warn">Restart pending</span>` : null}
        <div className="muted" style=${{ fontSize: '13px' }}>${d.update_hour != null ? `Installs overnight, around ${clockOfMinutes(d.update_hour * 60)}` : 'Installs automatically'}</div></div>
      <div>3-day update delay</div><div>${d.deferral == null ? '–' : d.deferral ? 'On. Updates wait up to 3 days' : 'Off'}</div>
      <div>Up for</div><div>${d.uptime_s ? fmtDur(d.uptime_s * 1000) : '–'}</div>
      <div>Snow melt</div><div>${SNOW[d.snow_melt] || d.snow_melt || '–'}</div>
      <div>Sleep schedule</div><div>${sleep.on && sleep.duration_min ? `On, ${clockOfMinutes(sleep.start_min)} to ${clockOfMinutes(sleep.start_min + sleep.duration_min)}` : 'Off'}</div>
      <div>Motors</div><div>${d.actuated ? 'Yes, it can stow itself' : 'None. It doesn’t stow, you aim it by hand'}</div>
      <div>Cable to router</div><div>${d.eth_mbps ? `${d.eth_mbps >= 1000 ? d.eth_mbps / 1000 + ' Gbps' : d.eth_mbps + ' Mbps'}` : '–'}</div>
      <div>Speed limit</div><div>${!d.dl_limit || d.dl_limit === 'NO_LIMIT' ? 'None' : d.dl_limit.replace(/_/g, ' ').toLowerCase()}</div>
      <div>Alerts</div><div>${d.alerts.length ? d.alerts.map(a => html`<div key=${a.key}>${a.text}</div>`) : 'None'}</div>
    </div>
    <p className="muted" style=${{ fontSize: '13px', marginTop: '12px' }}>Orbit shows what your dish is set to. Change it in the Starlink app.</p>
  </section>`;
}

const SOURCE_NAME = { mikrotik: 'MikroTik router', grandstream: 'Grandstream Wi-Fi', snmp: 'SNMP' };

/* Your own router or Wi-Fi controller, when the Starlink router is in bypass mode (orbit/routers.py). */
function SourcesCard({ s }) {
  return html`<section className="card">
    <div className="card-head"><div><h2 className="card-title">Your router and Wi-Fi</h2><div className="card-sub">The device list comes from here, not the Starlink router</div></div></div>
    <div className="kv">
      ${s.sources.map(x => html`<${Fragment} key=${x.kind + x.host}><div>${x.kind === 'snmp' ? x.host : SOURCE_NAME[x.kind] || x.kind}</div>
        <div>${x.ok === false ? html`<span className="chip chip-crit">Not answering</span>` : x.ok ? html`<span className="chip chip-good"><span className="dot" />Online</span>` : 'Connecting…'}
          <div className="muted" style=${{ fontSize: '13px' }}>${x.ok === false ? x.error : [x.model, x.host, x.ok ? plural(x.clients, 'device') : null, x.updated ? 'read ' + fmtAgo(x.updated, s.now) : null].filter(Boolean).join(' · ')}</div>
          ${x.kind === 'mikrotik' && x.ok && x.counts_usage === false ? html`<div className="muted" style=${{ fontSize: '13px' }}>Data per device needs Kid Control on the router (IP › Kid Control).</div>` : null}</div><//>`)}
    </div>
  </section>`;
}

function RouterCard({ s }) {
  if (s.sources) return html`<${SourcesCard} s=${s} />`;
  const r = s.router;
  const all = r.networks.map(n => ({ ...n, names: [...new Set(Object.values(n.ssids || {}))], devs: s.devices.filter(d => d.online && d.domain === n.domain) }));
  const firstGuest = all.find(n => n.guest);
  const nets = all.filter(n => !n.guest || n.devs.length || n === firstGuest);  // spare guest networks with nobody on them stay hidden
  return html`<section className="card">
    <div className="card-head"><div><h2 className="card-title">Router and Wi-Fi</h2><div className="card-sub">Starlink Router ${routerGen(r.hardware)}${r.software ? ' · software ' + r.software : ''}</div></div>
      ${r.reachable === false ? html`<span className="chip chip-crit">Not answering</span>` : html`<span className="chip chip-good"><span className="dot" />Online</span>`}</div>
    <div className="kv">
      <div>Up for</div><div>${r.uptime_s ? fmtDur(r.uptime_s * 1000) : '–'}</div>
      ${r.update ? html`<${Fragment}><div>Software</div><div><span className="mono">${r.update.running || r.software || '–'}</span><div className="muted" style=${{ fontSize: '13px' }}>${updateWords({ state: r.update.state, pending: r.update.pending, progress: r.update.progress })}</div></div><//>` : null}
      <div>Devices</div><div>${s.devices.filter(d => d.online).length} connected · ${s.devices.length} known</div>
      ${nets.map(n => html`<${Fragment} key=${n.domain}><div>${n.guest ? 'Guest Wi-Fi' : 'Main Wi-Fi'}</div>
        <div>${n.names.join(' · ') || '–'}<div className="muted" style=${{ fontSize: '13px' }}>${plural(n.devs.length, 'device')} on it now${n.guest && n.isolated ? '. Devices on it can’t see your other devices' : ''}</div></div><//>`)}
      ${r.bypass ? html`<${Fragment}><div>Bypass mode</div><div>On. Orbit can’t see devices on your own router.</div><//>` : null}
    </div>
    <p className="muted" style=${{ fontSize: '13px', marginTop: '12px' }}>Orbit reads the router’s device list every 2 seconds. Names you give devices in the Starlink app show up here too.</p>
  </section>`;
}

function SpeedTests({ s, ui }) {
  return html`<section className="card">
    <div className="card-head"><div><h2 className="card-title">Speed tests</h2><div className="card-sub">Run by your router, between your home and the internet</div></div>
      <button type="button" className="btn btn-sm" onClick=${() => ui.openModal({ type: 'speed' })}><${Icon} name="gauge" size=${15} />${s.speedtest.running ? 'Running…' : 'Run one'}</button></div>
    ${s.tests.length ? html`<div style=${{ overflowX: 'auto' }}><table className="table-view">
      <thead><tr><th>When</th><th>Download</th><th>Upload</th><th>Latency</th></tr></thead>
      <tbody>${s.tests.map(t => html`<tr key=${t.ts}><td>${fmtAgo(t.ts, s.now)}, ${fmtClock(t.ts)}</td>
        ${t.error ? html`<td colSpan="3" className="muted">${t.error_text}</td>`
          : html`<${Fragment}><td>${Math.round(t.down)} Mbps</td><td>${Math.round(t.up || 0)} Mbps</td><td>${t.latency ? Math.round(t.latency) + ' ms' : '–'}</td><//>`}</tr>`)}</tbody>
    </table></div>` : html`<div className="empty">No speed tests yet.</div>`}
  </section>`;
}

export function StarlinkView({ s, x, points, ui }) {
  const d = s.dish;
  const { data: map } = usePoll('/api/obstruction', 600000);
  const stateWord = d.reachable === false ? 'Can’t reach it' : d.online === false ? 'Offline' : d.online ? 'Online' : 'Connecting';
  const last = s.tests[0];
  return html`<div className="view">
    <div className="page-head"><div><h1 className="page-title">Starlink</h1><p className="page-sub">How your link to space is doing, what the dish can see, and the details of your dish and router.</p></div>
      <button type="button" className="btn btn-primary" onClick=${() => ui.openModal({ type: 'speed' })}><${Icon} name="gauge" size=${16} />${s.speedtest.running ? 'Speed test running…' : 'Run a speed test'}</button></div>
    <${LinkPath} s=${s} x=${x} />
    <div className="tiles">
      <${Stat} icon="satellite-dish" label="Connection" value=${stateWord} note=${d.online_since ? 'Online for ' + fmtDur(s.now - d.online_since) : d.outage ? d.outage.text : d.reachable === false ? 'It may be restarting or unplugged' : ''} />
      <${Stat} icon="timer" label="Latency" live=${d.latency_ms != null} value=${d.latency_ms == null ? '–' : Math.round(d.latency_ms)} unit=${d.latency_ms == null ? '' : 'ms'}
        note=${`How fast the internet responds. Under 40 ms is great.${d.drop_rate != null ? ` ${(d.drop_rate * 100).toFixed(d.drop_rate > 0 && d.drop_rate < 0.1 ? 1 : 0)}% of pings lost.` : ''}`}
        spark=${html`<${Spark} values=${points.slice(-40).map(p => p.lat)} w=${76} h=${30} max=${80} />`} />
      <${Stat} icon="gauge" label="Last speed test" value=${last && !last.error ? Math.round(last.down) : '–'} unit=${last && !last.error ? 'Mbps' : ''}
        note=${s.speedtest.running ? 'Testing now…' : last && !last.error ? `${Math.round(last.up || 0)} Mbps up · ${fmtAgo(last.ts, s.now)}` : last ? last.error_text : 'No test yet. Run one to see your real speed.'} />
      <${Stat} icon="triangle-alert" label="Dropouts, 24 h" value=${s.outages.length} note=${`${fmtSecs(sum(s.outages, o => o.duration_s))} offline in total`} />
      <${Stat} icon="eye-off" label="Sky blocked" value=${((d.obstructed || 0) * 100).toFixed(d.obstructed > 0 && d.obstructed < 0.01 ? 2 : 1)} unit="%" note=${d.obstructed_now ? 'Something is blocking it right now' : d.obstructed > 0.0005 ? 'Some of the time' : 'Nothing is in the way'} />
      <${Stat} icon="plug-zap" label="Power use" value=${d.power_w ? Math.round(d.power_w) : '–'} unit=${d.power_w ? 'W' : ''} note=${d.power_w ? `About ${((d.power_w * 24) / 1000).toFixed(1)} kWh a day` : 'Measuring…'} />
    </div>
    <div className="grid">
      <div className="s-5"><section className="sky sky-stars skymap-wrap" aria-label="Sky view">
        <div style=${{ position: 'relative' }}><h2 className="sky-title">What your dish sees</h2><div className="sky-sub">Its own map of the sky, ${map ? 'updated ' + fmtAgo(map.updated, s.now) : 'loading'}. North is at the top.</div></div>
        <${SkyMap} map=${map} obstructed=${d.obstructed} />
      </section></div>
      <div className="s-7"><${OutagesCard} s=${s} /></div>
      <div className="s-6"><${DishCard} s=${s} /></div>
      <div className="s-6"><${RouterCard} s=${s} /></div>
      <div className="s-6"><${ConnectionChain} s=${s} /></div>
      <div className="s-6"><${PointingCard} s=${s} /></div>
      <div className="s-12"><${SpeedTests} s=${s} ui=${ui} /></div>
    </div>
  </div>`;
}
