/* Devices: the full list with search, filters and changes to several at once, and the details drawer for one device. */
import {
  html, useState, useEffect, useRef, Fragment, sum, enterKey, readPref, writePref, fmtMbps, fmtBytes, fmtDur, fmtAgo,
  fmtClock, fmtDate, fmtWeekday, fmtLimit, Icon, Glyph, Bars, StatusChip, Spark, Seg, AssignMenu, TypeMenu, Switch,
  AllowanceMeter, AllowanceEditor, CopyBtn, UsageBars, Modal, trapTab, personOf, devColor, ownerLabel, ownName, signalInfo, effSignal,
  bandLabel, netLabel, connLabel, statusDetail, isWired, PERIOD, TYPES,
} from './lib.js';
import { usePoll } from './data.js';

const sigVal = d => (isWired(d) ? 0 : d.signal == null ? -99 : d.signal);

function DeviceRow({ d, s, ui, selecting, selected, onToggle }) {
  const p = personOf(s.people, d.owner);
  const act = selecting ? onToggle : () => ui.openDevice(d.id);
  const label = selecting ? `${d.name}, ${d.online ? 'online' : 'offline'}` : `${d.name}, ${d.online ? 'online' : 'offline'}. Open details`;
  return html`<div className=${'dev-row' + (selecting ? ' selecting' : '') + (selected ? ' is-selected' : '')} role=${selecting ? 'checkbox' : 'button'}
      aria-checked=${selecting ? (selected ? 'true' : 'false') : undefined} tabIndex="0" onClick=${act}
      onKeyDown=${e => { if (e.target === e.currentTarget) enterKey(act)(e); }} aria-label=${label}>
    <div className="dev-main">
      ${selecting ? html`<span className=${'tick' + (selected ? ' on' : '')} aria-hidden="true"><${Icon} name="check" size=${13} sw=${3.2} /></span>` : null}
      <${Glyph} d=${d} people=${s.people} />
      <div style=${{ minWidth: 0 }}>
        <div className="row" style=${{ gap: '6px' }}><span className="dev-name">${d.name}</span>${d.is_new ? html`<span className="new-tag">New</span>` : null}${d.hidden ? html`<span className="tag-muted">Hidden</span>` : null}</div>
        <div className="dev-meta"><span className=${'pdot' + (p ? '' : ' is-none')} style=${{ '--pc': devColor(d, p) }} /><span>${ownerLabel(d, p)} · ${netLabel(d)}</span></div>
      </div>
    </div>
    <div className="dev-status"><${StatusChip} d=${d} /><small>${statusDetail(d, s.now)}</small></div>
    <div className="dev-conn">${d.online ? html`<${Bars} d=${d} /><span>${connLabel(d)}</span>` : html`<span className="muted">${d.ip ? 'Last at ' + d.ip : 'Not connected'}</span>`}</div>
    <div className="dev-now">${d.online ? html`<span className="speeds tnum"><span><span className="u">↓ </span>${fmtMbps(d.down_mbps)}</span><span><span className="u">↑ </span>${fmtMbps(d.up_mbps)}</span><span className="u">Mbps</span></span>` : html`<span className="muted">No traffic</span>`}</div>
    <div className="dev-today tnum">${fmtBytes(d.today_down + d.today_up)}<span className="sr-only"> today</span>${d.allowance ? html`<${AllowanceMeter} a=${d.allowance} compact=${true} />` : null}</div>
    <div className="dev-acts">${selecting ? null : html`<${Icon} name="chevron-right" size=${16} className="muted" />`}</div>
  </div>`;
}

