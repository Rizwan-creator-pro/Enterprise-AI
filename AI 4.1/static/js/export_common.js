// ==============================================================================
// Dream Enterprise AI Hub - Core Helpers & Library Loader
// ==============================================================================

const FILE_COMPANY = 'Dream';
const PDF_COMPANY = 'Dream';
const PDF_CREDIT = 'Software Created By: M. RIZWAN - 03237004270';

// Heavy export libraries loaded on demand
const LIBS = {
    docx: {
        ok: () => window.docx && window.docx.Document,
        urls: [
            '/static/lib/docx.js',
            'https://cdn.jsdelivr.net/npm/docx@8.5.0/build/index.js',
            'https://cdn.jsdelivr.net/npm/docx@8.2.4/build/index.js',
            'https://cdnjs.cloudflare.com/ajax/libs/docx/7.1.0/docx.js',
            'https://unpkg.com/docx@8.5.0/build/index.js'
        ]
    },
    pptx: {
        ok: () => window.PptxGenJS,
        urls: [
            '/static/lib/pptxgen.bundle.js',
            'https://cdn.jsdelivr.net/npm/pptxgenjs@3.12.0/dist/pptxgen.bundle.js',
            'https://cdnjs.cloudflare.com/ajax/libs/pptxgenjs/3.12.0/pptxgen.bundle.js',
            'https://unpkg.com/pptxgenjs@3.12.0/dist/pptxgen.bundle.js'
        ]
    },
    zip: {
        ok: () => window.JSZip,
        urls: [
            '/static/lib/jszip.min.js',
            'https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js',
            'https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js',
            'https://unpkg.com/jszip@3.10.1/dist/jszip.min.js'
        ]
    }
};

const libLoading = {};

function loadScript(u) {
    return new Promise(res => {
        const s = document.createElement('script');
        s.src = u;
        s.async = true;
        s.onload = () => res(true);
        s.onerror = () => { s.remove(); res(false); };
        document.head.appendChild(s);
    });
}

function needLib(k) {
    const L = LIBS[k];
    if (L.ok()) return Promise.resolve();
    if (libLoading[k]) return libLoading[k];
    libLoading[k] = (async () => {
        const amd = window.define;
        try { window.define = undefined; } catch (e) {}
        try {
            for (const u of L.urls) {
                await loadScript(u);
                if (L.ok()) return;
            }
        } finally {
            if (amd) window.define = amd;
        }
        throw new Error('Could not load ' + k + ' library. Please check your internet connection.');
    })().finally(() => { delete libLoading[k]; });
    return libLoading[k];
}

function preloadLibs() {
    ['docx', 'pptx', 'zip'].forEach(k => needLib(k).catch(() => {}));
}

// Language / Script detection
const ARABIC_RE = /[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]/;
const COMPLEX_RE = /[\u0590-\u05FF\u0900-\u0DFF\u0E00-\u0EFF\u1100-\u11FF\u2E80-\u9FFF\uAC00-\uD7FF\uF900-\uFAFF\uFF00-\uFFEF\u{1F300}-\u{1FAFF}]/u;

const rtlT = t => /[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]/.test(t || '');
const hasArabic = t => ARABIC_RE.test(t || '');
const hasComplex = t => COMPLEX_RE.test(t || '');

const scriptOpts = t => rtlT(t) ? { rightToLeft: true, font: 'Arial' } : hasComplex(t) ? { font: 'Microsoft YaHei' } : {};
const pptFace = t => rtlT(t) ? { rtlMode: true, fontFace: 'Arial' } : hasComplex(t) ? { fontFace: 'Microsoft YaHei' } : {};
const xlFont = t => rtlT(t) ? { name: 'Arial' } : hasComplex(t) ? { name: 'Microsoft YaHei' } : {};

