// Shared canvas chart helpers for NAZMan dashboard + monitoring pages.
// Loaded via base.html so both pages use one source of truth.

function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
    return (bytes / 1073741824).toFixed(1) + ' GB';
}

function updateUsageColor(elementId, percent) {
    const element = document.getElementById(elementId);
    if (!element) return;
    element.classList.remove('low', 'medium', 'high');
    if (percent < 50) element.classList.add('low');
    else if (percent < 80) element.classList.add('medium');
    else element.classList.add('high');
}

function getColorForPercent(percent) {
    if (percent < 50) return '#27ae60';
    if (percent < 80) return '#f39c12';
    return '#e74c3c';
}

// deterministic color per series name for stable multi-line rendering
const SERIES_COLORS = ['#3498db', '#9b59b6', '#e67e22', '#16a085', '#c0392b', '#2980b9', '#8e44ad', '#d35400', '#27ae60', '#2c3e50'];
function seriesColor(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return SERIES_COLORS[h % SERIES_COLORS.length];
}
// Back-compat alias used by the dashboard disk legend.
function diskColor(name) { return seriesColor(name); }

// Draw a single filled-line sparkline on a shared 0-100 axis.
function drawSparkline(canvasId, data, color) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = rect.width || canvas.parentElement.clientWidth || 150;
    const h = rect.height || 40;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    if (data.length < 2) return;

    const maxPoints = Math.max(data.length, 2);
    const step = w / (maxPoints - 1);

    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
        const x = i * step;
        const y = h - (data[i] / 100) * h;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.stroke();

    ctx.lineTo((data.length - 1) * step, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = color + '18';
    ctx.fill();
}

// Draw multiple overlaid series as lines on a shared 0-100 axis.
// seriesList: [{name, data:[0..100]}] ; colors[i] per series.
function drawMultiline(canvasId, seriesList, colors, opts) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = rect.width || canvas.parentElement.clientWidth || 260;
    const h = rect.height || 80;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    const showGrid = !(opts && opts.noGrid);

    // gridlines at 25/50/75%
    if (showGrid) {
        ctx.strokeStyle = 'rgba(0,0,0,0.08)';
        ctx.lineWidth = 1;
        [0.25, 0.5, 0.75].forEach(f => {
            const y = h - f * h;
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(w, y);
            ctx.stroke();
        });
    }

    const drawn = seriesList.filter(s => s.data && s.data.length >= 2);
    let maxPointCount = 2;
    drawn.forEach(s => { maxPointCount = Math.max(maxPointCount, s.data.length); });

    drawn.forEach((s, idx) => {
        const data = s.data;
        const color = colors[idx];
        const step = w / (maxPointCount - 1);
        ctx.beginPath();
        // Align points to the right edge so short live series don't sit at left.
        const offset = maxPointCount - data.length;
        for (let i = 0; i < data.length; i++) {
            const x = (i + offset) * step;
            const v = Math.max(0, Math.min(100, data[i]));
            const y = h - (v / 100) * h;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.lineJoin = 'round';
        ctx.stroke();
    });
}