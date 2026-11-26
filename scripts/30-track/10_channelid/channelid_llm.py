"""GLM (HGX endpoint) assessment of one delivered channel's distinctive speech.

Transport follows ``scripts/sg_transcript/md_ai.py``: OpenAI-compatible streamed
``chat/completions`` with ``HGX_URL``/``HGX_API_KEY``/``HGX_MODEL`` from the repository
``.env``. A response is accepted only when it finishes with ``finish_reason: stop`` and
parses against the schema. Each streamed token is passed to ``on_delta`` so the dashboard
can show the reasoning and answer live.

The model is blind to the delivered channel number and filename label: it sees the
recorder's track sheet (true positions and roles) and excerpts of the channel's speech.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PROMPT_VERSION = 'channelid-v1'

SYSTEM = (
    'You are an expert on Apollo-era Mission Control (MOCR) voice loops and console positions. '
    'You identify which console position or loop a recorded track belongs to from the content of '
    'its speech. You return only the requested JSON.')

GUIDE = """Each track of this 30-track historical recorder recorded one console position or loop.
A track normally carries (a) the console operator's own conversations on the loops they had keyed,
(b) whatever that console was monitoring, and (c) faint bleed from physically adjacent tracks.
Air-to-ground and the flight director loop are heard on many tracks at once; such broadcast lines
have already been removed from the excerpt, so the lines below are what is distinctive about this
track. A line marked {also on N other tracks} was heard on a few other tracks at the same time
(shared loop or adjacent-track bleed) and is weaker evidence.

Clues: who is addressed and who answers ("FIDO, Flight" means Flight is calling FIDO; the speaker
answering "Go" is FIDO), the call signs used by this console when they key up, the subject matter
(trajectory, cryo, recovery ships, network sites, command loads, telemetry formats, solar flares,
biomedical data...), and readouts of the track sheet ("Channel 14, CAPCOM left, position 3010")
which are made only on the voice annotation track. A track whose position is not on this sheet,
a time-code track, or an empty track has no own speech or only faint bleed: use channel 0 for that.

