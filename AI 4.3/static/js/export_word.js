// ==============================================================================
// Dream Enterprise AI Hub - Word Export Module (docx.js)
// ==============================================================================

async function buildWord({ head, body }, title, summary = '', charts = []) {
    await needLib('docx');
    const {
        Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
        WidthType, ShadingType, AlignmentType, BorderStyle, HeadingLevel,
        ImageRun, Header, Footer, PageNumber, PageOrientation
    } = window.docx;

    const heading = niceTitle(title);
    const landscape = head.length > 6, W = landscape ? 15000 : 10100;
    const fs = head.length > 9 ? 14 : head.length > 6 ? 17 : 20;
    const wts = colWeights({ head, body }), tot = wts.reduce((a, b) => a + b, 0), colW = wts.map(w => Math.floor(W * w / tot));
    const nums = numCols({ head, body });
    const bd = { style: BorderStyle.SINGLE, size: 4, color: '0080BF' };

    const cell = (text, i, o = {}) => new TableCell({
        width: { size: colW[i], type: WidthType.DXA },
        shading: o.fill ? { type: ShadingType.CLEAR, color: 'auto', fill: o.fill } : undefined,
        margins: { top: 50, bottom: 50, left: 80, right: 80 },
        children: [
            new Paragraph({
                alignment: rtlT(String(text ?? '')) ? AlignmentType.LEFT : o.align,
                bidirectional: rtlT(String(text ?? '')),
                children: [new TextRun({ text: String(text ?? ''), bold: !!o.bold, size: fs, ...scriptOpts(String(text ?? '')) })]
            })
        ]
    });

    const rows = [
        new TableRow({
            tableHeader: true,
            cantSplit: true,
            children: head.map((h, i) => cell(h, i, { bold: true, fill: 'DFFFFF', align: AlignmentType.CENTER }))
        }),
        ...body.map((r, k) => new TableRow({
            cantSplit: true,
            children: head.map((_, i) => cell(r[i], i, { fill: k % 2 ? 'F5F9FF' : undefined, align: nums[i] ? AlignmentType.RIGHT : AlignmentType.LEFT }))
        }))
    ];

    const h2 = text => new Paragraph({
        heading: HeadingLevel.HEADING_2,
        spacing: { before: 280, after: 120 },
        children: [new TextRun({ text, bold: true, size: 26, color: '0080BF' })]
    });

    const kids = [
        new Paragraph({
            heading: HeadingLevel.HEADING_1,
            spacing: { after: 60 },
            children: [new TextRun({ text: heading, bold: true, size: 36, color: '1F2937', ...scriptOpts(heading) })]
        }),
        new Paragraph({
            spacing: { after: 200 },
            border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: '0080BF', space: 4 } },
            children: [new TextRun({ text: `Generated ${stamp()}  ·  ${body.length} rows`, size: 18, color: '6B7280' })]
        })
    ];

    const lines = summaryLines(summary);
    if (lines.length) {
        kids.push(h2('Summary'));
        lines.forEach(l => kids.push(new Paragraph({
            spacing: { after: 80 },
            bullet: /^•/.test(l) ? { level: 0 } : undefined,
            children: [new TextRun({ ...scriptOpts(l), text: l.replace(/^•\s*/, ''), size: 21 })]
        })));
    }

    kids.push(
        h2('Data'),
        new Table({
            width: { size: W, type: WidthType.DXA },
            columnWidths: colW,
            rows,
            borders: { top: bd, bottom: bd, left: bd, right: bd, insideHorizontal: bd, insideVertical: bd }
        })
    );

    if (charts.length) {
        kids.push(h2('Charts'));
        const maxW = landscape ? 900 : 620;
        charts.forEach(c => {
            let iw = Math.round(maxW * 0.62), ih = Math.round(iw * c.h / c.w);
            if (ih > 300) { ih = 300; iw = Math.round(ih * c.w / c.h); }
            kids.push(
                new Paragraph({
                    keepNext: true,
                    spacing: { before: 160, after: 60 },
                    children: [new TextRun({ text: c.title, bold: true, size: 22, ...scriptOpts(c.title) })]
                }),
                new Paragraph({
                    alignment: AlignmentType.CENTER,
                    spacing: { after: 160 },
                    children: [new ImageRun({ data: dataUrlBytes(c.img), transformation: { width: iw, height: ih }, type: 'png' })]
                })
            );
        });
    }

    const grey = { size: 14, color: '9CA3AF' };
    const doc = new Document({
        creator: FILE_COMPANY,
        title: heading,
        styles: { default: { document: { run: { font: 'Calibri', size: 20 } } } },
        sections: [{
            properties: {
                page: {
                    size: { orientation: landscape ? PageOrientation.LANDSCAPE : PageOrientation.PORTRAIT },
                    margin: { top: 1100, bottom: 1000, left: 900, right: 900 }
                }
            },
            headers: {
                default: new Header({
                    children: [new Paragraph({
                        border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: '0080BF', space: 2 } },
                        children: [new TextRun({ text: FILE_COMPANY + '   |   ' + heading, size: 16, color: '6B7280' })]
                    })]
                })
            },
            footers: {
                default: new Footer({
                    children: [new Paragraph({
                        alignment: AlignmentType.CENTER,
                        children: [
                            new TextRun({ text: PDF_CREDIT + '   ·   Page ', ...grey }),
                            new TextRun({ children: [PageNumber.CURRENT], ...grey }),
                            new TextRun({ text: ' of ', ...grey }),
                            new TextRun({ children: [PageNumber.TOTAL_PAGES], ...grey })
                        ]
                    })]
                })
            },
            children: kids
        }]
    });

    return await Packer.toBlob(doc);
}
