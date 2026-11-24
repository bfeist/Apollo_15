"""Rows of the JSC "NASA Historical Audio Archive - Digital Audio File Metadata" PDF.

The PDF is a 79-page table. Its text layer keeps each row on one baseline, but
plain text extraction misplaces the file-name column, so cells are assigned
from word coordinates against each page's column headings. A line without a
file name, date or mission continues the description of the row above.
"""
from __future__ import annotations

from pathlib import Path

from inventory_common import JSC_PDF, mission_of_code, tape_key

HEAD = ('FILENAME', 'YR', 'MO', 'DA', 'MISSION', 'DESCRIPTION',
        'GMT-ST', 'GMT-END', 'MET-ST', 'MET-END', 'CT-ST', 'CT-END')
TIMES = HEAD[6:]
FIELDS = ('key', 'jsc_file', 'jsc_date', 'jsc_mission_code', 'mission', 'jsc_description',
          'jsc_gmt_start', 'jsc_gmt_end', 'jsc_met_start', 'jsc_met_end', 'jsc_ct_start', 'jsc_ct_end', 'jsc_page')


def _blank(value: str) -> str:
    """The PDF writes an unknown time as zeros."""
    return '' if value in ('000:00:00', '00:00', '') else value


def parse(path: Path = JSC_PDF) -> list[dict]:
    import pymupdf
    rows = []
    with pymupdf.open(path) as doc:
        for number, page in enumerate(doc, 1):
            words = page.get_text('words')
            x = {}
            for w in words:
                if w[4] in HEAD and w[4] not in x:
                    x[w[4]] = w[0]
            if len(x) < len(HEAD):
                continue  # closing pages without the table
            top = min(w[1] for w in words if w[4] == 'FILENAME') + 5
            lines = []
            for w in sorted((w for w in words if w[1] > top), key=lambda w: (w[1] + w[3]) / 2):
                y = (w[1] + w[3]) / 2
                if lines and abs(lines[-1][0] - y) < 4:
                    lines[-1][1].append(w)
                else:
                    lines.append([y, [w]])
            for _y, line in lines:
                cells = {k: [] for k in HEAD}
                for w in sorted(line, key=lambda w: w[0]):
                    left, text = w[0], w[4]
                    # Values sit a little left of their (centred) headings.
                    if left < x['YR'] - 7:
                        cells['FILENAME'].append(text)
                    elif left < x['MO'] - 5:
                        cells['YR'].append(text)
                    elif left < x['DA'] - 7:
                        cells['MO'].append(text)
                    elif left < x['MISSION'] - 10:
                        cells['DA'].append(text)
                    elif left < x['GMT-ST'] - 8:
                        first = not cells['DESCRIPTION'] and left < x['MISSION'] + 25
                        cells['MISSION' if first else 'DESCRIPTION'].append(text)
                    else:
                        cells[min(TIMES, key=lambda k: abs(x[k] - left))].append(text)
                cells = {k: ' '.join(v) for k, v in cells.items()}
                if cells['FILENAME'] or cells['YR'] or cells['MISSION'] or not rows:
                    cells['page'] = number
                    rows.append(cells)
                else:
                    for k in HEAD:
                        if cells[k]:
                            rows[-1][k] = (rows[-1][k] + ' ' + cells[k]).strip()
    out = []
    for r in rows:
        yr, mo, da = r['YR'], r['MO'], r['DA']
        date = f'19{yr}-{mo}-{da}' if yr.isdigit() and mo.isdigit() and da.isdigit() and yr != '00' else ''
        out.append({'key': tape_key(r['FILENAME']), 'jsc_file': r['FILENAME'], 'jsc_date': date,
                    'jsc_mission_code': r['MISSION'], 'mission': mission_of_code(r['MISSION']),
                    'jsc_description': r['DESCRIPTION'],
                    'jsc_gmt_start': _blank(r['GMT-ST']), 'jsc_gmt_end': _blank(r['GMT-END']),
                    'jsc_met_start': _blank(r['MET-ST']), 'jsc_met_end': _blank(r['MET-END']),
                    'jsc_ct_start': _blank(r['CT-ST']), 'jsc_ct_end': _blank(r['CT-END']), 'jsc_page': r['page']})
    return out
