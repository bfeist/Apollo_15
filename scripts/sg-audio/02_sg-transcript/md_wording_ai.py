"""§5 wording review: AI judgement between the printed, AFJ and ALSJ readings of one utterance.

The model (HGX by default, through ``md_ai``'s transport) reads each wording-queue row with its
neighbouring rows and judges which existing reading most likely matches what was said, using
Apollo terminology, hardware and procedure names, call signs, numbers consistent with the
context, and typical OCR and typist errors. It picks one of the given readings or ``listen``;
it never writes wording, and every answer is checked locally against the readings present.

A confident pick (``CONFIDENT``) decides the row (status ``ai``) unless a human choice exists.
Every other row stays in the review queue with the model's pick and reason shown.

Judgements are cached in ``.local/mission_data/ai/wording_<backend>.jsonl`` keyed by wording key
(a hash of the readings), backend, model and prompt hash, so reruns are free. Prompts and
responses are kept under ``.local/mission_data/ai/diagnostics/``.
"""
from __future__ import annotations

import hashlib
import json
import os

from md_ai import DIAGNOSTICS, BackendError, probe, stream
from md_common import AI_CACHE

PROMPT_VERSION = 'wording-v2'
DROP_MIN = 3        # words a pick may leave out only when the neighbouring rows hold them
CONFIDENT = 0.8
CHOICES = ('printed', 'afj', 'alsj', 'listen')
BASES = ('terminology', 'ocr_error', 'context', 'editor_tidying', 'utterance_break', 'audible_only', 'other')
SYSTEM = (
    'You are an expert on the Apollo program, Apollo 15 in particular: its spacecraft systems, switch and '
    'panel names, procedures, call signs, crew, flight controllers and lunar geology. You judge which existing '
    'transcript reading most likely matches the words actually spoken. You never write or merge text. '
    'Return only the requested JSON.')
GUIDE = """Each item is one utterance of the Apollo 15 air-to-ground, onboard or PAO commentary audio, with up to three readings:
- printed: the 1971 NASA transcript, typed by contractors from the audio and read here by OCR. Expect OCR misreads (l/1, O/0, broken or joined words, stray page text) and typist errors on unfamiliar terms. The typists also normalized speech ("Yeah" typed as "Yes").
- afj / alsj: the modern Apollo Flight Journal and Apollo Lunar Surface Journal. Editors retyped the 1971 transcript and corrected it by listening to the audio, so a journal difference is often a real correction. Editors also sometimes tidy grammar, drop false starts and fillers, or move words to the neighbouring utterance.
The goal is a verbatim record of the audio. Choose the reading that most likely matches what was said:
- terminology: one reading names real Apollo hardware, a switch, procedure, place, person or call sign correctly and the other does not.
- ocr_error: one reading is an OCR or typing corruption of the other.
- context: the neighbouring rows (context_before, context_after) or numbers and replies make one reading consistent.
- editor_tidying: the journal smoothed what was said; the printed row keeps the spoken words.
- utterance_break: the difference is words the other source puts in the neighbouring row; pick the reading whose words belong to this utterance.
- audible_only: both readings are plausible and only listening can decide (fillers, "okay" vs "all right", a dropped word). Choose "listen" in this case unless the context decides it.
"differences" lists every differing word run (empty quotes: the words are absent from that reading). Judge those runs only; spelling and punctuation are already ignored.
Confidence is your probability that the chosen reading is the more faithful one. Use "listen" whenever the audio is the only real evidence. Give a reason of at most 20 words naming the deciding difference."""


def prompt_hash() -> str:
    return hashlib.sha256((PROMPT_VERSION + SYSTEM + GUIDE).encode('utf-8')).hexdigest()[:12]


