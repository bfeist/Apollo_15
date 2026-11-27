"""Deep transcript read for channel identification (stage 10, research pass).

The first pass (``channelid_evidence`` + ``channelid_llm``) showed GLM a short excerpt of each channel
with every line heard on four or more other tracks removed. That hides exactly the content that
identifies a *loop* track (3*FLT DIR LOOP, GOSS loops, MOCR dynamics ...), whose traffic is heard on
many consoles. This pass reads the whole transcript of every delivered channel:

1. ``lines``: every non-noise segment with its *width*, the number of other delivered channels
   carrying the same words at the same time (nothing is removed). Cached in ``deep/lines/``.
2. ``window``: GLM reads each channel in consecutive windows (at most ``WINDOW_WORDS`` words or
   ``WINDOW_S`` seconds) and reports the operator's call signs, the loops audible with their
   approximate share, a free-text identification and candidates on the track sheet.
3. ``final``: GLM reads all window reports of one channel and gives the channel's candidates.

GLM stays blind to delivered channel numbers, filename labels and any ordering model. No map is fitted
here; ``compare`` contrasts the deep answers with the shared-v2 maps and with the first pass, and
fits nothing beyond an unconstrained one-to-one assignment and both monotone directions.

    uv run python scripts/30-track/10_channelid/channelid_deep.py run --tape A15_T510092 --workers 8
    uv run python scripts/30-track/10_channelid/channelid_deep.py run --all-tapes --workers 8
    uv run python scripts/30-track/10_channelid/channelid_deep.py compare A15_T510092

Writes only to ``<root>/deep/`` (``lines/``, ``glm/<tape>/CHnn_wKK.json``, ``glm/<tape>/CHnn_final.json``,
``compare/``). Earlier stage-10 outputs are read, never changed.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import gzip
import hashlib
import json
import math
import sys
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import channelid_evidence as ce  # noqa: E402
import channelid_llm as cl  # noqa: E402
import channelid_sheets as cs  # noqa: E402

DATA = Path('D:/Apollo_15_30-Track')
DEFAULT_ROOT = DATA / 'channel_id'
DEFAULT_TRANSCRIPTS = DATA / 'timing_transcripts' / 'asr-c1198b48a6d3'
LINES_VERSION = 'channelid-deep-lines-v1'
PROMPT_VERSION = 'channelid-deep-v1'
WINDOW_WORDS = 6000
WINDOW_S = 3 * 3600.0
MIN_WORDS = 40              # a channel with fewer transcribed words gets "no own speech" without GLM
MIN_WINDOW_WORDS = 60

SYSTEM = ('You are an expert on Apollo-era Mission Control (MOCR) voice loops, console positions and the '
          'historical voice recorders. You read automatic transcripts of one recorder track and work out which '
          'console position or loop the track recorded. You return only the requested JSON.')

GUIDE = """A 30-track historical recorder in Mission Control recorded one console position or one voice loop per
track. A console-position track carries what that operator heard in the headset (every loop they monitored,
often including air-to-ground and the flight director loop) plus what they said when keyed. A loop track
(for example "3*FLT DIR LOOP" or a GOSS/network loop) carries only that one conference loop: everybody who
talks on that loop, and nothing that is not on it.

You see the COMPLETE automatic transcript of one track for part of the tape. Nothing has been removed.
Each line has a tag [n]: the number of OTHER tracks of this recorder that carry the same words at the same
moment. [0] is heard only on this track (the operator's own side conversations, keyed calls and private
loops are usually [0]-[3]). A high n is a widely monitored loop such as air-to-ground (crew and CAPCOM) or
the flight director loop. Do not discard high-n lines: which shared loops are present, and which are
absent, is evidence. A track whose content is almost all one shared loop is probably that loop's own track.