/* The bar of changes for the devices you picked. */
function BulkBar({ s, list, chosen, setSel, done, api, ui }) {
  const n = chosen.length;
  const ids = chosen.map(d => d.id);
  const what = n === 1 ? chosen[0].name : `${n} devices`;
  const allHidden = n > 0 && chosen.every(d => d.hidden);
  const online = chosen.filter(d => d.online);
  const all = fields => ids.map(id => ({ id, ...fields }));
  const owner = pid => {
    const p = personOf(s.people, pid);
    api.bulk(all({ owner: pid }), p ? `${what} now ${n === 1 ? 'belongs' : 'belong'} to ${p.name}` : `${what} ${n === 1 ? 'has' : 'have'} no owner now`);
  };
  const forget = () => ui.openModal({
    type: 'confirm', title: `Forget ${what}?`, danger: true, label: n === 1 ? 'Forget device' : `Forget ${n} devices`,
    body: `Orbit removes ${n === 1 ? 'its name and owner' : 'their names and owners'}. If ${n === 1 ? 'it connects' : 'they connect'} again, ${n === 1 ? 'it shows' : 'they show'} up as new. Usage history stays in your totals.`,
    fn: () => { api.forgetMany(ids); setSel(new Set()); },
  });
  return html`<div className="bulk" role="toolbar" aria-label="Change the selected devices">
    <div className="bulk-count"><span><b className="tnum">${n}</b> selected</span>
      ${n < list.length ? html`<button type="button" className="link-btn" onClick=${() => setSel(new Set(list.map(d => d.id)))}>Select all ${list.length}</button>` : null}
      ${n ? html`<button type="button" className="link-btn" onClick=${() => setSel(new Set())}>Clear</button>` : null}</div>
    <div className="bulk-acts">
      <${AssignMenu} people=${s.people} onPick=${owner} label="Owner" btnClass="btn btn-sm" noneOption=${true} disabled=${!n} />
      <${TypeMenu} onPick=${type => api.bulk(all({ type }), `${what}: ${TYPES[type].label}`)} disabled=${!n} />
      ${chosen.some(d => d.is_new) ? html`<button type="button" className="btn btn-sm" onClick=${() => api.bulk(all({ acknowledged: true }), `${what} ${n === 1 ? 'is' : 'are'} marked as known`, 'shield-check')}><${Icon} name="shield-check" size=${15} />Mark as known</button>` : null}
      <button type="button" className="btn btn-sm" disabled=${!n} onClick=${() => api.bulk(all({ hidden: !allHidden }), allHidden ? `${what} back in your lists` : `Hid ${what}`, allHidden ? 'eye' : 'eye-off')}><${Icon} name=${allHidden ? 'eye' : 'eye-off'} size=${15} />${allHidden ? 'Show' : 'Hide'}</button>
      <button type="button" className="btn btn-sm btn-danger" disabled=${!n || online.length > 0} title=${online.length ? `${online[0].name} is connected. Only offline devices can be forgotten.` : undefined} onClick=${forget}><${Icon} name="trash-2" size=${15} />Forget</button>
    </div>
    <button type="button" className="btn btn-sm btn-primary" onClick=${done}>Done</button>
  </div>`;
}