def response_format(size: int) -> dict:
    row = {'type': 'object', 'properties': {
        'i': {'type': 'integer', 'minimum': 0, 'maximum': max(0, size - 1)},
        'choice': {'type': 'string', 'enum': list(CHOICES)},
        'basis': {'type': 'string', 'enum': list(BASES)},
        'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
        'reason': {'type': 'string'},
    }, 'required': ['i', 'choice', 'basis', 'confidence', 'reason'], 'additionalProperties': False}
    return {'type': 'json_schema', 'json_schema': {'name': 'wording_judgements', 'strict': True, 'schema': {
        'type': 'object', 'properties': {'judgements': {'type': 'array', 'items': row, 'minItems': size,
                                                        'maxItems': size}},
        'required': ['judgements'], 'additionalProperties': False}}}


def differences(row: dict) -> list[str]:
    """The differing word runs between the printed row and each journal, for the model to focus on."""
    import difflib
    from md_common import tokens
    out = []
    p = tokens(row['printed'])
    for source in ('afj', 'alsj'):
        if not row[source]:
            continue
        j = tokens(row[source])
        for op, a0, a1, b0, b1 in difflib.SequenceMatcher(None, p, j, autojunk=False).get_opcodes():
            if op != 'equal':
                out.append(f'printed "{" ".join(p[a0:a1])}" / {source} "{" ".join(j[b0:b1])}"')
    return out


def item_of(i: int, row: dict) -> dict:
    out = {'i': i, 'get': row['get'], 'speaker': row['who']}
    for name in ('printed', 'afj', 'alsj'):
        if row[name]:
            out[name] = row[name]
    out['differences'] = differences(row)
    for name in ('context_before', 'context_after', 'moved'):
        if row.get(name):
            out[name] = row[name]
    return out


def build_prompt(rows: list[dict]) -> str:
    items = [item_of(i, r) for i, r in enumerate(rows)]
    return (GUIDE + '\n\nReturn {"judgements": [...]} with exactly one entry per item, in order, using its "i". '
            'Choose only a reading present in that item, or "listen".\n\n'
            + json.dumps({'items': items}, ensure_ascii=False, indent=0))


def parse(content: str, rows: list[dict]) -> tuple[dict[int, dict], list[str]]:
    errors = []
    try:
        start = content.find('{')
        data = json.loads(content[start:]) if start >= 0 else {}
    except json.JSONDecodeError as exc:
        return {}, [f'invalid json: {exc}']
    found = data.get('judgements') if isinstance(data, dict) else None
    if not isinstance(found, list):
        return {}, ['missing judgements array']
    out = {}
    for j in found:
        try:
            i, choice, basis, conf = int(j['i']), j['choice'], j['basis'], float(j['confidence'])
            reason = str(j.get('reason') or '')[:300]
        except (KeyError, TypeError, ValueError):
            errors.append(f'malformed {j!r}')
            continue
        if not 0 <= i < len(rows) or choice not in CHOICES or basis not in BASES or not 0 <= conf <= 1 or i in out:
            errors.append(f'out of range {j!r}')
            continue
        if choice != 'listen' and not rows[i][choice]:
            errors.append(f'{i}: chose {choice}, which has no reading')
            continue
        out[i] = {'ai_choice': choice, 'ai_basis': basis, 'ai_confidence': round(conf, 3), 'ai_reason': reason}
    return out, errors


class Cache:
    def __init__(self, backend: str):
        self.path = AI_CACHE / f'wording_{backend}.jsonl'
        self.rows = {}
        if self.path.is_file():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.rows[row['key']] = row

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(''.join(json.dumps(self.rows[k], ensure_ascii=False, sort_keys=True) + '\n'
                               for k in sorted(self.rows)), encoding='utf-8')
        os.replace(tmp, self.path)


