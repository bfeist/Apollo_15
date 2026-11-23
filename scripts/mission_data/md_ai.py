"""§4 step 4: AI span classification through an OpenAI-compatible endpoint.

Backends are chosen explicitly (``--backend hgx`` or ``--backend strata``);
there is no automatic switching. The transport follows Artemis
``transcript_common.stream_llm_assessment``: streamed ``chat/completions``,
``reasoning_effort: low``, temperature 0.1, and a response is valid only if it
finishes with ``finish_reason: stop``. The model labels spans; it never writes
wording, and every label is checked locally.

Labels are cached in ``.local/mission_data/ai/labels_<backend>.jsonl`` keyed by span key,
backend, model and prompt hash, so reruns are free and reproducible. Full
prompts and responses are kept under ``.local/mission_data/ai/diagnostics/``.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from md_common import AI_CACHE, load_env
from md_editorial import ACTIONS, CLASSES

DIAGNOSTICS = AI_CACHE / 'diagnostics'
PROMPT_VERSION = 'spans-v1'
SYSTEM = (
    'You label editorial insertions in Apollo 15 journal transcripts so that only the words actually spoken are '
    'published. You never rewrite text. Return only the requested JSON.')
CLASS_GUIDE = """Classes and their usual action:
- pause: pause / long pause / no answer / static -> annotate
- garble: unintelligible speech -> garble
- nonspeech: audible laughter, chuckle, cough, humming, singing, music, applause, whistling -> keep; other sounds -> annotate
- unit: unit conversion inserted by the editor, e.g. [58,282 km], (feet per second) -> remove
- gloss: expansion or explanation of a term, e.g. [Antenna], (LM), (Valve) -> remove
- inserted: word(s) added for grammar, e.g. [is], (to), occasional[ly] -> remove
- direction: stage direction or addressee, e.g. (To Jim), (Reading), [correcting himself] -> annotate
- note: editorial note longer than a phrase -> to_commentary
- literal: notation that was spoken, e.g. CB(16) -> keep
- restoration: words in brackets that were actually spoken (often present in the 1971 transcript) -> keep
- other: none of the above -> choose the best action
Actions: remove, garble, annotate, to_commentary, keep."""


class BackendError(RuntimeError):
    pass


def settings(backend: str) -> tuple[str, str, str]:
    env = load_env()
    if backend == 'hgx':
        url, key, model = env.get('HGX_URL', ''), env.get('HGX_API_KEY', ''), env.get('HGX_MODEL', 'LLM-Alpha-512K')
        if not url or not key:
            raise BackendError('HGX_URL and HGX_API_KEY must be set in .env (see .env.example)')
    elif backend == 'strata':
        url, key, model = env.get('STRATA_URL', ''), env.get('STRATA_API_KEY', ''), env.get('STRATA_MODEL', '')
        if not url or not model:
            raise BackendError('STRATA_URL and STRATA_MODEL must be set in .env (see .env.example)')
    else:
        raise BackendError(f'unknown backend {backend}')
    return url.rstrip('/'), key, model


def probe(backend: str) -> str:
    """Stop with a clear message when the selected backend's /models does not answer."""
    url, key, model = settings(backend)
    request = urllib.request.Request(url + '/models', headers={'Authorization': f'Bearer {key}'} if key else {})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            ids = [m.get('id') for m in json.load(response).get('data', [])]
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BackendError(f'{backend} backend at {url} is unreachable ({exc}). Checkpoints are kept; rerun with '
                           f'the other --backend if needed.') from exc
    if ids and model not in ids:
        raise BackendError(f'{backend} does not list model {model!r}; it offers {ids}')
    return model


def response_format(size: int) -> dict:
    row = {'type': 'object', 'properties': {
        'i': {'type': 'integer', 'minimum': 0, 'maximum': max(0, size - 1)},
        'cls': {'type': 'string', 'enum': list(CLASSES)},
        'action': {'type': 'string', 'enum': list(ACTIONS)},
        'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
    }, 'required': ['i', 'cls', 'action', 'confidence'], 'additionalProperties': False}
    return {'type': 'json_schema', 'json_schema': {'name': 'span_labels', 'strict': True, 'schema': {
        'type': 'object', 'properties': {'labels': {'type': 'array', 'items': row, 'minItems': size, 'maxItems': size}},
        'required': ['labels'], 'additionalProperties': False}}}


def build_prompt(targets: list[dict], context: list[dict]) -> str:
    """``targets``: [{'i', 'utterance', 'span', 'printed'}], ``context``: neighbouring utterances only."""
    payload = {'context_only': context, 'spans': targets}
    return (CLASS_GUIDE + '\n\nFor every item in "spans", label the bracketed span `span` inside `utterance` '
            '(its position is marked ⟦like this⟧). `printed` is the 1971 NASA transcript row for the same moment, '
            'when known; words present there were probably spoken. Return {"labels": [...]} with exactly one entry '
            'per span, in order, using its "i".\n\n' + json.dumps(payload, ensure_ascii=False, indent=0))


def prompt_hash(prompt_template: str) -> str:
    return hashlib.sha256((PROMPT_VERSION + SYSTEM + CLASS_GUIDE).encode('utf-8')).hexdigest()[:12]