export function DevicesView({ s, x, api, ui }) {
  const [q, setQ] = useState('');
  const [st, setSt] = useState('all');
  const [who, setWho] = useState('all');
  const [sort, setSort] = useState('usage');
  const [selecting, setSelecting] = useState(false);
  const [sel, setSel] = useState(() => new Set());
  const ql = q.trim().toLowerCase();
  // Hidden devices only show under Hidden, or when a search finds them.
  const match = { all: d => !d.hidden || !!ql, online: d => d.online && !d.hidden, offline: d => !d.online && !d.hidden, new: d => d.is_new, blocked: d => d.blocked && !d.hidden, hidden: d => d.hidden };
  const count = k => s.devices.filter(k === 'all' ? d => !d.hidden : match[k]).length;
  const list = s.devices.filter(d => match[st](d) && (who === 'all' || (who === 'none' ? !d.owner : d.owner === who)) &&
    (!ql || [d.name, d.router_name, d.hostname, d.ip, d.mac, d.notes, (personOf(s.people, d.owner) || {}).name, (TYPES[d.type] || {}).label].join(' ').toLowerCase().includes(ql)));
  const rank = d => (d.online ? 0 : 1);
  list.sort((a, b) => sort === 'name' ? a.name.localeCompare(b.name)
    : sort === 'signal' ? rank(a) - rank(b) || sigVal(b) - sigVal(a)
    : sort === 'today' ? (b.today_down + b.today_up) - (a.today_down + a.today_up)
    : rank(a) - rank(b) || (b.down_mbps + b.up_mbps) - (a.down_mbps + a.up_mbps) || (b.today_down - a.today_down));
  // While you pick devices, rows keep their places instead of moving with live speeds.
  const frozen = useRef({ key: null, at: new Map() });
  if (selecting) {
    const key = [st, who, sort, ql].join('|');
    if (frozen.current.key !== key) frozen.current = { key, at: new Map(list.map((d, i) => [d.id, i])) };
    const at = frozen.current.at;
    list.sort((a, b) => (at.has(a.id) ? at.get(a.id) : 1e9) - (at.has(b.id) ? at.get(b.id) : 1e9));
  } else {
    frozen.current.key = null;
  }
  const filters = [{ value: 'all', label: 'All', count: count('all') }, { value: 'online', label: 'Online', count: count('online') },
    { value: 'offline', label: 'Offline', count: count('offline') }];
  if (count('new')) filters.push({ value: 'new', label: 'New', count: count('new') });
  if (count('blocked')) filters.push({ value: 'blocked', label: 'Blocked', count: count('blocked') });
  if (count('hidden') || st === 'hidden') filters.push({ value: 'hidden', label: 'Hidden', count: count('hidden') });
  const chosen = list.filter(d => sel.has(d.id));  // only what's on screen: a filter change never acts on unseen devices
  const shownKey = list.map(d => d.id).join(',');
  useEffect(() => {  // a device that leaves the list, say after you hide it, leaves the selection too
    setSel(m => (chosen.length === m.size ? m : new Set(chosen.map(d => d.id))));
  }, [shownKey]);
  const toggle = id => setSel(m => { const next = new Set(m); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const done = () => { setSelecting(false); setSel(new Set()); };
  useEffect(() => {
    if (!selecting) return undefined;
    const k = e => { if (e.key === 'Escape' && !document.querySelector('.modal-wrap, .drawer')) done(); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [selecting]);
  const hiddenN = count('hidden');
  return html`<div className="view">
    <div className="page-head"><div><h1 className="page-title">Devices</h1>
      <p className="page-sub">${x.conn.length} connected, ${x.active.length} using the internet${count('offline') ? `, ${count('offline')} offline` : ''}${hiddenN ? `, ${hiddenN} hidden` : ''}. Tap a device to name it, say whose it is, or set an allowance. Use Select to change several at once.</p></div></div>
    <div className="filters">
      <div className="search-in"><${Icon} name="search" size=${16} /><input id="dev-search" className="input" type="search" placeholder="Search name, IP address, owner or notes" value=${q} onChange=${e => setQ(e.target.value)} aria-label="Search devices" /></div>
      <${Seg} label="Filter by status" value=${st} onChange=${setSt} options=${filters} />
      <select id="dev-person" className="select" value=${who} onChange=${e => setWho(e.target.value)} aria-label="Filter by person">
        <option value="all">Everyone</option>${s.people.map(p => html`<option key=${p.id} value=${p.id}>${p.name}</option>`)}<option value="none">No owner yet</option>
      </select>
      <select id="dev-sort" className="select" value=${sort} onChange=${e => setSort(e.target.value)} aria-label="Sort devices">
        <option value="usage">Using the most</option><option value="today">Most data today</option><option value="signal">Strongest signal</option><option value="name">Name</option>
      </select>
      ${selecting ? null : html`<button type="button" className="btn" onClick=${() => setSelecting(true)} disabled=${!list.length}><${Icon} name="list" size=${16} />Select</button>`}
    </div>
    ${selecting ? html`<${BulkBar} s=${s} list=${list} chosen=${chosen} setSel=${setSel} done=${done} api=${api} ui=${ui} />` : null}
    <section className="card dev-list" aria-label="Device list">
      <div className="dev-row head" aria-hidden="true"><span>Device</span><span>Status</span><span>Connection</span><span>Right now</span><span style=${{ textAlign: 'right' }}>Today</span><span /></div>
      ${list.map(d => html`<${DeviceRow} key=${d.id} d=${d} s=${s} ui=${ui} selecting=${selecting} selected=${sel.has(d.id)} onToggle=${() => toggle(d.id)} />`)}
      ${list.length === 0 ? html`<div className="empty">${st === 'hidden' ? 'Nothing is hidden. Hide a device from its details to keep it off your lists.' : s.devices.length ? 'No devices match. Clear the search or pick another filter.' : 'No devices yet. They appear as soon as Orbit hears from your router.'}</div>` : null}
    </section>
  </div>`;
}

/* ---------- the drawer ---------- */
function WeekBars({ id, p, d }) {
  const { data } = usePoll(`/api/devices/${id}/usage`, 60000);
  if (!data) return html`<div className="cols" aria-busy="true" />`;
  const bars = data.days.map((x, i) => (i === data.days.length - 1 ? { ...x, down: d.today_down, up: d.today_up } : x));
  return html`<div className="drawer-sec"><h3>Data used</h3>
    <div className="row" style=${{ gap: '20px' }}>
      <div><div className="muted" style=${{ fontSize: '12px', fontWeight: 650 }}>Today</div><b>${fmtBytes(d.today_down + d.today_up)}</b> <span className="muted" style=${{ fontSize: '12.5px' }}>↓ ${fmtBytes(d.today_down)} · ↑ ${fmtBytes(d.today_up)}</span></div>
      <div><div className="muted" style=${{ fontSize: '12px', fontWeight: 650 }}>Last 7 days</div><b>${fmtBytes(sum(bars, b => b.down + b.up))}</b></div>
    </div>
    <${UsageBars} bars=${bars} color=${devColor(d, p)} labelOf=${(b, i) => (i === bars.length - 1 ? 'Today' : fmtWeekday(b.t))} tipOf=${b => new Date(b.t).toLocaleDateString([], { weekday: 'long' })} />
  </div>`;
}

function AllowanceSec({ d, api }) {
  const [edit, setEdit] = useState(false);
  useEffect(() => setEdit(false), [d.id]);
  const a = d.allowance;
  const save = v => { api.setAllowance(d.id, v); setEdit(false); };
  return html`<div className="drawer-sec"><h3>Allowance</h3>
    ${edit ? html`<${AllowanceEditor} a=${a} onSave=${save} onCancel=${() => setEdit(false)} />`
      : a ? html`<div className="allow-row"><div className="grow"><div className="allow-title">${fmtLimit(a.bytes)} ${PERIOD[a.period].per}</div><${AllowanceMeter} a=${a} /></div>
          <button type="button" className="btn btn-sm" onClick=${() => setEdit(true)}>Change</button></div>`
      : html`<div className="allow-row"><p className="muted grow" style=${{ fontSize: '13.5px' }}>Give it an amount of data a day or a month. Orbit tells you when it’s nearly used up, and when it’s used up.</p>
          <button type="button" className="btn btn-sm" onClick=${() => setEdit(true)}><${Icon} name="bell-ring" size=${15} />Set an allowance</button></div>`}
    ${a || edit ? html`<p className="muted fine">Orbit can’t cut it off, because your router only takes changes from the Starlink app. To pause it, use the app.</p>` : null}
  </div>`;
}

function NewDeviceBanner({ d, s, api, ui }) {
  const [notSame, setNotSame] = useState(() => readPref('not-same', {}));
  const same = d.maybe_same && !notSame[d.id + ':' + d.maybe_same] && s.devices.find(z => z.id === d.maybe_same);
  if (same) {
    const no = () => { const next = { ...notSame, [d.id + ':' + same.id]: Date.now() }; setNotSame(next); writePref('not-same', next); };
    return html`<div className="banner accent"><span className="banner-ic"><${Icon} name="link" size=${17} /></span>
      <div className="grow"><div className="banner-title">Is this ${same.name} with a new address?</div>
        <div className="banner-body">It has the same name as ${same.name}, which isn’t connected now. Phones make up a new address for each Wi-Fi network, and now and then a new one, so the same phone can show up twice.</div>
        <div className="row wrap" style=${{ marginTop: '10px' }}><button type="button" className="btn btn-sm btn-primary" onClick=${() => ui.openModal({ type: 'merge', id: d.id, other: same.id })}><${Icon} name="link" size=${15} />Yes, merge them</button>
          <button type="button" className="btn btn-sm" onClick=${no}>No, it’s another device</button></div></div></div>`;
  }
  return html`<div className="banner warn"><span className="banner-ic"><${Icon} name="shield-alert" size=${17} /></span>
    <div className="grow"><div className="banner-title">Do you recognize this device?</div>
      <div className="banner-body">It first joined ${fmtAgo(d.first_seen, s.now)}, on ${netLabel(d).toLowerCase()}. ${d.signal != null && effSignal(d) < -72 ? 'Its weak signal suggests it’s far away, maybe outside your home. ' : ''}If nobody knows it, change your Wi-Fi password in the Starlink app.</div>
      <div className="row wrap" style=${{ marginTop: '10px' }}><${AssignMenu} people=${s.people} onPick=${pid => api.assign(d.id, pid)} label="It’s ours" /><button type="button" className="btn btn-sm" onClick=${() => api.acknowledge(d.id)}>It’s fine</button></div></div></div>`;
}

export function DeviceDrawer({ id, s, devHist, api, ui }) {
  const d = s.devices.find(z => z.id === id);
  const [name, setName] = useState(d ? d.name : '');
  const [notes, setNotes] = useState(d ? d.notes || '' : '');
  const closeRef = useRef(null);
  useEffect(() => {
    if (d) { setName(d.custom_name || d.name); setNotes(d.notes || ''); }
    const back = document.activeElement;
    if (closeRef.current) closeRef.current.focus();
    return () => { if (back && back.focus) back.focus(); };
  }, [id]);
  if (!d) return null;
  const p = personOf(s.people, d.owner);
  const sig = signalInfo(effSignal(d));
  const t = TYPES[d.type] || TYPES.unknown;
  const commit = () => {
    const v = name.trim();
    if (v === (d.custom_name || d.name)) return;
    if (!v && !d.custom_name) { setName(d.name); return; }
    api.rename(d.id, v);
  };
  const saveNotes = () => { if (notes.trim() !== (d.notes || '')) api.setNotes(d.id, notes.trim()); };
  const routerName = d.router_name || d.hostname;
  return html`<${Fragment}>
    <div className="scrim" onClick=${ui.closeDevice} />
    <aside className="drawer" role="dialog" aria-modal="true" aria-label=${d.name + ' details'} onKeyDown=${trapTab}>
      <div className="drawer-top"><span className="eyebrow">${t.label}${d.hidden ? ' · Hidden' : ''}</span><button ref=${closeRef} type="button" className="btn btn-icon btn-ghost" aria-label="Close" onClick=${ui.closeDevice}><${Icon} name="x" size=${18} /></button></div>
      <div className="drawer-body">
        <div className="row" style=${{ gap: '14px', alignItems: 'flex-start' }}>
          <${Glyph} d=${d} people=${s.people} size="lg" />
          <div className="grow">
            <input id="device-name" className="name-in" value=${name} aria-label="Device name" maxLength=${40} placeholder=${d.router_name || d.hostname || 'Name this device'}
              onChange=${e => setName(e.target.value)} onBlur=${commit}
              onKeyDown=${e => { if (e.key === 'Enter') e.currentTarget.blur(); if (e.key === 'Escape') { setName(d.custom_name || d.name); e.stopPropagation(); } }} />
            <div className="row wrap" style=${{ gap: '6px', marginTop: '4px' }}><${StatusChip} d=${d} /><span className="muted" style=${{ fontSize: '13px' }}>${statusDetail(d, s.now)}</span></div>
            <div className="row wrap" style=${{ marginTop: '10px', gap: '8px' }}>
              <${AssignMenu} people=${s.people} current=${d.owner} onPick=${pid => api.assign(d.id, pid)} btnClass="btn btn-sm"
                label=${html`<span className=${'pdot' + (p ? '' : ' is-none')} style=${{ '--pc': devColor(d, p) }} />${p ? p.name : 'Choose an owner'}`} />
              <select className="select" style=${{ height: '32px', fontSize: '13px' }} value=${d.type} aria-label="Kind of device" onChange=${e => api.setType(d.id, e.target.value)}>
                ${Object.entries(TYPES).map(([k, v]) => html`<option key=${k} value=${k}>${v.label}</option>`)}
              </select>
            </div>
          </div>
        </div>
        ${d.is_new ? html`<${NewDeviceBanner} key=${d.id} d=${d} s=${s} api=${api} ui=${ui} />` : null}
        ${d.blocked ? html`<div className="banner crit"><span className="banner-ic"><${Icon} name="ban" size=${17} /></span>
          <div className="grow"><div className="banner-title">No internet access right now</div>
            <div className="banner-body">It’s blocked from the Starlink app, either paused or inside a schedule. It stays on your Wi-Fi. To change it, use the Starlink app.</div></div></div>` : null}
        ${d.online ? html`<div className="live-box">
          <div><div className="muted" style=${{ fontSize: '12px', fontWeight: 650 }}>Download</div><b className="tnum">${fmtMbps(d.down_mbps)}</b> <span className="muted">Mbps</span></div>
          <div><div className="muted" style=${{ fontSize: '12px', fontWeight: 650 }}>Upload</div><b className="tnum">${fmtMbps(d.up_mbps)}</b> <span className="muted">Mbps</span></div>
          <div className="grow" style=${{ display: 'flex', justifyContent: 'flex-end' }}><${Spark} values=${devHist[d.id] || []} w=${120} h=${40} color=${devColor(d, p)} /></div>
        </div>` : null}
        <p className="muted" style=${{ fontSize: '12.5px', marginTop: '-8px' }}>Live speeds are measured from your router over the last few seconds. ${d.schedules
          ? `It has ${d.schedules === 1 ? 'a block schedule' : d.schedules + ' block times'} set in the Starlink app. `
          : ''}To pause a device or give it a schedule, use the Starlink app: your router only accepts changes from there. Orbit shows what you set.</p>
        <div className="drawer-sec"><h3>Connection</h3>
          <div className="kv">
            <div>Network</div><div>${netLabel(d)}${d.ssid ? html` <span className="muted">· ${d.ssid}</span>` : null}</div>
            ${!isWired(d) && d.online ? html`<${Fragment}><div>Band</div><div>${bandLabel(d)}${d.upper_band ? ' · upper channels' : ''}${d.width ? ` · ${d.width} MHz wide` : ''}</div>
              <div>Signal</div><div><span className="row" style=${{ gap: '8px' }}><${Bars} d=${d} />${sig.word} <span className="muted tnum">(${d.signal} dBm)</span></span>
                ${sig.level <= 2 ? html`<div className="muted" style=${{ fontSize: '13px', marginTop: '4px' }}>${d.band === '5' ? 'Move it closer to the router. 2.4 GHz reaches further through walls, if the device lets you pick.' : 'Move it closer to the router for a steadier connection.'}</div>` : null}
                ${d.band === '2.4' ? html`<div className="muted" style=${{ fontSize: '13px', marginTop: '4px' }}>2.4 GHz reaches farther, so its signal is judged 6 dB more gently than 5 GHz.</div>` : null}</div><//>` : null}
            ${d.ap && d.online ? html`<${Fragment}><div>Access point</div><div>${d.ap}</div><//>` : null}
            ${d.port && d.online ? html`<${Fragment}><div>Switch port</div><div>${d.port}</div><//>` : null}
            ${d.online && (d.rate_down || d.rate_up) ? html`<${Fragment}><div>Link speed</div><div className="tnum">↓ ${Math.round(d.rate_down || 0)} · ↑ ${Math.round(d.rate_up || 0)} Mbps <span className="muted">right now</span></div><//>` : null}
            <div>${d.online ? 'Connected' : 'Last seen'}</div><div>${d.online ? (d.since ? `${fmtDur(s.now - d.since)} (since ${fmtClock(d.since)})` : 'Yes') : fmtAgo(d.last_seen || s.now, s.now)}</div>
          </div>
        </div>
        <${WeekBars} id=${d.id} p=${p} d=${d} />
        <${AllowanceSec} d=${d} api=${api} />
        <div className="drawer-sec"><h3><label htmlFor="device-notes">Notes</label></h3>
          <textarea id="device-notes" className="input" rows="2" maxLength=${500} value=${notes} placeholder="Where it is, who to ask about it…"
            onChange=${e => setNotes(e.target.value)} onBlur=${saveNotes} />
        </div>
        <div className="drawer-sec"><h3>Device</h3>
          <div className="kv">
            ${routerName ? html`<${Fragment}><div>Router calls it</div><div>${d.router_name || d.hostname}${d.router_name && d.hostname && d.hostname !== d.router_name ? html` <span className="muted">(${d.hostname})</span>` : null}</div><//>` : null}
            <div>IP address</div><div className="row" style=${{ gap: '4px' }}>${d.ip ? html`<span className="mono">${d.ip}</span><${CopyBtn} text=${d.ip} ui=${ui} label="Copy IP address" />` : '–'}</div>
            <div>MAC address</div><div><span className="mono">${d.mac || '–'}</span>
              <div className="muted" style=${{ fontSize: '12.5px' }}>${d.private_mac ? 'A private address the device made up for your network. ' : ''}Your router hides the second half.</div></div>
            <div>First seen</div><div>${fmtDate(d.first_seen)}</div>
          </div>
        </div>
        <div className="drawer-sec"><h3>Manage</h3>
          <div className="setting"><div className="setting-text"><label className="setting-title" htmlFor="device-hidden">Hide from lists</label>
            <div className="setting-sub">It stays off the device list, the Overview map and suggestions. Orbit still records what it uses.</div></div>
            <${Switch} id="device-hidden" on=${d.hidden} onChange=${v => api.setHidden(d.id, v)} label="Hide from lists" /></div>
          <div className="setting"><div className="setting-text"><div className="setting-title">Showing up twice?</div>
            <div className="setting-sub">Phones make up a new address now and then, and Orbit then sees a new device. Merge the two entries to count them as one.</div></div>
            <button type="button" className="btn btn-sm" onClick=${() => ui.openModal({ type: 'merge', id: d.id })}><${Icon} name="link" size=${15} />Merge…</button></div>
          ${d.aliases.length ? html`<div className="setting"><div className="setting-text"><div className="setting-title">Merged into this device</div>
            <div className="setting-sub">${d.aliases.length === 1 ? 'This connection counts' : 'These connections count'} as ${d.name}.</div>
            ${d.aliases.map(a => html`<div className="alias" key=${a.cid}>
              <div className="grow"><div className="alias-name">${a.name || 'No name'} ${a.mac ? html`<span className="mono muted">${a.mac.slice(0, 8)}</span>` : null}</div>
                <div className="muted" style=${{ fontSize: '12.5px' }}>Last seen ${a.last_seen ? fmtAgo(a.last_seen, s.now) : 'before the merge'}</div></div>
              <button type="button" className="btn btn-sm btn-ghost" onClick=${() => ui.openModal({
                type: 'confirm', title: `Separate ${a.name || 'this connection'}?`, label: 'Separate',
                body: `It becomes its own device again, so what it uses from now on counts separately. What it used so far stays with ${d.name}.`,
                fn: () => api.separate(d.id, a.cid, a.name),
              })}><${Icon} name="unlink" size=${15} />Separate</button></div>`)}</div></div>` : null}
        </div>
        ${!d.online ? html`<button type="button" className="btn btn-ghost btn-danger" style=${{ alignSelf: 'flex-start' }} onClick=${() => ui.openModal({ type: 'confirm', title: `Forget ${d.name}?`, body: 'Orbit removes its name and owner. If it connects again, it shows up as a new device. Its usage history stays in your totals.', label: 'Forget device', danger: true, fn: () => api.forget(d.id) })}><${Icon} name="trash-2" size=${16} />Forget this device</button>` : null}
      </div>
    </aside>
  <//>`;
}

/* ---------- merging two entries for the same device ---------- */
const lowNames = x => [x.router_name, x.hostname, ...(x.aliases || []).map(a => a.name)].filter(Boolean).map(n => n.toLowerCase());

export function MergeModal({ s, api, ui, id, other }) {
  const d = s.devices.find(z => z.id === id);
  const [pick, setPick] = useState(other || (d && d.maybe_same) || null);
  if (!d) return null;
  const mine = new Set(lowNames(d));
  const sameName = x => lowNames(x).some(n => mine.has(n));
  const score = x => (x.id === d.maybe_same ? 4 : 0) + (sameName(x) ? 2 : 0) + (x.type === d.type ? 1 : 0);
  const cands = s.devices.filter(x => x.id !== d.id).sort((a, b) => score(b) - score(a) || a.online - b.online || a.name.localeCompare(b.name));
  const clash = x => d.online && x.online;  // both connected at once: two different devices
  const o = pick ? s.devices.find(x => x.id === pick && !clash(x)) : null;
  let preview = null;
  if (o) {  // the same rules the server uses
    const [base, extra] = (d.first_seen || 0) <= (o.first_seen || 0) ? [d, o] : [o, d];
    const keep = d.online || (!o.online && (d.last_seen || 0) >= (o.last_seen || 0)) ? d : o;
    const final = base.custom_name || extra.custom_name || (ownName(base) ? base.name : keep.name);
    const owner = personOf(s.people, base.owner || extra.owner);
    preview = `They become one device, ${final}${owner ? `, owned by ${owner.name}` : ''}, with both histories added together. Whichever one connects, Orbit counts it as ${final}. You can separate them later, but the history stays combined.`;
  }
  return html`<${Modal} onClose=${ui.closeModal} title=${`Merge ${d.name} with…`}>
    <p className="ink2">Pick the entry that’s the same device as ${d.name}.</p>
    <div className="pick-list" role="radiogroup" aria-label=${`Same device as ${d.name}`}>
      ${cands.map(x => {
        const xp = personOf(s.people, x.owner);
        return html`<label key=${x.id} className=${'pick' + (clash(x) ? ' is-off' : '') + (pick === x.id ? ' is-on' : '')}>
          <input type="radio" name="merge-with" value=${x.id} checked=${pick === x.id} disabled=${clash(x)} onChange=${() => setPick(x.id)} />
          <${Glyph} d=${x} people=${s.people} size="sm" />
          <span className="grow" style=${{ minWidth: 0 }}><span className="pick-name">${x.name}</span>${sameName(x) ? html`<span className="new-tag same-tag">Same name</span>` : null}
            <span className="pick-meta">${clash(x) ? 'Also connected now, so it’s a different device' : `${ownerLabel(x, xp)} · ${x.online ? 'Connected now' : 'Seen ' + fmtAgo(x.last_seen || s.now, s.now)}`}</span></span>
        </label>`;
      })}
      ${cands.length === 0 ? html`<div className="empty">There’s no other device to merge with.</div>` : null}
    </div>
    ${preview ? html`<p className="muted" style=${{ fontSize: '13.5px' }}>${preview}</p>` : null}
    <div className="row" style=${{ justifyContent: 'flex-end' }}><button type="button" className="btn btn-ghost" onClick=${ui.closeModal}>Cancel</button>
      <button type="button" className="btn btn-primary" disabled=${!o} onClick=${() => api.merge(d.id, o.id)}><${Icon} name="link" size=${16} />Merge</button></div>
  <//>`;
}