const niceTitle = t => String(t || 'Report').toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
const stamp = () => {
    const d = new Date(), p = n => String(n).padStart(2, '0');
    return `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const summaryLines = s => String(s || '').split('\n').map(l => l.trim()).filter(Boolean);
const sheetSafe = n => String(n || '').replace(/[*?:\\\/\[\]]/g, '-').trim().slice(0, 31) || 'Sheet1';
const dataUrlBytes = u => Uint8Array.from(atob(u.split(',')[1]), c => c.charCodeAt(0));

const numCols = ({ head, body }) => head.map((_, i) => body.some(r => String(r[i] ?? '').trim() !== '') &&
    body.every(r => { const v = String(r[i] ?? '').replace(/[,$%\s]|Rs\.?/gi, ''); return v === '' || !isNaN(v); }));

const colWeights = ({ head, body }) => head.map((h, i) => Math.min(Math.max(String(h).length, ...body.slice(0, 200).map(r => String(r[i] ?? '').length), 5), 28));

function collectCharts(box) {
    if (!box || !window.echarts) return [];
    const dupeObj = o => JSON.parse(JSON.stringify(o));
    const renderOne = (opt, w, h, defaultTitle) => {
        const tmp = document.createElement('div');
        tmp.style.cssText = `position:fixed;left:-10000px;top:0;width:${w}px;height:${h + 36}px`;
        document.body.appendChild(tmp);
        let img = '', title = '';
        try {
            const ch = echarts.init(tmp, null, { width: w, height: h + 36 });
            const o = typeof exportOption === 'function' ? exportOption(opt, w) : dupeObj(opt);
            o.animation = false;
            o.toolbox = { show: false };
            title = ([].concat(o.title || [])[0] || {}).text || defaultTitle || '';
            ch.setOption(o);
            img = ch.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#fff' });
            ch.dispose();
        } catch (e) {
            console.warn('chart export failed', e);
        }
        tmp.remove();
        return img ? { opt: dupeObj(opt), title: title || defaultTitle, img, w, h: h + 36 } : null;
    };

    return [...box.querySelectorAll('.echarts-container.rendered')].flatMap((el, i) => {
        const live = echarts.getInstanceByDom(el);
        if (!live || !el._raw) return [];
        const w = Math.max(live.getWidth(), 560);
        const h = Math.max(live.getHeight(), 300);
        const raw = el._raw;
        const series = [].concat(raw.series || []);
        const baseTitle = ([].concat(raw.title || [])[0] || {}).text || `Chart ${i + 1}`;

        // Check if multi-series needs splitting (different scales or multiple metrics)
        if (series.length > 1 && series.every(s => ['bar', 'line'].includes(s.type || 'bar'))) {
            const seriesStats = series.map(s => {
                const vals = (s.data || [])
                    .map(v => typeof v === 'object' && v !== null ? v.value : v)
                    .map(v => Number(v))
                    .filter(v => !isNaN(v));
                const maxV = vals.length ? Math.max(...vals.map(Math.abs)) : 0;
                return { name: s.name || 'Series', max: maxV };
            });
            const maxes = seriesStats.map(s => s.max).filter(m => m > 0);
            const ratio = maxes.length >= 2 ? Math.max(...maxes) / Math.max(Math.min(...maxes), 0.001) : 1;
            const diffMetrics = seriesStats.some(s => /count|emp|qty|num/i.test(s.name)) &&
                                seriesStats.some(s => /salary|amount|total|rev|cost|price/i.test(s.name));

            if (ratio > 2.5 || diffMetrics) {
                // Split each series into its own dedicated chart for clean print visibility
                const splitResults = series.map((s, sIdx) => {
                    const sOpt = dupeObj(raw);
                    sOpt.series = [ dupeObj(s) ];
                    delete sOpt.series[0].yAxisIndex;
                    if (Array.isArray(sOpt.yAxis)) {
                        const targetY = sOpt.yAxis[s.yAxisIndex || 0] || sOpt.yAxis[0] || { type: 'value' };
                        sOpt.yAxis = dupeObj(targetY);
                    }
                    if (sOpt.yAxis) {
                        sOpt.yAxis.position = 'left';
                        sOpt.yAxis.name = s.name || '';
                        sOpt.yAxis.splitLine = { show: true };
                    }
                    delete sOpt.legend;
                    const sTitle = baseTitle.toLowerCase().includes((s.name || '').toLowerCase())
                        ? baseTitle
                        : `${baseTitle} — ${s.name || `Metric ${sIdx + 1}`}`;
                    sOpt.title = {
                        text: sTitle,
                        subtext: ([].concat(raw.title || [])[0] || {}).subtext || ''
                    };
                    return renderOne(sOpt, w, h, sTitle);
                }).filter(Boolean);

                if (splitResults.length > 0) return splitResults;
            }
        }

        const single = renderOne(raw, w, h, baseTitle);
        return single ? [single] : [];
    });
}

function typeCell(raw) {
    const s = String(raw ?? '').trim(), clean = s.replace(/,/g, '');
    let m;
    if (s === '') return { v: '' };
    if ((m = s.match(/^(\$|Rs\.?)\s?(-?[\d,]+(?:\.\d+)?)$/i))) {
        const n = Number(m[2].replace(/,/g, ''));
        if (!isNaN(n)) return { v: n, fmt: (m[1] === '$' ? '"$"' : '"Rs"') + '#,##0.00', right: true };
    }
    if ((m = s.match(/^(-?\d+(?:\.\d+)?)\s*%$/))) return { v: parseFloat(m[1]) / 100, fmt: '0.00%', right: true };
    if ((m = s.match(/^(\d{2})[\/-](\d{2})[\/-](\d{4})(?:\s+(\d{2}):(\d{2}))?$/)))
        return { v: new Date(Date.UTC(+m[3], +m[2] - 1, +m[1], +(m[4] || 0), +(m[5] || 0))), fmt: m[4] ? 'dd/mm/yyyy hh:mm' : 'dd/mm/yyyy', center: true };
    if ((m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/))) return { v: new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])), fmt: 'yyyy-mm-dd', center: true };
    if ((m = s.match(/^(\d{2})\/(\d{2})\/(\d{2})$/))) return { v: new Date(Date.UTC(2000 + +m[3], +m[2] - 1, +m[1])), fmt: 'dd/mm/yy', center: true };
    if (/^-?\d+(\.\d+)?$/.test(clean) && !/^-?0\d/.test(clean) && clean.replace(/[-.]/g, '').length <= 15)
        return { v: Number(clean), fmt: clean.includes('.') ? '#,##0.00' : '#,##0', right: true };
    return { v: s };
}

function saveBlob(blob, name) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function fileBase(t) {
    return (String(t || 'report').replace(/[^\w\- ]+/g, '').trim().replace(/\s+/g, '_').slice(0, 40) || 'report') + '_' + new Date().toISOString().slice(0, 10);
}
