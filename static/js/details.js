// Shared object details modal. Every layer (disks -> vdevs -> pools ->
// datasets -> shares) renders its own panel here, including adjacency lists
// that link to the details panel of the adjacent layer object.

const CACHE_TTL = 15000;
const detailsCache = {};

async function cached(key, fn) {
    const hit = detailsCache[key];
    if (hit && Date.now() - hit.t < CACHE_TTL) return hit.v;
    const v = await fn();
    detailsCache[key] = { t: Date.now(), v };
    return v;
}

const cachedPoolStatus = n => cached('ps|' + n, () => api.getPoolStatus(n));
const cachedDatasetsFor = n => cached('ds|' + n, () => api.getDatasets(n));
const cachedNfsShares = () => cached('nfs', () => api.getNfsExports());
const cachedSmbShares = () => cached('smb', () => api.getSmbShares());
const cachedDisksList = () => cached('disks', () => api.getDisks());
const cachedDiskHealth = id => cached('dh|' + id, () => api.getDiskHealth(id));

function diskIdentityKeys(d) {
    const keys = [];
    if (d.by_id) {
        keys.push(d.by_id);
        keys.push(d.by_id.replace('/dev/disk/by-id/', ''));
    }
    if (d.device_name) keys.push(d.device_name.replace(/^\/dev\//, ''));
    return keys.filter(Boolean);
}

function matchChildToKeys(child, keys) {
    const cands = [child.name || '', child.path || ''];
    cands.push((child.name || '').replace('/dev/disk/by-id/', ''));
    cands.push((child.path || '').replace('/dev/disk/by-id/', ''));
    for (const c of cands) {
        if (!c) continue;
        for (const k of keys) {
            if (c === k || c.startsWith(k + '-part')) return true;
        }
    }
    return false;
}

function vkey(pool, guid) {
    return encodeURIComponent(pool) + '|' + guid;
}

function parseVkey(key) {
    const i = key.indexOf('|');
    return { pool: decodeURIComponent(key.slice(0, i)), guid: key.slice(i + 1) };
}

function skey(proto, ds) {
    return proto + '|' + encodeURIComponent(ds);
}

function parseSkey(key) {
    const i = key.indexOf('|');
    return { proto: key.slice(0, i), ds: decodeURIComponent(key.slice(i + 1)) };
}

function vdevLabel(groupName, v) {
    const type = v.type || v.name || 'vdev';
    return groupName === 'data' ? type : `${groupName}: ${type}`;
}

function adjLink(kind, key, label, sub) {
    return `<a href="javascript:void(0)" onclick="Details.open('${kind}','${key}')" title="${escapeHtml(sub || '')}">${escapeHtml(label)}</a>`;
}

function adjSection(title, items) {
    if (!items || !items.length) return '';
    return `<div class="disk-detail-section" style="margin-top:12px">
        <h4>${title}</h4>
        ${items.map(it => `<div style="margin-bottom:6px"><span class="detail-key">${it.lbl}</span><span class="detail-val">${adjLink(it.kind, it.key, it.label, it.sub)}</span></div>`).join('')}
    </div>`;
}

function parseSize(str) {
    if (!str) return 0;
    const m = str.match(/([\d.]+)\s*([KMGTP]i?B?)/i);
    if (!m) return parseFloat(str) || 0;
    const val = parseFloat(m[1]);
    const unit = m[2][0].toUpperCase();
    const mult = {K: 1024, M: 1024 ** 2, G: 1024 ** 3, T: 1024 ** 4, P: 1024 ** 5};
    return val * (mult[unit] || 1);
}

function formatEventTime(t) {
    if (!t) return '—';
    const date = toLocalDate(t);
    return date ? date.toLocaleString() : String(t);
}

function shortEventClass(cls) {
    return (cls || '').split('.').pop();
}

function zfsErrorTotal(disk) {
    const e = disk.zfs_errors;
    return e ? (e.cksum || 0) + (e.read || 0) + (e.write || 0) : 0;
}

function diskHealthState(disk) {
    if (disk.status === 'dead') return 'dead';
    if (disk.status === 'removed') return 'removed';
    if (!disk.device_name) return 'absent';
    if (disk.health_status === 'failing' || zfsErrorTotal(disk) > 0) return 'attention';
    return disk.health_status === 'ok' ? 'ok' : 'unknown';
}

function healthCell(disk) {
    const state = diskHealthState(disk);
    if (state === 'attention') {
        const bits = [];
        if (disk.health_status === 'failing') bits.push('SMART: failing');
        const e = disk.zfs_errors || {};
        if (e.cksum) bits.push(`ZFS checksum: ${e.cksum}`);
        if (e.read) bits.push(`ZFS read: ${e.read}`);
        if (e.write) bits.push(`ZFS write: ${e.write}`);
        return `<span class="badge badge-health-attention" title="${escapeHtml(bits.join('; ') || 'problems detected')}">
            <i class="fas fa-exclamation-triangle"></i> Attention</span>`;
    }
    if (state === 'dead') return '<span class="status status-dead">Dead</span>';
    if (state === 'removed' || state === 'absent') return '<span class="badge badge-removed">not present</span>';
    if (state === 'unknown') return '<span class="status status-unknown">Unknown</span>';
    return '<span class="status status-ok">OK</span>';
}

// ── Disk ───────────────────────────────────────────────────

async function diskVdevsFor(d) {
    if (!d.pools || !d.pools.length) return [];
    const keys = diskIdentityKeys(d);
    if (!keys.length) return [];
    const out = [];
    for (const pool of d.pools) {
        let st;
        try { st = await cachedPoolStatus(pool); } catch (e) { continue; }
        for (const g of [['data_vdevs', 'data'], ['special_vdevs', 'special'], ['log_vdevs', 'log'], ['cache_vdevs', 'cache']]) {
            const group = st[g[0]] || [];
            for (const v of group) {
                const kids = v.children || [];
                if (kids.some(k => matchChildToKeys(k, keys))) {
                    out.push({
                        lbl: 'Vdev',
                        kind: 'vdev',
                        key: vkey(pool, v.guid || v.name),
                        label: vdevLabel(g[1], v),
                        sub: `${v.state || 'UNKNOWN'} · ${kids.length} device(s)`,
                    });
                }
            }
        }
    }
    return out;
}

function smartSectionHtml(smart) {
    if (!smart) {
        return '<p class="empty-hint">No SMART data available (disk not present or no device path).</p>';
    }
    const parts = [];
    const status = smart.health_status === 'failing'
        ? '<span class="status status-failing">Failing</span>'
        : (smart.health_status === 'ok' ? '<span class="status status-ok">OK</span>' : '<span class="status status-unknown">Unknown</span>');
    parts.push(`<div style="margin-bottom:6px"><span class="detail-key">Overall health</span><span class="detail-val">${status}</span></div>`);
    if (smart.temperature !== null && smart.temperature !== undefined) {
        parts.push(`<div style="margin-bottom:6px"><span class="detail-key">Temperature</span><span class="detail-val">${escapeHtml(smart.temperature)} °C</span></div>`);
    }
    if (smart.power_on_hours !== null && smart.power_on_hours !== undefined) {
        parts.push(`<div style="margin-bottom:6px"><span class="detail-key">Power-on hours</span><span class="detail-val">${escapeHtml(smart.power_on_hours)}</span></div>`);
    }
    if (smart.nvme) {
        parts.push(`<div style="margin-bottom:6px"><span class="detail-key">NVMe</span><span class="detail-val">warning ${escapeHtml(smart.nvme.critical_warning)} &middot; used ${escapeHtml(smart.nvme.percentage_used)}% &middot; media errors ${escapeHtml(smart.nvme.media_errors)}</span></div>`);
    }
    if (smart.problems && smart.problems.length) {
        parts.push('<div style="margin-bottom:6px"><span class="detail-key">Problems</span><div class="detail-val">'
            + smart.problems.map(p => `<p class="problem-item problem-warn">${escapeHtml(p)}</p>`).join('')
            + '</div></div>');
    } else if (smart.attributes && smart.attributes.length) {
        parts.push('<p class="empty-hint" style="margin:4px 0 8px">No SMART problems detected.</p>');
    }
    if (smart.attributes && smart.attributes.length) {
        parts.push('<table class="smart-attr-table"><thead><tr><th>Attr</th><th>Value</th><th>Worst</th><th style="text-align:right">Raw</th><th></th></tr></thead><tbody>'
            + smart.attributes.map(a => {
                const bad = a.when_failed
                    ? `<span class="problem-fatal" title="${escapeHtml(a.when_failed)}">FAILED</span>`
                    : (a.raw ? '<span class="problem-warn">attention</span>' : '');
                return `<tr><td>${escapeHtml(a.name || a.id)}</td><td class="num">${a.value ?? '—'}</td><td class="num">${a.worst ?? '—'}</td><td class="num" style="text-align:right">${a.raw ?? '—'}</td><td>${bad}</td></tr>`;
            }).join('')
            + '</tbody></table>');
    }
    if (smart.self_test && smart.self_test.length) {
        parts.push('<h4 style="margin:12px 0 6px;font-size:12px;color:#777;text-transform:uppercase">Self-test log</h4>'
            + '<table class="event-table"><thead><tr><th>Type</th><th>Status</th><th>Remaining</th><th>Hours</th></tr></thead><tbody>'
            + smart.self_test.map(t => `<tr><td>${escapeHtml(t.type || '—')}</td><td class="${t.failed ? 'problem-fatal' : ''}">${escapeHtml(t.status)}</td><td>${escapeHtml(t.remaining ?? '—')}</td><td class="num">${escapeHtml(t.lifetime_hours ?? '—')}</td></tr>`).join('')
            + '</tbody></table>');
    }
    return parts.join('');
}

function zfsSectionHtml(zfs) {
    if (!zfs || (zfs.pool === null && !zfs.errors && (!zfs.events || zfs.events.length === 0))) {
        return '<p class="empty-hint">This disk is not part of a ZFS pool.</p>';
    }
    const parts = [];
    if (zfs.pool !== null && zfs.pool !== undefined) {
        parts.push(`<div style="margin-bottom:6px"><span class="detail-key">Pool</span><span class="detail-val">${escapeHtml(zfs.pool)}</span></div>`);
    }
    const e = zfs.errors;
    if (e !== null && e !== undefined) {
        const total = (e.cksum || 0) + (e.read || 0) + (e.write || 0);
        const statusHtml = total > 0
            ? '<span class="badge badge-health-attention">Errors detected</span>'
            : '<span class="status status-ok">No errors</span>';
        parts.push(`<div style="margin-bottom:8px"><span class="detail-key">Counters</span><span class="detail-val">${statusHtml}</span></div>`);
        parts.push('<table class="event-table"><tbody>'
            + `<tr><td>Read errors</td><td class="num">${e.read || 0}</td></tr>`
            + `<tr><td>Write errors</td><td class="num">${e.write || 0}</td></tr>`
            + `<tr><td>Checksum errors</td><td class="num">${e.cksum || 0}</td></tr>`
            + '</tbody></table>');
    }
    parts.push('<h4 style="margin:12px 0 6px;font-size:12px;color:#777;text-transform:uppercase">Recent ZFS error events</h4>');
    if (zfs.events && zfs.events.length) {
        parts.push('<table class="event-table"><thead><tr><th>Time</th><th>Event</th></tr></thead><tbody>'
            + zfs.events.slice(0, 50).map(ev => `<tr><td class="mono num">${escapeHtml(formatEventTime(ev.time))}</td><td>${escapeHtml(shortEventClass(ev.class))}</td></tr>`).join('')
            + '</tbody></table>');
    } else {
        parts.push('<p class="empty-hint">No recent ZFS error events recorded for this disk.</p>');
    }
    if (zfs.pool) {
        parts.push('<p class="empty-hint" style="margin-top:8px">Counters are cumulative since the last <code>zpool clear</code>; events cover only activity retained in the recent ring buffer.</p>');
    }
    return parts.join('');
}

async function renderDisk(id) {
    const payload = await cachedDiskHealth(id);
    const d = payload.disk || {};
    const deviceId = (d.by_id || '').replace('/dev/disk/by-id/', '') || 'unavailable';

    let roleRows = '';
    if (d.role === 'pool') {
        const pools = d.pools && d.pools.length ? d.pools : [d.role_detail || '?'];
        roleRows = pools.map(p =>
            `<div style="margin-bottom:6px"><span class="detail-key">Role</span><span class="detail-val">Pool: <a href="javascript:void(0)" onclick="Details.pool('${escapeHtml(p)}'); return false;">${escapeHtml(p)}</a></span></div>`).join('');
    } else {
        roleRows = `<div style="margin-bottom:6px"><span class="detail-key">Role</span><span class="detail-val">${escapeHtml(d.role || '—')}</span></div>`;
    }

    const summary = `
        <div style="margin-bottom:6px"><span class="detail-key">Device</span><span class="detail-val">${escapeHtml(d.device_name || '—')}</span></div>
        <div style="margin-bottom:6px"><span class="detail-key">Model</span><span class="detail-val">${escapeHtml(d.model || 'Unknown')}</span></div>
        <div style="margin-bottom:6px"><span class="detail-key">Serial</span><span class="detail-val">${escapeHtml(d.serial || '—')}</span></div>
        <div style="margin-bottom:6px"><span class="detail-key">Device ID</span><span class="detail-val mono">${escapeHtml(deviceId)}</span></div>
        <div style="margin-bottom:6px"><span class="detail-key">Size</span><span class="detail-val">${formatBytes(d.size_bytes)}</span></div>
        <div style="margin-bottom:6px"><span class="detail-key">Type</span><span class="detail-val">${escapeHtml(d.disk_type || '—')}</span></div>
        ${roleRows}
        <div><span class="detail-key">Health</span><span class="detail-val">${healthCell(d)}</span></div>`;

    const smart = smartSectionHtml(payload.smart);
    const zfs = zfsSectionHtml(payload.zfs);
    const adj = (d.role === 'pool') ? adjSection('Contributes to', await diskVdevsFor(d)) : '';

    const html = `
        <div class="disk-detail-grid">
            <div class="detail-col">
                <div class="disk-detail-section">
                    <h4>Device</h4>
                    <div>${summary}</div>
                </div>
                <div class="disk-detail-section">
                    <h4>SMART</h4>
                    <div>${smart}</div>
                </div>
                ${adj ? `<div class="disk-detail-section"><h4>Adjacency</h4>${adj}</div>` : ''}
            </div>
            <div class="detail-col">
                <div class="disk-detail-section">
                    <h4>ZFS Error History</h4>
                    <div>${zfs}</div>
                </div>
            </div>
        </div>`;

    return { title: `Details — ${d.device_name || d.serial || 'Disk #' + id}`, html };
}

// ── Vdev ───────────────────────────────────────────────────

async function renderVdev(key) {
    const { pool, guid } = parseVkey(key);
    const st = await cachedPoolStatus(pool);
    let v = null, groupName = 'data';
    for (const g of [['data_vdevs', 'data'], ['special_vdevs', 'special'], ['log_vdevs', 'log'], ['cache_vdevs', 'cache']]) {
        const arr = st[g[0]] || [];
        const found = arr.find(x => (x.guid || x.name) === guid);
        if (found) { v = found; groupName = g[1]; break; }
    }
    if (!v) {
        return { title: 'Vdev', html: '<p class="empty-hint">Vdev not found.</p>' };
    }

    const disks = await cachedDisksList();
    const diskEntries = disks.map(d => ({ d, keys: diskIdentityKeys(d) }));

    const kids = (v.children || []).map(c => {
        const match = diskEntries.find(e => e.keys.length && matchChildToKeys(c, e.keys));
        const label = escapeHtml(c.name || c.path || '?');
        const size = c.size ? ` <span style="opacity:0.6">${c.size}</span>` : '';
        const state = c.state ? ` <span class="status status-${c.state}">${c.state}</span>` : '';
        const nameHtml = match
            ? adjLink('disk', String(match.d.id), label, match.d.model || '')
            : label;
        return `<div style="padding-left:8px">${nameHtml}${size}${state}</div>`;
    }).join('');

    const header = `
        <div style="margin-bottom:12px">
            <span class="badge badge-${groupName === 'data' ? 'data' : groupName}">${escapeHtml(groupName)}</span>
            <span class="topology" style="margin-left:8px">${escapeHtml(v.type || 'stripe')}</span>
            <span class="status status-${escapeHtml(v.state || 'UNKNOWN')}" style="margin-left:8px">${escapeHtml(v.state || 'UNKNOWN')}</span>
        </div>`;
    const capacity = v.total_space
        ? `<div style="margin-bottom:12px"><strong>Capacity:</strong> ${v.alloc_space ? formatBytes(parseSize(v.alloc_space)) + ' used / ' : ''}${formatBytes(parseSize(v.total_space))}</div>`
        : '';

    const belowItems = diskEntries.filter(e => e.keys.length && (v.children || []).some(c => matchChildToKeys(c, e.keys)))
        .map(e => ({
            lbl: 'Disk',
            kind: 'disk',
            key: String(e.d.id),
            label: e.d.device_name || e.d.serial || 'Disk #' + e.d.id,
            sub: e.d.model || '',
        }));
    const adj = adjSection('Composed of', belowItems)
        + adjSection('Part of', [{
            lbl: 'Pool',
            kind: 'pool',
            key: pool,
            label: pool,
            sub: st.topology || '',
        }]);

    const html = `
        ${header}
        ${capacity}
        <div style="margin-bottom:8px"><strong>Devices</strong>${kids ? `<div style="margin:4px 0 0 16px;list-style:none">${kids}</div>` : ': no devices'}</div>
        ${adj}`;

    return { title: `Vdev — ${vdevLabel(groupName, v)} (${pool})`, html };
}

// ── Pool ───────────────────────────────────────────────────

function physicalLabel(bytes) {
    if (!bytes) return '';
    const label = (bytes >= 1024 && bytes % 1024 === 0) ? (bytes / 1024) + 'K' : bytes + 'B';
    return label + ' sectors (physical)';
}

function sectorLabel(a) {
    if (!a) return '';
    const b = 1 << a;
    const label = (b >= 1024 && b % 1024 === 0) ? (b / 1024) + 'K' : b + 'B';
    return label + ' sectors (ashift ' + a + ')';
}

function vdevSummary(vdevs, label, pool) {
    if (!vdevs || vdevs.length === 0) return '';
    const items = vdevs.map(v => {
        const type = v.type || 'unknown';
        const withAshift = (v.children || []).filter(c => c.ashift);
        const groupSector = v.physical_sector_size
            ? physicalLabel(v.physical_sector_size)
            : sectorLabel(v.ashift || (withAshift.length ? withAshift[0].ashift : 0));
        const devs = (v.children || []).map(c => {
            const state = c.state || '';
            const size = c.size || '';
            const sector = c.physical_sector_size ? physicalLabel(c.physical_sector_size) : sectorLabel(c.ashift);
            return `<div style="padding-left:8px">${c.name || '?'} ${sector} <span style="opacity:0.6">${size}</span> <span class="status status-${state}">${state}</span></div>`;
        }).join('');
        const link = `<a href="javascript:void(0)" onclick="Details.vdev('${pool}', '${v.guid || v.name}')" style="color:#3498db">${type}</a>`;
        return `<li><strong>${link}</strong>${groupSector ? ` <span style="opacity:0.7">${groupSector}</span>` : ''}${devs ? '' : ': no devices'}${devs}</li>`;
    }).join('');
    return `<div style="margin-bottom:8px"><strong>${label}</strong><ul style="margin:4px 0 0 16px;list-style:none">${items}</ul></div>`;
}

function vdevSize(vdevs) {
    if (!vdevs || vdevs.length === 0) return null;
    let total = 0;
    for (const v of vdevs) {
        const kids = v.children || [];
        const sizes = kids.map(c => parseSize(c.size)).filter(s => s > 0);
        if (sizes.length === 0) continue;
        const type = v.type || 'stripe';
        if (type === 'mirror') {
            total += Math.min(...sizes);
        } else if (type === 'raidz1') {
            total += sizes.reduce((a, b) => a + b, 0) - Math.min(...sizes);
        } else if (type === 'raidz2') {
            const sorted = [...sizes].sort((a, b) => a - b);
            total += sizes.reduce((a, b) => a + b, 0) - sorted[0] - sorted[1];
        } else if (type === 'raidz3') {
            const sorted = [...sizes].sort((a, b) => a - b);
            total += sizes.reduce((a, b) => a + b, 0) - sorted[0] - sorted[1] - sorted[2];
        } else {
            total += sizes.reduce((a, b) => a + b, 0);
        }
    }
    return total || null;
}

async function renderPool(name) {
    const status = await cachedPoolStatus(name);
    const datasets = await cachedDatasetsFor(name);

    const dataUsable = vdevSize(status.data_vdevs);
    const rawSize = (status.data_vdevs || []).concat(status.special_vdevs || [], status.log_vdevs || [], status.cache_vdevs || [])
        .flatMap(v => (v.children || []).map(c => parseSize(c.size)))
        .reduce((a, b) => a + b, 0);

    let html = `
        <div style="margin-bottom:12px">
            <span class="status status-${status.status}" style="font-size:1.1em">${status.status}</span>
            <span class="topology" style="margin-left:8px">${status.topology}</span>
        </div>`;
    if (rawSize > 0) {
        html += `<div style="margin-bottom:12px"><strong>Raw capacity:</strong> ${formatBytes(rawSize)}</div>`;
    }
    if (dataUsable && dataUsable !== rawSize) {
        html += `<div style="margin-bottom:12px"><strong>Usable (data):</strong> ${formatBytes(dataUsable)}</div>`;
    }

    html += vdevSummary(status.data_vdevs, 'Data Vdevs', name);
    html += vdevSummary(status.special_vdevs, 'Special Vdevs', name);
    html += vdevSummary(status.log_vdevs, 'Log Vdevs', name);
    html += vdevSummary(status.cache_vdevs, 'Cache Vdevs', name);

    if (status.scan && status.scan.state) {
        html += `<div style="margin-top:8px"><strong>Last scrub:</strong> ${status.scan.state} ${status.scan.end_time || ''}</div>`;
    }

    const below = [];
    for (const g of [['data_vdevs', 'data'], ['special_vdevs', 'special'], ['log_vdevs', 'log'], ['cache_vdevs', 'cache']]) {
        (status[g[0]] || []).forEach(v => below.push({
            lbl: 'Vdev',
            kind: 'vdev',
            key: vkey(name, v.guid || v.name),
            label: vdevLabel(g[1], v),
            sub: `${v.state || 'UNKNOWN'} · ${(v.children || []).length} device(s)`,
        }));
    }
    const above = datasets.map(d => ({
        lbl: 'Dataset',
        kind: 'dataset',
        key: d.name,
        label: d.name,
        sub: d.mountpoint || '',
    }));

    html += adjSection('Composed of (vdevs)', below);
    html += adjSection('Contains (datasets)', above);

    return { title: `Pool "${name}"`, html };
}

// ── Dataset ────────────────────────────────────────────────

async function renderDataset(name) {
    const ds = await api.getDataset(name);
    const pool = name.split('/')[0];
    const [nfs, smb] = await Promise.all([cachedNfsShares(), cachedSmbShares()]);
    const nfsShare = nfs.find(s => s.dataset_name === name);
    const smbShare = smb.find(s => s.dataset_name === name);

    const rows = [
        ['Name', ds.name],
        ['Pool', pool],
        ['Compression', ds.compression || '-'],
        ['Record Size', ds.recordsize || '-'],
        ['Sync', ds.sync_mode || '-'],
        ['Atime', ds.atime || '-'],
        ['CanMount', ds.canmount || '-'],
        ['Read Only', ds.readonly || '-'],
        ['Quota', ds.quota || '-'],
        ['Special Blocks', ds.special_small_blocks || '0'],
        ['Mountpoint', ds.mountpoint || '-'],
        ['Used', ds.used || '-'],
        ['Available', ds.available || '-'],
        ['Referenced', ds.referenced || '-'],
        ['Created', ds.created_at ? formatDate(ds.created_at) : '-'],
    ].map(([k, v]) =>
        `<div style="margin-bottom:6px"><span class="detail-key">${escapeHtml(k)}</span><span class="detail-val">${escapeHtml(v)}</span></div>`).join('');

    const shares = [];
    if (nfsShare) {
        shares.push({
            lbl: 'NFS',
            kind: 'share',
            key: skey('nfs', name),
            label: nfsShare.export_path,
            sub: nfsShare.enabled ? 'Shared' : 'Paused',
        });
    }
    if (smbShare) {
        shares.push({
            lbl: 'SMB',
            kind: 'share',
            key: skey('smb', name),
            label: smbShare.share_name,
            sub: smbShare.read_only ? 'Read-only' : 'Read-write',
        });
    }

    const adj = adjSection('Stored in', [{
        lbl: 'Pool',
        kind: 'pool',
        key: pool,
        label: pool,
        sub: '',
    }]) + adjSection('Shared as', shares);

    const html = `
        <div style="margin-bottom:12px">
            <span class="status status-online">Dataset</span>
            <code style="margin-left:8px">${escapeHtml(ds.name)}</code>
        </div>
        ${rows}
        ${adj}`;

    return { title: `Dataset "${ds.name}"`, html };
}

// ── Share ──────────────────────────────────────────────────

async function renderShare(key) {
    const { proto, ds } = parseSkey(key);
    let s, title;
    if (proto === 'nfs') {
        const list = await cachedNfsShares();
        s = list.find(x => x.dataset_name === ds) || null;
        title = `NFS Share — ${ds}`;
    } else {
        const list = await cachedSmbShares();
        s = list.find(x => x.dataset_name === ds) || null;
        title = `SMB Share — ${ds}`;
    }
    if (!s) {
        return { title: 'Share', html: '<p class="empty-hint">Share not found.</p>' };
    }

    const pool = ds.split('/')[0];
    let rows;
    if (proto === 'nfs') {
        rows = [
            ['Type', 'NFS'],
            ['Dataset', ds],
            ['Export Path', s.export_path],
            ['Share Options', s.sharenfs || 'not set'],
            ['Status', s.enabled ? 'Shared' : 'Paused'],
        ];
    } else {
        rows = [
            ['Type', 'SMB'],
            ['Dataset', ds],
            ['Share Name', s.share_name],
            ['Path', s.share_path],
            ['Access', s.read_only ? 'Read-only' : 'Read-write'],
            ['Status', s.enabled ? 'Shared' : 'Disabled'],
        ];
    }
    const rowsHtml = rows.map(([k, v]) =>
        `<div style="margin-bottom:6px"><span class="detail-key">${escapeHtml(k)}</span><span class="detail-val">${escapeHtml(v)}</span></div>`).join('');

    const adj = adjSection('Based on', [{
        lbl: 'Dataset',
        kind: 'dataset',
        key: ds,
        label: ds,
        sub: pool,
    }]);

    const html = `
        <div style="margin-bottom:12px">
            <span class="badge badge-${proto === 'nfs' ? 'log' : 'cache'}">${proto === 'nfs' ? 'NFS' : 'SMB'}</span>
            <code style="margin-left:8px">${escapeHtml(ds)}</code>
        </div>
        ${rowsHtml}
        ${adj}`;

    return { title, html };
}

const DetailsRenderers = {
    disk: renderDisk,
    vdev: renderVdev,
    pool: renderPool,
    dataset: renderDataset,
    share: renderShare,
};

window.Details = {
    open(kind, key) {
        const modal = document.getElementById('details-modal');
        if (!modal) return;
        modal.style.display = 'flex';
        const title = document.getElementById('details-modal-title');
        const body = document.getElementById('details-modal-body');
        title.textContent = 'Loading...';
        body.innerHTML = '<p class="loading">Loading details...</p>';

        const renderer = DetailsRenderers[kind];
        (renderer ? renderer(key) : Promise.resolve({ title: 'Unknown', html: '<p class="empty-hint">Unknown layer.</p>' }))
            .then(result => {
                title.textContent = result.title;
                body.innerHTML = result.html;
            })
            .catch(error => {
                console.error('Failed to load details:', error);
                title.textContent = 'Details';
                body.innerHTML = `<p class="empty-hint">Failed to load details: ${escapeHtml(error.message)}</p>`;
            });
    },

    close() {
        const modal = document.getElementById('details-modal');
        if (modal) modal.style.display = 'none';
    },

    disk(id) { this.open('disk', String(id)); },
    vdev(pool, guid) { this.open('vdev', vkey(pool, guid)); },
    pool(name) { this.open('pool', name); },
    dataset(name) { this.open('dataset', name); },
    share(proto, ds) { this.open('share', skey(proto, ds)); },
};