Return JSON:
{"candidates": [{"channel": <0-30>, "p": <probability>} ...up to 6, most likely first, p summing to <= 1],
 "summary": "<one or two sentences: what this track is and why>",
 "callsigns": ["<call signs this track's operator uses or is addressed by>"],
 "evidence": ["<up to 5 short quotes with their time stamps that decide it>"]}"""


class BackendError(RuntimeError):
    pass


def load_env(path: Path = REPO / '.env') -> dict:
    values = {}
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({k: v for k, v in os.environ.items() if k.startswith('HGX_')})
    return values


def settings():
    env = load_env()
    url, key, model = env.get('HGX_URL', ''), env.get('HGX_API_KEY', ''), env.get('HGX_MODEL', 'LLM-Alpha-512K')
    if not url or not key:
        raise BackendError('HGX_URL and HGX_API_KEY must be set in .env')
    return url.rstrip('/'), key, model


def probe():
    url, key, model = settings()
    request = urllib.request.Request(url + '/models', headers={'Authorization': f'Bearer {key}'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            ids = [m.get('id') for m in json.load(response).get('data', [])]
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BackendError(f'HGX endpoint {url} unreachable: {exc}') from exc
    if ids and model not in ids:
        raise BackendError(f'HGX does not list model {model!r}; it offers {ids}')
    return model


def schema():
    cand = {'type': 'object', 'properties': {'channel': {'type': 'integer', 'minimum': 0, 'maximum': 30},
                                             'p': {'type': 'number', 'minimum': 0, 'maximum': 1}},
            'required': ['channel', 'p'], 'additionalProperties': False}
    body = {'type': 'object', 'properties': {
        'candidates': {'type': 'array', 'items': cand, 'minItems': 1, 'maxItems': 6},
        'summary': {'type': 'string'},
        'callsigns': {'type': 'array', 'items': {'type': 'string'}},
        'evidence': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['candidates', 'summary', 'callsigns', 'evidence'], 'additionalProperties': False}
    return {'type': 'json_schema', 'json_schema': {'name': 'track_identity', 'strict': True, 'schema': body}}


def hms(t):
    t = int(t)
    return f'{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}'


def build_prompt(recorder: str, sheet: dict, stats: dict, lines: list) -> str:
    rows = '\n'.join(f'{c:2d}  {s["sheet"]:<26} pos {s["position"] or "-":<6} {s["role"]}'
                     for c, s in sorted(sheet.items()))
    total = max(1, stats['words'])
    head = (f'Recorder {recorder} track sheet (channel, sheet label, CCS position, what is heard):\n{rows}\n'
            f' 0  (none)                      -      no own speech: empty track, time code, or bleed only.\n\n'
            f'This track: {stats["words"]} transcribed words over the tape; {stats["own_words"] * 100 // total}% '
            f'heard only on this track, {stats["local_shared_words"] * 100 // total}% on 1-3 other tracks, '
            f'{stats["broadcast_words"] * 100 // total}% broadcast background (removed). Transcripts are automatic '
            f'and contain recognition errors.\n\nExcerpt (tape time, text):\n')
    body = []
    for t, text, shared in lines:
        tag = f'  {{also on {len(shared)} other tracks}}' if shared else ''
        body.append(f'[{hms(t)}] {text}{tag}')
    if not body:
        body.append('(no distinctive speech at all)')
    return GUIDE + '\n\n' + head + '\n'.join(body)


def prompt_key(prompt: str, model: str) -> str:
    return hashlib.sha256(f'{PROMPT_VERSION}|{model}|{SYSTEM}|{prompt}'.encode()).hexdigest()[:20]


def stream(prompt: str, *, on_delta=None, timeout=900, max_tokens=12000):
    """Return (content, reasoning, meta). ``on_delta(kind, text)`` gets 'reasoning'/'content' tokens."""
    url, key, model = settings()
    payload = {'model': model, 'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt}],
               'max_tokens': max_tokens, 'temperature': 0.1, 'stream': True, 'reasoning_effort': 'low',
               'response_format': schema(), 'stream_options': {'include_usage': True}}
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream', 'Authorization': f'Bearer {key}'}
    request = urllib.request.Request(url + '/chat/completions', data=json.dumps(payload).encode('utf-8'),
                                     headers=headers)
    content, reasoning, finish, usage = [], [], None, {}
    t0 = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for line in response:
            line = line.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get('error'):
                raise BackendError(f'stream error: {chunk["error"]}')
            usage = chunk.get('usage') or usage
            for choice in chunk.get('choices') or []:
                finish = choice.get('finish_reason') or finish
                delta = choice.get('delta') or {}
                r = delta.get('reasoning') or delta.get('reasoning_content') or ''
                c = delta.get('content') or ''
                if r:
                    reasoning.append(r)
                    on_delta and on_delta('reasoning', r)
                if c:
                    content.append(c)
                    on_delta and on_delta('content', c)
    meta = dict(finish_reason=finish, elapsed_s=round(time.monotonic() - t0, 2), model=model, usage=usage)
    return ''.join(content), ''.join(reasoning), meta


def parse(content: str) -> dict:
    text = content.strip()
    if text.startswith('```'):
        text = text.strip('`').split('\n', 1)[-1]
    start, end = text.find('{'), text.rfind('}')
    data = json.loads(text[start:end + 1])
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
    return dict(candidates=cands, summary=str(data.get('summary', ''))[:600],
                callsigns=[str(x)[:40] for x in (data.get('callsigns') or [])][:12],
                evidence=[str(x)[:240] for x in (data.get('evidence') or [])][:6])


def assess(prompt: str, cache_path: Path, *, on_delta=None, retries=3, model=None):
    """Cached assessment. The cache file holds prompt, reasoning, answer and parse."""
    model = model or settings()[2]
    key = prompt_key(prompt, model)
    if cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
        if cached.get('key') == key and cached.get('parsed'):
            return cached, True
    last = None
    for attempt in range(retries):
        try:
            content, reasoning, meta = stream(prompt, on_delta=on_delta)
            if meta['finish_reason'] != 'stop':
                raise BackendError(f'finish_reason {meta["finish_reason"]}')
            parsed = parse(content)
            record = dict(key=key, prompt_version=PROMPT_VERSION, model=model, meta=meta, attempt=attempt,
                          prompt=prompt, reasoning=reasoning, content=content, parsed=parsed)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding='utf-8')
            tmp.replace(cache_path)
            return record, False
        except (BackendError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError, OSError) as exc:
            last = exc
            on_delta and on_delta('error', f'\n[attempt {attempt + 1} failed: {exc}]\n')
            time.sleep(3 * (attempt + 1))
    raise BackendError(f'assessment failed after {retries} attempts: {last}')
