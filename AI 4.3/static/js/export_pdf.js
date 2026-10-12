// ==============================================================================
// Dream Enterprise AI Hub - PDF Export Module
// ==============================================================================

let _pdfFontP = null;

function ensurePdfFont() {
    if (_pdfFontP) return _pdfFontP;
    _pdfFontP = (async () => {
        for (const u of ['/static/fonts/Amiri-Regular.ttf', 'https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/amiri/Amiri-Regular.ttf']) {
            try {
                const r = await fetch(u);
                if (!r.ok) continue;
                const bytes = new Uint8Array(await r.arrayBuffer());
                let bin = '';
                for (let i = 0; i < bytes.length; i += 0x8000) {
                    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
                }
                return btoa(bin);
            } catch (e) {}
        }
        return null;
    })();
    _pdfFontP.then(v => { if (!v) _pdfFontP = null; });
    return _pdfFontP;
}

function textImg(text, pt, bold) {
    const S = 4, px = pt * S, c = document.createElement('canvas'), x = c.getContext('2d');
    const font = `${bold ? 'bold ' : ''}${px}px "Noto Sans SC","Microsoft YaHei","PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Noto Sans Devanagari","Nirmala UI","Noto Sans Thai","Leelawadee UI","Malgun Gothic","Segoe UI Emoji","Segoe UI",Arial,sans-serif`;
    x.font = font;
    const w = Math.ceil(x.measureText(text).width) + 8, h = Math.ceil(px * 1.5);
    c.width = w; c.height = h;
    x.font = font; x.fillStyle = '#141414'; x.textBaseline = 'middle';
    x.fillText(text, 4, h / 2);
    const mm = 0.3528 / S;
    return { url: c.toDataURL('image/png'), w: w * mm, h: h * mm };
}