Clues: who keys and who answers ("FIDO, Flight" is Flight calling FIDO; whoever answers is FIDO); call
signs; how the operator is addressed; subject matter; network sites; recovery forces; command loads;
telemetry; biomedical; science instruments. A technician reading the track sheet aloud ("Channel 14,
CAPCOM left, position 3010 ...") is heard only on the voice-annotation track. Positions not on the sheet
(for example FAO on HR2) may still be heard on a loop track or as bleed: say what you hear, then map it
to the sheet. Transcripts contain recognition errors; call signs are often misheard ("Flight FAS" for
"Flight, FAO", "Inco" as "Inko", "Fido" as "Fight-o", "Eecom" as "E com", "Guido" as "Guide-o")."""

LOOP_ITEM = {'type': 'object', 'properties': {'loop': {'type': 'string'}, 'share': {'type': 'number'}},
             'required': ['loop', 'share'], 'additionalProperties': False}
CAND = {'type': 'object', 'properties': {'channel': {'type': 'integer', 'minimum': 0, 'maximum': 30},
                                         'p': {'type': 'number', 'minimum': 0, 'maximum': 1}},
        'required': ['channel', 'p'], 'additionalProperties': False}


def _schema(name, props, required):
    return {'type': 'json_schema', 'json_schema': {'name': name, 'strict': True, 'schema': {
        'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False}}}


WINDOW_SCHEMA = _schema('track_window', {
    'operator_callsigns': {'type': 'array', 'items': {'type': 'string'}},
    'loops': {'type': 'array', 'items': LOOP_ITEM},
    'position': {'type': 'string'},
    'candidates': {'type': 'array', 'items': CAND, 'minItems': 1, 'maxItems': 6},
    'summary': {'type': 'string'},
    'evidence': {'type': 'array', 'items': {'type': 'string'}}},
    ['operator_callsigns', 'loops', 'position', 'candidates', 'summary', 'evidence'])
FINAL_SCHEMA = _schema('track_identity', {
    'position': {'type': 'string'},
    'candidates': {'type': 'array', 'items': CAND, 'minItems': 1, 'maxItems': 6},
    'summary': {'type': 'string'},
    'callsigns': {'type': 'array', 'items': {'type': 'string'}},
    'evidence': {'type': 'array', 'items': {'type': 'string'}}},
    ['position', 'candidates', 'summary', 'callsigns', 'evidence'])

WINDOW_ASK = """Return JSON:
{"operator_callsigns": ["<call signs this track's own operator uses when keying, or is addressed by and answers to>"],
 "loops": [{"loop": "<loop or conversation audible, e.g. air-to-ground, flight director loop, network/GOSS, recovery>", "share": <fraction of the lines>} ...],
 "position": "<free text: which console position or loop this track recorded>",
 "candidates": [{"channel": <0-30 on the sheet above>, "p": <probability>} ...up to 6, most likely first, p summing to <= 1],
 "summary": "<two or three sentences: what this track is and why>",
 "evidence": ["<up to 6 short quotes with time stamps and [n] tags that decide it>"]}"""

FINAL_ASK = """Above are reports from reading the complete transcript of ONE track, window by window. The track
recorded the same position or loop for the whole tape; windows that disagree usually had little own speech.
Weigh the windows with the most decisive evidence (the operator keying with a call sign, being addressed and
answering, a loop present throughout and nothing else). Return JSON:
{"position": "<free text identification>",
 "candidates": [{"channel": <0-30>, "p": <probability>} ...up to 6, most likely first, p summing to <= 1],
 "summary": "<two or three sentences>",
 "callsigns": ["<call signs of this track's operator>"],
 "evidence": ["<up to 6 quotes with time stamps that decide it>"]}"""


# ---------------------------------------------------------------------------- lines

def build_lines(transcripts: Path, tape: str):
    """{ch: [[t, text, width]]} with width = other channels carrying >= half the segment's content words."""
    sources = ce.tape_sources(transcripts, tape)
    segs, provenance, head = {}, {}, None
    for ch, (folder, meta) in sorted(sources.items()):
        src = meta['source']
        name = (src.get('review') or {}).get('source_filename', '')
        head = head or next((p for p in name.split('_') if p.startswith('HR')), None)
        origin, rate = float(src.get('canonical_origin_s') or 0.0), float(src['sample_rate'])
        provenance[ch] = dict(identity=str(folder), transcript_sha256=ce.identity_state(folder)['sha256'],
                              source_filename=name)
        out = []
        for r in ce.read_rows(folder):
            if r.get('sf') is None or ce.is_noise(r):
                continue
            words = [(origin + w['sf'] / rate, w['w']) for w in (r.get('words') or []) if w.get('sf') is not None]
            out.append(dict(t=origin + r['sf'] / rate, text=r['text'].strip(), words=words))
        freq = collections.Counter(s['text'].lower() for s in out)
        segs[ch] = [s for s in out if freq[s['text'].lower()] <= 25 or len(s['text']) > 40]
    binw = ce.SHARE_WINDOW_S
    index = collections.defaultdict(set)
    for ch, rows in segs.items():
        for s in rows:
            for t, w in s['words']:
                for cw in ce.content_words(w):
                    index[(cw, int(t // binw))].add(ch)
    lines = {}
    for ch, rows in segs.items():
        out = []
        for s in rows:
            hits, n = collections.Counter(), 0
            for t, w in s['words']:
                for cw in ce.content_words(w):
                    n += 1
                    b = int(t // binw)
                    seen = index.get((cw, b - 1), set()) | index.get((cw, b), set()) | index.get((cw, b + 1), set())
                    seen.discard(ch)
                    hits.update(seen)
            need = max(1, math.ceil(0.5 * n))
            width = sum(1 for k in hits.values() if n and k >= need)
            out.append([round(s['t'], 1), s['text'], width])
        lines[str(ch)] = out
    return dict(version=LINES_VERSION, tape=tape, head=head, recorder=head[:3] if head else None,
                provenance={str(k): v for k, v in provenance.items()}, lines=lines)


def load_lines(root: Path, transcripts: Path, tape: str):
    path = root / 'deep' / 'lines' / f'{tape}.json.gz'
    if path.is_file():
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            data = json.load(f)
        current = {str(d): ce.identity_state(folder)['sha256'] for d, (folder, _) in ce.tape_sources(transcripts, tape).items()}
        if data.get('version') == LINES_VERSION and current == {d: p['transcript_sha256'] for d, p in data['provenance'].items()}:
            return data
    data = build_lines(transcripts, tape)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8') as f:
        json.dump(data, f)
    tmp.replace(path)
    return data


def windows(lines):
    out, cur, words, start = [], [], 0, None
    for row in lines:
        n = len(ce.norm_words(row[1]))
        if cur and (words + n > WINDOW_WORDS or row[0] - start > WINDOW_S):
            out.append(cur)
            cur, words = [], 0
        if not cur:
            start = row[0]
        cur.append(row)
        words += n
    if cur:
        out.append(cur)
    return [w for w in out if sum(len(ce.norm_words(r[1])) for r in w) >= MIN_WINDOW_WORDS]


# ---------------------------------------------------------------------------- prompts

def sheet_text(recorder):
    rows = '\n'.join(f'{c:2d}  {s["sheet"]:<26} pos {s["position"] or "-":<6} {s["role"]}'
                     for c, s in sorted(cs.sheet(recorder).items()))
    return (f'Recorder {recorder} track sheet (channel, sheet label, CCS position, what is heard):\n{rows}\n'
            f' 0  (none)                      -      empty track, time code, or bleed only.')


def width_profile(lines):
    words = [(len(ce.norm_words(r[1])), r[2]) for r in lines]
    total = max(1, sum(n for n, _ in words))
    own = sum(n for n, w in words if w == 0)
    local = sum(n for n, w in words if 1 <= w <= 3)
    return (f'Whole tape for this track: {total} transcribed words; {own * 100 // total}% heard only here, '
            f'{local * 100 // total}% on 1-3 other tracks, {(total - own - local) * 100 // total}% on 4 or more.')


def window_prompt(recorder, lines, win, k, n):
    body = '\n'.join(f'[{cl.hms(t)}] [{w}] {text}' for t, text, w in win)
    return (f'{GUIDE}\n\n{sheet_text(recorder)}\n\n{width_profile(lines)}\n\nWindow {k + 1} of {n}, '
            f'tape time {cl.hms(win[0][0])} to {cl.hms(win[-1][0])}:\n{body}\n\n{WINDOW_ASK}')


def final_prompt(recorder, lines, reports):
    parts = []
    for k, (win, rec) in enumerate(reports):
        p = rec['parsed']
        cands = ', '.join(f'{c["channel"]}:{c["p"]:.2f}' for c in p['candidates'])
        loops = ', '.join(f'{x["loop"]} {x["share"]:.2f}' for x in p['loops'])
        parts.append(f'Window {k + 1} ({cl.hms(win[0][0])}-{cl.hms(win[-1][0])}, '
                     f'{sum(len(ce.norm_words(r[1])) for r in win)} words)\n'
                     f'  position: {p["position"]}\n  operator call signs: {", ".join(p["operator_callsigns"])}\n'
                     f'  loops: {loops}\n  candidates: {cands}\n  summary: {p["summary"]}\n'
                     f'  evidence: {" | ".join(p["evidence"])}')
    return (f'{GUIDE}\n\n{sheet_text(recorder)}\n\n{width_profile(lines)}\n\n' + '\n\n'.join(parts) + f'\n\n{FINAL_ASK}')


# ---------------------------------------------------------------------------- GLM

def parse(content, final):
    text = content.strip()
    data = json.loads(text[text.find('{'):text.rfind('}') + 1])
    cands = []
    for c in data.get('candidates') or []:
        ch, p = int(c['channel']), float(c['p'])
        if 0 <= ch <= 30 and p >= 0 and ch not in [x['channel'] for x in cands]:
            cands.append(dict(channel=ch, p=p))
    if not cands:
        raise ValueError('no candidates')
    total = sum(c['p'] for c in cands)
    if total > 1.0:
        for c in cands:
            c['p'] /= total
    out = dict(candidates=cands, position=str(data.get('position', ''))[:300], summary=str(data.get('summary', ''))[:800],
               evidence=[str(x)[:240] for x in data.get('evidence') or []][:8])
    if final:
        out['callsigns'] = [str(x)[:40] for x in data.get('callsigns') or []][:12]
    else:
        out['operator_callsigns'] = [str(x)[:40] for x in data.get('operator_callsigns') or []][:12]
        out['loops'] = [dict(loop=str(x.get('loop', ''))[:80], share=float(x.get('share') or 0))
                        for x in data.get('loops') or []][:8]
    return out


def assess(prompt, path: Path, final, model, retries=3):
    key = hashlib.sha256(f'{PROMPT_VERSION}|{model}|{SYSTEM}|{prompt}'.encode()).hexdigest()[:20]
    if path.is_file():
        rec = json.loads(path.read_text(encoding='utf-8'))
        if rec.get('key') == key and rec.get('parsed'):
            return rec, True
    last = None
    for attempt in range(retries):
        try:
            content, reasoning, meta = cl.stream(prompt, system=SYSTEM, response_format=FINAL_SCHEMA if final else WINDOW_SCHEMA,
                                                 max_tokens=16000)
            if meta['finish_reason'] != 'stop':
                raise cl.BackendError(f'finish_reason {meta["finish_reason"]}')
            rec = dict(key=key, prompt_version=PROMPT_VERSION, model=model, meta=meta, attempt=attempt, prompt=prompt,
                       reasoning=reasoning, content=content, parsed=parse(content, final))
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
            tmp.replace(path)
            return rec, False
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last = exc
            time.sleep(3 * (attempt + 1))
    raise cl.BackendError(f'{path.name}: failed after {retries} attempts: {last}')


def channel_job(root, tape, recorder, ch, lines, model, pool):
    """Window reports (in parallel on ``pool``), then the final answer. Returns the final record."""
    out = root / 'deep' / 'glm' / tape
    words = sum(len(ce.norm_words(r[1])) for r in lines)
    wins = windows(lines)
    if words < MIN_WORDS or not wins:
        rec = dict(key='deterministic', model=None, parsed=dict(
            candidates=[dict(channel=0, p=0.9)], position='no speech', callsigns=[], evidence=[],
            summary=f'{words} transcribed words over the whole tape: empty, time code or bleed only.'))
        out.mkdir(parents=True, exist_ok=True)
        (out / f'CH{ch:02d}_final.json').write_text(json.dumps(rec, indent=1), encoding='utf-8')
        return rec, 0, 0
    futs = [pool.submit(assess, window_prompt(recorder, lines, w, k, len(wins)), out / f'CH{ch:02d}_w{k:02d}.json',
                        False, model) for k, w in enumerate(wins)]
    reports = [(w, f.result()[0]) for w, f in zip(wins, futs)]
    cached = sum(f.result()[1] for f in futs)
    rec, c = assess(final_prompt(recorder, lines, reports), out / f'CH{ch:02d}_final.json', True, model)
    return rec, len(wins), cached + int(c)


def run(args):
    root, transcripts = Path(args.root), Path(args.transcripts)
    tapes = sorted(p.name for p in (transcripts / 'sources' / 'tape').glob('A15_T*') if p.is_dir()) \
        if args.all_tapes else args.tape
    model = cl.probe()
    win_pool = cf.ThreadPoolExecutor(args.workers, thread_name_prefix='win')
    ch_pool = cf.ThreadPoolExecutor(max(2, args.workers), thread_name_prefix='ch')
    lock = threading.Lock()
    t0 = time.monotonic()
    for tape in tapes:
        data = load_lines(root, transcripts, tape)
        rec = data['recorder']
        futs = {int(ch): ch_pool.submit(channel_job, root, tape, rec, int(ch), lines, model, win_pool)
                for ch, lines in data['lines'].items()}
        for ch, f in sorted(futs.items()):
            try:
                r, nw, nc = f.result()
                p = r['parsed']
                top = ', '.join(f'{c["channel"]}:{c["p"]:.2f}' for c in p['candidates'][:3])
                with lock:
                    print(f'{tape} CH{ch:02d} [{nw} win, {nc} cached] {top:<22} {p["position"][:90]}', flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f'{tape} CH{ch:02d} FAILED: {exc}', flush=True)
        print(f'{tape}: done ({time.monotonic() - t0:.0f} s elapsed)', flush=True)


# ---------------------------------------------------------------------------- compare

def probs(cands):
    named = {c['channel']: c['p'] for c in cands}
    rest = max(0.03, 1 - sum(named.values()))
    p = np.array([max(0.004, named.get(t, 0) + rest / max(1, 31 - len(named))) for t in range(31)])
    return p / p.sum()


def compare(args):
    from scipy.optimize import linear_sum_assignment
    root = Path(args.root)
    out = root / 'deep' / 'compare'
    out.mkdir(parents=True, exist_ok=True)
    for tape in args.tapes:
        ev = ce.read(root / 'evidence' / f'{tape}.json.gz')
        rec = ev['recorder']
        sheet = cs.sheet(rec)
        v2 = json.loads((root / 'shared-v2' / 'maps' / f'{tape}.json').read_text(encoding='utf-8'))
        deep, old = {}, {}
        for d in sorted(int(x) for x in ev['channels']):
            f = root / 'deep' / 'glm' / tape / f'CH{d:02d}_final.json'
            if f.is_file():
                deep[d] = json.loads(f.read_text(encoding='utf-8'))['parsed']
            old[d] = json.loads((root / 'glm' / tape / f'CH{d:02d}.json').read_text(encoding='utf-8'))['parsed']
        ds = sorted(deep)
        lp = {d: np.log(probs(deep[d]['candidates'])) for d in ds}
        cost = np.zeros((len(ds), 30 + len(ds)))
        for i, d in enumerate(ds):
            cost[i, :30], cost[i, 30:] = -lp[d][1:31], -lp[d][0]
        r, c = linear_sum_assignment(cost)
        free = {ds[i]: (int(j) + 1 if j < 30 else 0) for i, j in zip(r, c)}

        def rigid(direction):
            best = None
            for o in range(-40, 41):
                s = sum(lp[d][(o - d if direction < 0 else d + o) if 1 <= (o - d if direction < 0 else d + o) <= 30 else 0]
                        for d in ds)
                best = max(best or (s, o), (s, o))
            return best
        ro, rs = rigid(-1), rigid(1)
        lines = [f'# {tape} {v2["head"]}: deep read vs shared-v2', '',
                 f'Rigid fits on deep answers (log-lik): opposite best `true = {ro[1]} - d` {ro[0]:.1f}; '
                 f'same best `true = d {"+" if rs[1] >= 0 else "-"} {abs(rs[1])}` {rs[0]:.1f}', '',
                 '| Del | Label | v2 true | deep top (p) | deep position | free | old GLM top |', '|---:|---|---:|---|---|---:|---:|']
        agree = dict(v2=0, free=0, old=0)
        for d in ds:
            t2 = v2['channels'][str(d)]['true_channel'] if str(d) in v2['channels'] else None
            top = deep[d]['candidates'][0]
            agree['v2'] += (top['channel'] == (t2 or 0))
            agree['old'] += (top['channel'] == old[d]['candidates'][0]['channel'])
            lab = v2['channels'][str(d)]['delivered_label']
            name = sheet[top['channel']]['label'] if top['channel'] else '-'
            lines.append(f'| CH{d:02d} | {lab} | {t2 or "-"} | {top["channel"]} {name} ({top["p"]:.2f}) | '
                         f'{deep[d]["position"][:70]} | {free[d] or "-"} | {old[d]["candidates"][0]["channel"]} |')
        lines += ['', f'Deep top agrees with shared-v2 on {agree["v2"]}/{len(ds)}, with old GLM top on {agree["old"]}/{len(ds)}.']
        (out / f'{tape}.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print('\n'.join(lines))


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--root', default=str(DEFAULT_ROOT))
    p.add_argument('--transcripts', default=str(DEFAULT_TRANSCRIPTS))
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--tape', action='append', default=[])
    r.add_argument('--all-tapes', action='store_true')
    r.add_argument('--workers', type=int, default=8)
    c = sub.add_parser('compare')
    c.add_argument('tapes', nargs='+')
    args = p.parse_args(argv)
    run(args) if args.cmd == 'run' else compare(args)


if __name__ == '__main__':
    main()
