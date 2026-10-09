// ==============================================================================
// Dream Enterprise AI Hub - PowerPoint Export Module (PptxGenJS)
// ==============================================================================

async function buildPPT({ head, body }, title, summary = '', charts = []) {
    await needLib('pptx');
    const heading = niceTitle(title);
    const p = new PptxGenJS();
    p.layout = 'LAYOUT_WIDE';
    p.author = FILE_COMPANY;
    p.company = FILE_COMPANY;
    p.title = heading;

    p.defineSlideMaster({
        title: 'HUB',
        background: { color: 'FFFFFF' },
        objects: [
            { rect: { x: 0, y: 0, w: '100%', h: 0.14, fill: { color: '0080BF' } } },
            { text: { text: PDF_CREDIT, options: { x: 0.4, y: 7.08, w: 9, h: 0.3, fontSize: 8, italic: true, color: '9CA3AF' } } }
        ],
        slideNumber: { x: 12.4, y: 7.08, fontSize: 9, color: '6B7280' }
    });

    const slide = t => {
        const s = p.addSlide({ masterName: 'HUB' });
        if (t) s.addText(t, { x: 0.4, y: 0.3, w: 12.5, h: 0.6, fontSize: 24, bold: true, color: '1F2937' });
        return s;
    };

    const s0 = p.addSlide({ masterName: 'HUB' });
    s0.addText(FILE_COMPANY, { x: 0.8, y: 2.0, w: 11.7, h: 0.5, fontSize: 16, bold: true, color: '0080BF' });
    s0.addText(heading, { ...pptFace(heading), x: 0.8, y: 2.6, w: 11.7, h: 1.4, fontSize: 38, bold: true, color: '1F2937', valign: 'top' });
    s0.addText(`${body.length} rows  ·  ${stamp()}`, { x: 0.8, y: 4.1, w: 11.7, h: 0.4, fontSize: 14, color: '6B7280' });

    const lines = summaryLines(summary).slice(0, 8);
    if (lines.length) {
        slide('Summary').addText(
            lines.map(l => ({ text: l.replace(/^•\s*/, ''), options: { ...pptFace(l), bullet: /^•/.test(l), breakLine: true } })),
            { x: 0.6, y: 1.2, w: 12.1, h: 5.5, fontSize: 18, color: '1F2937', valign: 'top', paraSpaceAfter: 8, fit: 'shrink' }
        );
    }

    const MAXR = 240, shown = body.slice(0, MAXR);
    const per = head.length > 8 ? 10 : 12, fz = head.length > 9 ? 8 : head.length > 6 ? 10 : 12;
    const nums = numCols({ head, body }), wts = colWeights({ head, body }), tot = wts.reduce((a, b) => a + b, 0), colW = wts.map(w => 12.5 * w / tot);
    const bd = { type: 'solid', pt: 0.5, color: '0080BF' };
    const pages = Math.max(1, Math.ceil(shown.length / per));

    for (let k = 0; k < pages; k++) {
        const s = slide((pages > 1 ? `Data (${k + 1}/${pages})` : 'Data') + (body.length > MAXR ? `  ·  first ${MAXR} of ${body.length} rows` : ''));
        const hdr = head.map(h => ({
            text: String(h),
            options: { ...pptFace(String(h)), bold: true, fill: { color: 'DFFFFF' }, color: '000000', align: 'center', border: bd }
        }));
        const rows = shown.slice(k * per, (k + 1) * per).map((r, j) => head.map((_, i) => ({
            text: String(r[i] ?? ''),
            options: {
                ...pptFace(String(r[i] ?? '')),
                align: (nums[i] || rtlT(String(r[i] ?? ''))) ? 'right' : 'left',
                border: bd,
                fill: { color: j % 2 ? 'F5F9FF' : 'FFFFFF' }
            }
        })));
        s.addTable([hdr, ...rows], { x: 0.4, y: 1.1, w: 12.5, colW, fontSize: fz, color: '1F2937', valign: 'middle' });
    }

    charts.forEach(c => {
        const s = slide(c.title);
        let w = 9, h = w * c.h / c.w;
        if (h > 4.8) { h = 4.8; w = h * c.w / c.h; }
        s.addImage({ data: c.img.replace(/^data:/, ''), x: (13.33 - w) / 2, y: 1.2, w, h });
    });

    return await p.write({ outputType: 'blob' });
}