function sanitizeForPdf(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/₹\s*/g, 'Rs. ')
        .replace(/[\u2018\u2019]/g, "'")
        .replace(/[\u201C\u201D]/g, '"')
        .replace(/[\u2013\u2014]/g, '-')
        .replace(/\u2026/g, '...')
        .replace(/\u00A0/g, ' ')
        .replace(/[▍▎▏▌]/g, '')
        .replace(/\*\*/g, '')
        .replace(/`/g, '');
}

async function buildPDF({ head, body }, title, summary = '', charts = []) {
    if (!window.jspdf || !window.jspdf.jsPDF) {
        showError('error', 'PDF', 'PDF library failed to load.');
        return null;
    }
    const heading = niceTitle(title);
    const cleanHead = head.map(h => sanitizeForPdf(h));
    const cleanBody = body.map(row => row.map(cell => sanitizeForPdf(cell)));
    const cleanSummary = sanitizeForPdf(summary).trim();

    const doc = new window.jspdf.jsPDF({ orientation: 'landscape', unit: 'mm', format: 'a4' });
    doc.setProperties({ title: heading });
    const everything = [heading, cleanSummary, PDF_COMPANY, ...charts.map(c => c.title), ...cleanHead, ...cleanBody.flat()].join(' ');
    let AR = 'helvetica';
    if (hasArabic(everything)) {
        const b64 = await ensurePdfFont();
        if (b64) {
            doc.addFileToVFS('Amiri-Regular.ttf', b64);
            doc.addFont('Amiri-Regular.ttf', 'Amiri', 'normal');
            doc.addFont('Amiri-Regular.ttf', 'Amiri', 'bold');
            AR = 'Amiri';
        }
    }
    const fontFor = t => (AR !== 'helvetica' && hasArabic(t)) ? AR : 'helvetica';
    const put = (t, x, y, o = {}) => {
        t = String(t ?? '');
        if (hasComplex(t)) {
            const im = textImg(t, doc.getFontSize(), o.bold), w = Math.min(im.w, o.maxW || 1e9), h = im.h * w / im.w;
            doc.addImage(im.url, 'PNG', x + (o.align === 'center' ? -w / 2 : o.align === 'right' ? -w : 0), y - h * 0.72, w, h);
            return;
        }
        const f = fontFor(t);
        doc.setFont(f, o.bold ? 'bold' : (o.italic && f === 'helvetica') ? 'italic' : 'normal');
        doc.text(t, x, y, o.align ? { align: o.align } : undefined);
    };

    const pageW = doc.internal.pageSize.getWidth();
    const num = i => cleanBody.length && cleanBody.every(r => (r[i] || '') === '' || !isNaN((r[i] || '').replace(/,/g, '')));
    const columnStyles = {};
    cleanHead.forEach((h, i) => { if (num(i)) columnStyles[i] = { halign: 'right' }; });

    let startY = 15;
    if (cleanSummary) {
        let y = 19;
        doc.setFontSize(9).setTextColor(0, 139, 139);
        put('Summary', 10, y, { bold: true });
        doc.setTextColor(40);
        y += 4.5;
        const maxTextW = pageW - 26; // Safe buffer to prevent margin clipping
        doc.setFontSize(8);

        cleanSummary.split('\n').forEach(l => {
            const trimmed = l.trim();
            if (!trimmed) {
                y += 1.8;
                return;
            }
            const isHeading = /^[A-Z][A-Za-z0-9\s&,–-]{2,40}:?$/.test(trimmed) && !trimmed.startsWith('•') && !trimmed.startsWith('-');
            let parts = [trimmed];
            if (!hasComplex(trimmed)) {
                doc.setFont(fontFor(trimmed), isHeading ? 'bold' : 'normal');
                parts = doc.splitTextToSize(trimmed, maxTextW);
            }
            parts.forEach(sl => {
                const rtl = hasArabic(sl);
                put(sl, rtl ? pageW - 10 : 10, y, { align: rtl ? 'right' : undefined, bold: isHeading, maxW: maxTextW });
                y += 3.8;
            });
        });
        startY = y + 3.5;
    }

    const TOTAL = '{totalPages}';
    const frame = n => {
        const h = doc.internal.pageSize.getHeight(), cx = pageW / 2, r = pageW - 10;
        doc.setFontSize(9).setTextColor(0);
        put(PDF_COMPANY, 10, 10, { bold: true });
        doc.setFontSize(12);
        put(heading, cx, 10, { bold: true, align: 'center', maxW: pageW - 130 });
        doc.setFontSize(8).setFont('helvetica', 'normal').setTextColor(80);
        doc.text(new Date().toLocaleDateString(), r - 28, 10, { align: 'right' });
        doc.text(`Page ${n}/${TOTAL}`, r, 10, { align: 'right' });

        // Header line: only display if summary text is present above the table on page 1.
        // If only the table shows first, the line is not displayed.
        if (cleanSummary && n === 1) {
            doc.setDrawColor(0, 128, 191).setLineWidth(0.25).line(10, 13, r, 13);
        }

        doc.setFontSize(7).setFont('helvetica', 'italic').setTextColor(150).text(PDF_CREDIT, cx, h - 5, { align: 'center' });
    };

    const pad = 0.8;
    let page = 0;
    doc.autoTable({
        head: [cleanHead],
        body: cleanBody,
        startY,
        columnStyles,
        margin: { top: 15, left: 10, right: 10, bottom: 10 },
        styles: { fontSize: 7, cellPadding: pad, lineWidth: 0.25, lineColor: [0, 128, 191], textColor: 20 },
        headStyles: { fillColor: [223, 255, 255], textColor: 0, fontStyle: 'normal', halign: 'center' },
        didParseCell(d) {
            const raw = d.cell.raw && d.cell.raw.content !== undefined ? d.cell.raw.content : d.cell.raw, t = String(raw ?? '');
            if (hasComplex(t)) {
                const im = textImg(t, d.cell.styles.fontSize, d.section === 'head'), w = Math.min(im.w, 70), h = im.h * w / im.w;
                d.cell._img = { im, w, h };
                d.cell.text = [''];
                d.cell.styles.minCellHeight = h + 2 * pad;
                d.cell.styles.minCellWidth = w + 2 * pad;
                return;
            }
            if (AR !== 'helvetica' && hasArabic(t)) {
                d.cell.styles.font = AR;
                if (!d.cell.styles.halign || d.cell.styles.halign === 'left') d.cell.styles.halign = 'right';
                doc.setFont(AR, 'normal');
                doc.setFontSize(d.cell.styles.fontSize);
                const tw = Math.max(...t.split('\n').map(l => doc.getTextWidth(l.replace(/\s+/g, ' ').trim()))), need = tw * 1.03 + 2 * pad + 0.3;
                if (need <= 60) {
                    d.cell.styles.overflow = 'visible';
                    d.cell.styles.minCellWidth = Math.max(d.cell.styles.minCellWidth || 0, need);
                }
            }
        },
        didDrawCell(d) {
            const g = d.cell._img;
            if (g) doc.addImage(g.im.url, 'PNG', d.cell.x + pad, d.cell.y + (d.cell.height - g.h) / 2, g.w, g.h);
        },
        didDrawPage() {
            page++;
            frame(page);
        }
    });

    const pageH = doc.internal.pageSize.getHeight();
    const bottomLimit = pageH - 14;
    const finalY = (doc.lastAutoTable && doc.lastAutoTable.finalY) ? doc.lastAutoTable.finalY : startY;
    let curY = finalY + 6;

    charts.forEach((c) => {
        let w = 210, h = w * (c.h || 1) / (c.w || 1);
        if (h > 72) { h = 72; w = h * (c.w || 1) / (c.h || 1); }
        if (h < 45) { h = 45; }
        if (w > pageW - 20) { w = pageW - 20; h = w * (c.h || 1) / (c.w || 1); }
        const blockH = h + 10;

        // Auto-manage space: place chart on current page if it fits, else advance to next page
        if (curY + blockH > bottomLimit) {
            doc.addPage();
            frame(doc.getNumberOfPages());
            curY = 18;
        }

        doc.setFontSize(10).setTextColor(0);
        put(c.title, 10, curY, { bold: true, maxW: pageW - 20 });
        const left = (pageW - w) / 2;
        doc.addImage(c.img, 'PNG', left, curY + 3.5, w, h);
        curY += blockH + 4;
    });

    if (doc.putTotalPages) doc.putTotalPages(TOTAL);
    return doc;
}