def stream(backend: str, prompt: str, schema: dict | None, *, json_object=False, timeout=600) -> tuple[str, dict]:
    url, key, model = settings(backend)
    payload = {'model': model, 'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt}],
               'max_tokens': 16384, 'temperature': 0.1, 'stream': True, 'reasoning_effort': 'low'}
    if json_object:
        payload['response_format'] = {'type': 'json_object'}
    elif schema is not None:
        payload['response_format'] = schema
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    request = urllib.request.Request(url + '/chat/completions', data=json.dumps(payload).encode('utf-8'),
                                     headers=headers)
    content, finish, raw = [], None, []
    t0 = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for line in response:
            line = line.decode('utf-8', 'replace').strip()
            raw.append(line)
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
            choices = chunk.get('choices') or []
            if choices:
                finish = choices[0].get('finish_reason') or finish
                content.append((choices[0].get('delta') or {}).get('content') or '')
    return ''.join(content), {'finish_reason': finish, 'elapsed_s': round(time.monotonic() - t0, 2), 'model': model,
                              'backend': backend, 'raw_lines': len(raw)}


def parse_labels(content: str, size: int) -> tuple[dict[int, dict], list[str]]:
    errors = []
    try:
        start = content.find('{')
        data = json.loads(content[start:]) if start >= 0 else {}
    except json.JSONDecodeError as exc:
        return {}, [f'invalid json: {exc}']
    labels = data.get('labels') if isinstance(data, dict) else None
    if not isinstance(labels, list):
        return {}, ['missing labels array']
    if len(labels) != size:
        errors.append(f'expected {size} labels, got {len(labels)}')
    out = {}
    for label in labels:
        try:
            i, cls, action, conf = int(label['i']), label['cls'], label['action'], float(label['confidence'])
        except (KeyError, TypeError, ValueError):
            errors.append(f'malformed label {label!r}')
            continue
        if not 0 <= i < size or cls not in CLASSES or action not in ACTIONS or not 0 <= conf <= 1:
            errors.append(f'out-of-range label {label!r}')
            continue
        if action == 'garble' and cls not in ('garble', 'other'):
            errors.append(f'garble action for class {cls} at {i}')
            continue
        if i in out:
            errors.append(f'duplicate index {i}')
            continue
        out[i] = {'class': cls, 'action': action, 'confidence': round(conf, 3)}
    return out, errors


class LabelCache:
    def __init__(self, backend: str):
        self.path = AI_CACHE / f'labels_{backend}.jsonl'
        self.rows: dict[str, dict] = {}
        if self.path.is_file():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.rows[row['key']] = row

    @staticmethod
    def key(span_key: str, backend: str, model: str, phash: str) -> str:
        return f'{span_key}|{backend}|{model}|{phash}'

    def get(self, span_key, backend, model, phash):
        return self.rows.get(self.key(span_key, backend, model, phash))

    def put(self, row: dict):
        self.rows[row['key']] = row

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(''.join(json.dumps(self.rows[k], ensure_ascii=False, sort_keys=True) + '\n'
                               for k in sorted(self.rows)), encoding='utf-8')
        os.replace(tmp, self.path)


def label_spans(items: list[dict], backend: str, budget: int, window: int = 40) -> dict[str, dict]:
    """Label ``items`` (each: span_key, utterance, span, printed, context). Returns span_key -> label.

    Rows already cached for this backend/model/prompt are reused. Uncached rows are sent in windows; invalid
    rows are retried alone; anything still unresolved stays explicit (absent from the result).
    """
    model = probe(backend)
    phash = prompt_hash(SYSTEM)
    cache = LabelCache(backend)
    result, pending = {}, []
    for item in items:
        hit = cache.get(item['span_key'], backend, model, phash)
        if hit:
            result[item['span_key']] = hit
        else:
            pending.append(item)
    DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
    requests = 0
    json_object = False

    def send(batch):
        nonlocal requests, json_object
        if requests >= budget:
            raise BackendError(f'request budget exhausted ({budget}); checkpoints retained')
        requests += 1
        targets = [{'i': i, 'utterance': b['utterance'], 'span': b['span'], 'printed': b.get('printed', '')}
                   for i, b in enumerate(batch)]
        context = []
        for b in batch:
            context.extend(c for c in b.get('context', []) if c not in context)
        prompt = build_prompt(targets, context[:2 * len(batch)])
        schema = response_format(len(batch))
        if json_object:
            prompt += '\n\nJSON schema:\n' + json.dumps(schema['json_schema']['schema'])
        try:
            content, stats = stream(backend, prompt, schema, json_object=json_object)
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and not json_object and backend == 'strata':
                json_object = True  # Strata without strict json_schema: schema goes in the prompt
                requests -= 1
                return send(batch)
            raise
        stamp = hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:16]
        (DIAGNOSTICS / f'{backend}_{stamp}.json').write_text(json.dumps(
            {'system': SYSTEM, 'prompt': prompt, 'response': content, 'stats': stats}, ensure_ascii=False, indent=1),
            encoding='utf-8')
        if stats['finish_reason'] != 'stop':
            return {}, [f'finish_reason={stats["finish_reason"]}']
        labels, errors = parse_labels(content, len(batch))
        for i, label in labels.items():
            b = batch[i]
            row = {'key': LabelCache.key(b['span_key'], backend, model, phash), 'span_key': b['span_key'],
                   'backend': backend, 'model': model, 'prompt_hash': phash, 'prompt_sha': stamp,
                   'response_format': 'json_object' if json_object else 'json_schema', **label}
            cache.put(row)
            result[b['span_key']] = row
        cache.save()
        return labels, errors

    try:
        for start in range(0, len(pending), window):
            batch = pending[start:start + window]
            labels, errors = send(batch)
            for i, b in enumerate(batch):
                if i not in labels:
                    send([b])  # retry alone; stays unresolved if still invalid
            print(f'  ai {backend}: {min(start + window, len(pending))}/{len(pending)} spans, {requests} requests')
    finally:
        cache.save()
    return result


def cached_labels(backend: str) -> dict[str, dict]:
    """Every cached label of ``backend`` by span key (latest model/prompt wins by sort order)."""
    out = {}
    for row in LabelCache(backend).rows.values():
        out[row['span_key']] = row
    return out