def judge(rows: list[dict], backend: str, budget: int, window: int = 15) -> int:
    """Judge the ``rows`` not yet cached for this backend, model and prompt. Returns requests sent."""
    model = probe(backend)
    phash = prompt_hash()
    cache = Cache(backend)
    pending = [r for r in rows if f'{r["wording_key"]}|{backend}|{model}|{phash}' not in cache.rows]
    DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
    requests = 0

    def send(batch):
        nonlocal requests
        if requests >= budget:
            raise BackendError(f'request budget exhausted ({budget}); judgements so far are cached')
        requests += 1
        prompt = build_prompt(batch)
        content, stats = stream(backend, prompt, response_format(len(batch)), system=SYSTEM)
        stamp = hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:16]
        (DIAGNOSTICS / f'wording_{backend}_{stamp}.json').write_text(json.dumps(
            {'system': SYSTEM, 'prompt': prompt, 'response': content, 'stats': stats}, ensure_ascii=False, indent=1),
            encoding='utf-8')
        if stats['finish_reason'] != 'stop':
            return {}
        found, errors = parse(content, batch)
        if errors:
            print(f'  ai wording: {len(errors)} rejected: {errors[:3]}')
        for i, j in found.items():
            key = f'{batch[i]["wording_key"]}|{backend}|{model}|{phash}'
            cache.rows[key] = {'key': key, 'wording_key': batch[i]['wording_key'], 'backend': backend, 'model': model,
                               'prompt_hash': phash, 'prompt_sha': stamp, **j}
        cache.save()
        return found

    try:
        for start in range(0, len(pending), window):
            batch = pending[start:start + window]
            found = send(batch)
            for i, r in enumerate(batch):
                if i not in found and len(batch) > 1:
                    send([r])  # retry alone; stays unjudged if still invalid
            print(f'  ai wording {backend}: {min(start + window, len(pending))}/{len(pending)} rows, '
                  f'{requests} requests')
    except BackendError as exc:
        print(f'  ai wording: stopped: {exc}')
    finally:
        cache.save()
    return requests


def cached(backend: str = 'hgx') -> dict[str, dict]:
    """Wording key -> the latest cached judgement for ``backend`` with the current prompt."""
    phash = prompt_hash()
    return {r['wording_key']: r for r in Cache(backend).rows.values() if r['prompt_hash'] == phash}


def dropped(row: dict, choice: str) -> list[str]:
    """Runs of DROP_MIN or more words another reading has and ``choice`` lacks, not found in the
    neighbouring rows: words that taking ``choice`` would lose from the record."""
    import difflib
    from md_common import tokens
    from md_reconcile import covered, spelling_form
    chosen = spelling_form(tokens(row[choice]))
    nearby = spelling_form(tokens(' '.join(row.get(k) or '' for k in ('context_before', 'context_after'))))
    out = []
    for other in ('printed', 'afj', 'alsj'):
        if other == choice or not row[other]:
            continue
        words = spelling_form(tokens(row[other]))
        for op, a0, a1, b0, b1 in difflib.SequenceMatcher(None, words, chosen, autojunk=False).get_opcodes():
            run = words[a0:a1]
            if op in ('delete', 'replace') and len(run) - (b1 - b0) >= DROP_MIN and covered(run, nearby) < 0.8:
                out.append(' '.join(run))
    return out


def apply(wording: list[dict], backend: str = 'hgx') -> int:
    """Add cached judgements to the queue rows; a confident pick decides a row awaiting review.

    A pick that would lose DROP_MIN or more words not held by a neighbouring row stays in review.
    """
    found = cached(backend)
    decided = 0
    for row in wording:
        j = found.get(row['wording_key'])
        if not j:
            continue
        row.update({k: j[k] for k in ('ai_choice', 'ai_basis', 'ai_confidence', 'ai_reason')})
        row['ai_backend'] = f'{j["backend"]}:{j["model"]}'
        if row['status'] == 'review' and j['ai_choice'] != 'listen' and j['ai_confidence'] >= CONFIDENT:
            lost = dropped(row, j['ai_choice'])
            if lost:
                row['ai_reason'] += f' [kept for review: would lose "{"; ".join(lost)[:200]}"]'
                continue
            row['status'], row['proposed'] = 'ai', j['ai_choice']
            decided += 1
    return decided
