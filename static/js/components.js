// UI Components for NAZMan

// Escape HTML entities to prevent XSS
function escapeHtml(text) {
    if (!text) return '';
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.toString().replace(/[&<>"']/g, m => map[m]);
}

// Fly-in alert (toast) history. Every message is kept in localStorage so
// transient alerts (which auto-dismiss after a few seconds) can be reviewed
// later from the Messages modal in the header.
const MESSAGE_LOG_KEY = 'nazman_messages';
const MESSAGE_LOG_MAX = 200;

let messageHistory = loadMessageHistory();

function loadMessageHistory() {
    try {
        const parsed = JSON.parse(localStorage.getItem(MESSAGE_LOG_KEY) || '[]');
        return Array.isArray(parsed) ? parsed.slice(0, MESSAGE_LOG_MAX) : [];
    } catch (e) {
        return [];
    }
}

function saveMessageHistory() {
    try {
        localStorage.setItem(MESSAGE_LOG_KEY, JSON.stringify(messageHistory.slice(0, MESSAGE_LOG_MAX)));
    } catch (e) {
        // Storage unavailable/full; the in-memory log still works for this page.
    }
}

// Alert function
function showAlert(message, type = 'success', duration = 3000) {
    messageHistory.unshift({ ts: new Date().toISOString(), type, message: String(message) });
    saveMessageHistory();

    const alert = document.createElement('div');
    alert.className = `alert alert-${type}`;
    alert.textContent = message;
    document.body.appendChild(alert);

    setTimeout(() => {
        alert.remove();
    }, duration);
}

function renderMessageHistory() {
    const list = document.getElementById('message-history-list');
    if (!list) return;
    if (messageHistory.length === 0) {
        list.innerHTML = '<p class="text-muted">No messages yet.</p>';
        return;
    }
    list.innerHTML = messageHistory.map(m =>
        `<div class="message-entry message-${m.type}">
            <span class="message-ts">${formatDate(m.ts)}</span>
            <span class="message-text">${escapeHtml(m.message)}</span>
        </div>`
    ).join('');
}

function openMessageHistory() {
    renderMessageHistory();
    showModal('message-history-modal');
}

function clearMessageHistory() {
    messageHistory = [];
    saveMessageHistory();
    renderMessageHistory();
}

// Format bytes to human readable
function formatBytes(bytes, decimals = 2) {
    if (!bytes || bytes === 0) return '0 Bytes';
    
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB', 'PB'];
    
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

// Format date
function formatDate(dateString) {
    if (!dateString) return 'Never';
    
    const date = new Date(dateString);
    return date.toLocaleString();
}

// Create loading element
function createLoading() {
    return '<p class="loading"><i class="fas fa-spinner fa-spin"></i> Loading...</p>';
}

// Create empty state
function createEmptyState(message, link = null) {
    let html = `<p class="empty-state">${message}`;
    if (link) {
        html += ` <a href="${link.url}">${link.text}</a>`;
    }
    html += '</p>';
    return html;
}

// Create error state
function createErrorState(message) {
    return `<p class="error"><i class="fas fa-exclamation-circle"></i> ${message}</p>`;
}

// Confirm dialog
function confirmAction(message, onConfirm, onCancel = null) {
    if (confirm(message)) {
        onConfirm();
    } else if (onCancel) {
        onCancel();
    }
}

// Debounce function
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// Throttle function
function throttle(func, limit) {
    let inThrottle;
    return function(...args) {
        if (!inThrottle) {
            func.apply(this, args);
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    };
}

// Simple event emitter
class EventEmitter {
    constructor() {
        this.events = {};
    }
    
    on(event, callback) {
        if (!this.events[event]) {
            this.events[event] = [];
        }
        this.events[event].push(callback);
    }
    
    off(event, callback) {
        if (!this.events[event]) return;
        this.events[event] = this.events[event].filter(cb => cb !== callback);
    }
    
    emit(event, ...args) {
        if (!this.events[event]) return;
        this.events[event].forEach(callback => callback(...args));
    }
}

// Create global event emitter
const events = new EventEmitter();

// Shared device picker used by pool vdev selection and backup-disk declaration.
// Options:
//   single                 input type: radio (single) vs checkbox (multi)
//   excludeOsDisks         skip OS disks entirely (backup declare)
//   alwaysAllowWholeDisk   always offer the whole disk, even when partitioned
//   disabledWhole          Set of disk ids whose whole disk is unavailable
//   disabledPartitions     Map<diskId, Set<slotUuid>> of unavailable partitions
window.DevicePicker = {
    render(container, opts) {
        container.innerHTML = this.renderHtml(opts);
    },

    clear(container) {
        container.querySelectorAll('.device-pick-radio:checked, .device-pick-cb:checked')
            .forEach(el => { el.checked = false; });
    },

    renderHtml({ disks, partitionsByDisk, single = false, excludeOsDisks = false,
                 alwaysAllowWholeDisk = false, disabledWhole, disabledPartitions,
                 hideUsed = false }) {
        const wholeUsed = disabledWhole || new Set();
        const partUsed = disabledPartitions || {};
        const cls = single ? 'device-pick-radio' : 'device-pick-cb';
        const type = single ? 'radio' : 'checkbox';

        const rows = [];
        for (const disk of disks) {
            if (disk.status === 'dead') continue;
            if (excludeOsDisks && disk.is_os_disk) continue;
            const parts = partitionsByDisk[disk.id] || [];
            const diskIdKey = String(disk.id);
            const usedParts = partUsed[diskIdKey] || new Set();

            const wholeDisabled = wholeUsed.has(diskIdKey)
                || parts.some(p => usedParts.has(p.slot_uuid));
            if (!(hideUsed && wholeDisabled)
                && (alwaysAllowWholeDisk || parts.length === 0)) {
                rows.push(this._labelHtml({
                    cls, type, disabled: wholeDisabled, key: diskIdKey,
                    main: disk.device_name,
                    sub: `${formatBytes(disk.size_bytes)} ${disk.disk_type || ''}`,
                    tag: 'whole disk',
                    group: single ? 'device-pick-radio' : null,
                }));
            }

            for (const p of parts) {
                const disabled = (disk.is_os_disk && p.reserved)
                    || wholeUsed.has(diskIdKey)
                    || usedParts.has(p.slot_uuid);
                if (hideUsed && disabled) continue;
                rows.push(this._labelHtml({
                    cls, type, disabled, key: `${diskIdKey}:${p.slot_uuid}`,
                    main: `${disk.device_name} p${p.number}`,
                    sub: formatBytes(p.size_bytes),
                    tag: (disk.is_os_disk && p.reserved)
                        ? 'reserved (OS)'
                        : (p.slot_uuid || '').substring(0, 8) + '...',
                    group: single ? 'device-pick-radio' : null,
                }));
            }
        }

        if (rows.length === 0) {
            return '<p class="empty-state">No available devices.</p>';
        }
        return rows.join('');
    },

    _labelHtml({ cls, type, disabled, key, main, sub, tag, group }) {
        return `
        <label class="device-picker-item" style="display:flex;align-items:center;gap:8px;padding:6px 0;cursor:${disabled ? 'not-allowed' : 'pointer'};opacity:${disabled ? '0.4' : '1'}">
            <input type="${type}" class="${cls}" value="${key}" ${group ? `name="${group}"` : ''} ${disabled ? 'disabled' : ''}>
            <span style="flex:1">
                <span>${main}</span>
                <small class="text-muted" style="margin-left:8px">${sub}</small>
            </span>
            <small class="text-muted">${tag}</small>
        </label>`;
    },

    selectedKeys(container) {
        return [...container.querySelectorAll('.device-pick-radio:checked, .device-pick-cb:checked')]
            .map(el => el.value);
    },
};

document.addEventListener('DOMContentLoaded', function () {
    const messagesBtn = document.getElementById('messages-btn');
    if (messagesBtn) messagesBtn.addEventListener('click', openMessageHistory);
    const clearBtn = document.getElementById('messages-clear-btn');
    if (clearBtn) clearBtn.addEventListener('click', clearMessageHistory);
});
