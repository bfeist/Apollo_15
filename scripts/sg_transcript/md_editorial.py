"""§4: editorial spans, pattern rules, published assembly and the release check.

The journal extractor calls :func:`split_spans` to turn utterance text into
ordered ``spoken`` and ``editorial`` spans. Square brackets and parentheses are
editorial in both journals; the AFJ uses ``[...]`` and the ALSJ ``(...)`` for
insertions, and both occasionally use the other. Spoken literal notation such
as ``CB(16)`` is recognized by :func:`rule_judgement` before any removal rule.
"""
from __future__ import annotations

import re

CLASSES = ('pause', 'garble', 'nonspeech', 'unit', 'gloss', 'inserted', 'direction', 'note', 'literal',
           'restoration', 'other')
ACTIONS = ('remove', 'garble', 'annotate', 'to_commentary', 'keep')
GARBLE = '...'

APPROVED_MARKERS = ('(Laughter)', '(Humming)', '(Chuckle)', '(Singing)', '(Cough)', '(Music)', '(Applause)',
                    '(Whistling)')
# Spoken literal notation: a designator immediately followed by a parenthesized number, e.g. CB(16).
LITERAL = re.compile(r'[A-Z][A-Z0-9/]{0,5}\(\d{1,3}[A-Z]?\)')
UNIT_PATTERN = re.compile(r'\d[\d,.]* ?(?:km|m/s|kg|kPa|°C|°F)\b|\d ?°[CF]\b')

_OPEN = {'[': ']', '(': ')'}


def split_spans(text: str, source: str) -> list[dict]:
    """Ordered spans. Editorial spans carry their inner text and exact source characters."""
    spans: list[dict] = []
    buffer = []
    i, n = 0, len(text)

    def spoken():
        if buffer:
            chunk = ''.join(buffer)
            if chunk:
                spans.append({'kind': 'spoken', 'bracket': '', 'text': chunk, 'source_chars': chunk})
            buffer.clear()

    while i < n:
        ch = text[i]
        if ch in _OPEN:
            depth, j, stack = 0, i, []
            while j < n:
                c = text[j]
                if c in _OPEN:
                    stack.append(_OPEN[c])
                elif stack and c == stack[-1]:
                    stack.pop()
                    if not stack:
                        break
                j += 1
            if j >= n:  # unbalanced: keep as one editorial span to the end, flagged by its bracket
                spoken()
                spans.append({'kind': 'editorial', 'bracket': ch + '?', 'text': text[i + 1:].strip(),
                              'source_chars': text[i:]})
                return spans
            spoken()
            spans.append({'kind': 'editorial', 'bracket': ch, 'text': text[i + 1:j].strip(),
                          'source_chars': text[i:j + 1]})
            i = j + 1
            continue
        if ch in ']' or ch == ')':
            buffer.append(ch)  # stray closer stays visible to the release check
            i += 1
            continue
        buffer.append(ch)
        i += 1
    spoken()
    return spans


# ------------------------------------------------------------------- rules
FUNCTION_WORDS = set("""a an the is are was were be been am it it's its i i'm i'd i'll i've you you're we we're we'll
we've he he's she they they're them us me my our your his her to of in on at for from by with and or but so that
this these those there here has have had do does did will would can could should shall may might must not no as
if then than up down out over into onto off about just also all any some one that's what's there's let's""".split())
PAUSE_PART = re.compile(r'^(?:(?:(?:very |quite a )?long|short|brief|a) )?pauses?$|^no (?:answer|reply|response)$|'
                        r'^(?:(?:burst of|brief) )?static(?: (?:clears|fades))?$|^silence$|^(?:long )?comm(?:unications?)? break$|'
                        r'^los$|^loss of signal$|^(?:long |short )?gap$|^no comm$|^noise$|^hiss$|^squeal$|'
                        r'^tones?$|^beeps?$|^quindar(?: tones?)?$|^(?:long )?(?:silence|gap) in (?:the )?'
                        r'(?:audio|tape|recording)$', re.I)
GARBLE_PART = re.compile(r'^(?:garble[ds]?|garbled?|unintelligible|unreadable|inaudible|indecipherable|'
                         r'words? (?:lost|garbled)|lost|lost in static|(?:garbled?|lost|words? lost) (?:under|in|by|'
                         r'due to|behind) .*|garble[ds]?\b.*|unclear|several words|\?+)$', re.I)
NONSPEECH = (
    (re.compile(r'^(?:[A-Z]\w+ )?(?:laugh(?:ter|s|ing|ed)?|laugher)\b.*$', re.I), '(Laughter)'),
    (re.compile(r'^(?:[A-Z]\w+ )?chuckl(?:e|es|ing|ed)\b.*$', re.I), '(Chuckle)'),
    (re.compile(r'^(?:[A-Z]\w+ )?cough(?:s|ing|ed)?\b.*$', re.I), '(Cough)'),
    (re.compile(r'^(?:[A-Z]\w+ )?humm(?:s|ing)\b.*$', re.I), '(Humming)'),
    (re.compile(r'^(?:[A-Z]\w+ )?(?:sings|singing)\b.*$', re.I), '(Singing)'),
    (re.compile(r'^(?:[A-Z]\w+ )?whistl(?:e|es|ing)\b.*$', re.I), '(Whistling)'),
    (re.compile(r'^music\b.*$', re.I), '(Music)'),
    (re.compile(r'^(?:[A-Z]\w+ )?applau(?:se|ding|ds)\b.*$', re.I), '(Applause)'),
)
PLAIN_MARKERS = {'laughter', 'laughs', 'laughing', 'laugh', 'laugher', 'chuckle', 'chuckling', 'chuckles', 'cough',
                 'coughing', 'coughs', 'humming', 'singing', 'sings', 'whistling', 'music', 'applause'}
SOUNDS = re.compile(r'^(?:[A-Z]\w+ )?(?:camera click(?:s|ing)?|(?:onboard )?clank(?:s|ing)?|'
                    r'yawn(?:s|ing)?|sneez(?:e|es|ing)|sigh(?:s|ing)?|clear(?:s|ed|ing)? (?:his )?throat|'
                    r'cleared throat|yell(?:s|ing)?|grunt(?:s|ing)?|whoop(?:s|ing)?|groan(?:s|ing)?|'
                    r'(?:heavy )?breathing|pant(?:s|ing)|strain(?:s|ing)|exhal(?:es|ing)|clap(?:s|ping)?|'
                    r'click(?:s|ing)?|thump(?:s|ing)?|bang(?:s|ing)?|cheer(?:s|ing)?|hiccups?)\b[\w ,]*$', re.I)
UNIT_WORDS = re.compile(r'\b(?:km|kilomet(?:re|er)s?|met(?:re|er)s?|m/s|kph|km/h|kPa|kg|kilograms?|grams?|cm|mm|'
                        r'centimet(?:re|er)s?|millimet(?:re|er)s?|celsius|fahrenheit|litres?|liters?|'
                        r'newtons?|kN|tonnes?|feet per second|nautical miles?|statute miles?|miles?|knots?|psi|'
                        r'pounds?|lb|lbs|inch(?:es)?|feet|ft|fps|mph)\b|\d-?mm\b|°', re.I)
UNIT_ONLY = re.compile(r'^(?:(?:feet|metres|meters|kilometres|kilometers|miles|nautical miles) per (?:second|hour)|'
                       r'(?:nautical|statute)(?: miles?)?|degrees?(?: (?:celsius|fahrenheit))?|psi|volts?|amps?|'
                       r'per ?cent(?: remaining)?|percent(?: remaining)?|fps|minutes?|seconds?|hours?|feet|'
                       r'met(?:re|er)s?|km|kilomet(?:re|er)s?|knots?|pounds?|degrees? per second|'
                       r'(?:distance|bearing|range|heading|altitude)(?: [\w ]+)?)$', re.I)
NUMBER = re.compile(r'\d')
DIRECTION = re.compile(r'^(?:[Tt]o [A-Z]|[Tt]o (?:himself|houston|the|both|them|everyone)|responding|reading|reads|'
                       r'before |after |while |correcting|talking|aside|quietly|sotto|thinking|joking|obviously|'
                       r'as |with |still |now |off-?mike|on (?:the )?(?:intercom|vox|loop)|mumbling|muttering|'
                       r'counting|repeating|interrupting|overlapping|simultaneous|not hearing|hearing|ignoring|'
                       r'apparently|possibly|probably|sounds like|transmitted|tape\b|onboard|in (?:falcon|endeavour)|'
                       r'amused|surprised|pleased|frustrated|exasperated|teasing|mimicking|imitating|whispering|'
                       r'quoting|speaking|cuts? (?:in|out)|in (?:the )?background|background|faint|weak|stops|'
                       r'answering|subvocal|pointing|listening|singsong|emphatic|emphasizing|excited|calling|'
                       r'returning|getting|moving|looking|turning|standing|walking|driving|working|handing|'
                       r'giving|taking|over (?:the )?(?:near|far)|lmp\b|cdr\b|cmp\b)', re.I)
GLOSSARY = {s.casefold() for s in """antenna circuit breaker circuit breakers valve nautical lm csm cm sps lrv plss
opss alsep mesa hga lga dac tv mcc fido guido capcom pao eva seva lmp cdr cmp sim rcs dsky imu pgns ags
electro-luminescent suit isolation switch breaker panel degrees percent apollo rover crater camera spectrometer
magazine control circuit bay boom battery batteries cooling remaining pressure temperature temperatures burn
photography""".split()}
ACRONYM = re.compile(r'^[A-Z][A-Z0-9/&-]{1,9}(?:\s[A-Z][A-Z0-9/&-]{1,9}){0,2}s?$')
WORDS = re.compile(r"^[A-Za-z’'-]+(?: [A-Za-z’'-]+)*$")
# Where rules with different classes fire together, these pairs are resolved by PRECEDENCE.
# Any other combination is ambiguous and labelled 'none'.
PRECEDENCE = ('literal', 'garble', 'nonspeech', 'pause', 'note', 'direction', 'unit', 'gloss', 'inserted', 'other')
RESOLVED = {frozenset(p) for p in (('garble', 'pause'), ('garble', 'direction'), ('nonspeech', 'direction'),
                                   ('nonspeech', 'pause'), ('nonspeech', 'garble'), ('note', 'direction'),
                                   ('note', 'pause'), ('note', 'unit'), ('note', 'gloss'), ('pause', 'direction'),
                                   ('unit', 'gloss'), ('unit', 'inserted'), ('gloss', 'inserted'),
                                   ('direction', 'unit'), ('direction', 'gloss'), ('direction', 'inserted'),
                                   ('garble', 'note'), ('pause', 'gloss'))}


def marker_for(text: str) -> str | None:
    t = (text or '').strip(' .!')
    for pattern, marker in NONSPEECH:
        if pattern.match(t):
            return marker
    return None


def _neighbours(spans, index):
    before = spans[index - 1]['source_chars'] if index > 0 else ''
    after = spans[index + 1]['source_chars'] if index + 1 < len(spans) else ''
    return before, after


def rule_matches(spans: list[dict], index: int) -> list[tuple[str, str, str]]:
    """Every (class, action, rule) whose pattern matches editorial span ``index``."""
    span = spans[index]
    bare = span['text'].strip().strip(' .!;,')
    low = bare.casefold()
    before, after = _neighbours(spans, index)
    attached_before = bool(re.search(r'\w$', before))
    attached_after = bool(re.match(r'^\w', after))
    words = len(bare.split())
    out: list[tuple[str, str, str]] = []
    if span['bracket'].endswith('?'):
        return out
    designator = re.search(r'[A-Z][A-Z0-9/]{0,5}$', before)
    if span['bracket'] == '(' and designator and LITERAL.fullmatch(designator.group(0) + span['source_chars']):
        return [('literal', 'keep', 'literal_designator')]
    parts = [p.strip(' .') for p in re.split(r'[;,.]\s+|[;,]', bare) if p.strip(' .')]
    if parts and all(PAUSE_PART.match(p) for p in parts):
        out.append(('pause', 'annotate', 'pause_phrase'))
    if parts and any(GARBLE_PART.match(p) for p in parts) and all(
            GARBLE_PART.match(p) or PAUSE_PART.match(p) for p in parts):
        out.append(('garble', 'garble', 'garble_variant'))
    marker = marker_for(bare)
    if marker and words <= 12:
        out.append(('nonspeech', 'keep', 'nonspeech_marker' if low in PLAIN_MARKERS else 'nonspeech_marker+detail'))
    elif SOUNDS.match(bare) and words <= 8:
        out.append(('nonspeech', 'annotate', 'nonspeech_unlisted'))
    if words >= 15 or '<a' in span['source_chars'] or 'http' in bare or re.search(r'\w[.;]\s+[A-Z]', bare):
        out.append(('note', 'to_commentary', 'long_note'))
    if DIRECTION.match(bare):
        out.append(('direction', 'annotate', 'direction_phrase'))
    if NUMBER.search(bare) and UNIT_WORDS.search(bare) and words <= 14:
        out.append(('unit', 'remove', 'number_with_unit'))
    elif UNIT_ONLY.match(bare):
        out.append(('unit', 'remove', 'unit_words'))
    if ACRONYM.match(bare) and not NUMBER.search(bare):
        out.append(('gloss', 'remove', 'acronym'))
    elif low in GLOSSARY or (WORDS.match(bare) and words <= 3 and all(w.casefold() in GLOSSARY for w in bare.split())):
        out.append(('gloss', 'remove', 'glossary'))
    acronym = re.search(r'\b([A-Z][A-Z0-9]{1,6})s?\W*$', before)
    initials = ''.join(w[0] for w in re.split(r'[\s-]+', bare) if w and w[0].isupper())
    if acronym and words >= 2 and initials and acronym.group(1).replace('0', 'O') == initials:
        out.append(('gloss', 'remove', 'acronym_expansion'))
    if WORDS.match(bare) and (attached_before or attached_after) and words == 1 and bare.islower():
        out.append(('inserted', 'remove', 'attached_fragment'))
    elif WORDS.match(bare) and words <= 2 and all(w.casefold() in FUNCTION_WORDS for w in bare.split()):
        out.append(('inserted', 'remove', 'function_word'))
    if low in ('sic', 'emphasis added', 'sp?', 'sp'):
        out.append(('other', 'remove', 'editorial_marker'))
    return out


def rule_judgement(spans: list[dict], index: int) -> dict:
    """Pattern-rule label for editorial span ``index``: class, action, rule and marker (if any)."""
    span = spans[index]
    if span['bracket'].endswith('?'):
        return {'class': 'other', 'action': '', 'rule': 'none', 'marker': '', 'note': 'unbalanced bracket'}
    matches = rule_matches(spans, index)
    if not matches:
        return {'class': 'other', 'action': '', 'rule': 'none', 'marker': '', 'note': ''}
    classes = {m[0] for m in matches}
    unresolved = [(a, b) for a in classes for b in classes if a < b and frozenset((a, b)) not in RESOLVED]
    if unresolved:
        return {'class': 'other', 'action': '', 'rule': 'none', 'marker': '',
                'note': 'conflict: ' + ','.join(sorted(classes))}
    cls, action, rule = min(matches, key=lambda m: PRECEDENCE.index(m[0]))
    return {'class': cls, 'action': action, 'rule': rule,
            'marker': (marker_for(span['text']) or '') if (cls, action) == ('nonspeech', 'keep') else '',
            'note': ('also: ' + ','.join(sorted(classes - {cls}))) if len(classes) > 1 else ''}

# ----------------------------------------------------------- publication
def published_piece(span: dict, decision: dict) -> str:
    if span['kind'] == 'spoken':
        return span['text']
    action, cls = decision.get('action'), decision.get('class')
    if action == 'garble':
        return f' {GARBLE} '
    if action == 'keep':
        if cls == 'nonspeech':
            return f' {decision.get("marker") or marker_for(span["text"]) or ""} '
        if cls == 'literal':
            return span['source_chars']
        return span['text']  # restoration confirmed by review or audio
    return ' '  # removed, annotated or moved; tidy() closes the gap


def assemble(spans: list[dict], decisions: list[dict]) -> str:
    """Published utterance text from spans and their decisions (one per span; spoken ones ignored)."""
    return tidy(''.join(published_piece(s, d or {}) for s, d in zip(spans, decisions)))


def tidy(text: str) -> str:
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\s+([,.;:!?])', r'\1', text)
    text = re.sub(r'\.{4,}', GARBLE, text)
    text = re.sub(r'([,;:])\s*(?=[,;:.])', '', text)
    text = re.sub(r'(\.\.\.)\s*\.', r'\1', text)
    text = re.sub(r'^[\s,;:]+', '', text)
    text = re.sub(r'\(\s+', '(', text)
    text = re.sub(r'\s+\)', ')', text)
    return text.strip()


def release_problems(text: str) -> list[str]:
    """Release check for one published utterance."""
    problems = []
    if '[' in text or ']' in text:
        problems.append('square_bracket')
    stripped = text
    for marker in APPROVED_MARKERS:
        stripped = stripped.replace(marker, '')
    stripped = LITERAL.sub('', stripped)
    if '(' in stripped or ')' in stripped:
        problems.append('parenthesis')
    if UNIT_PATTERN.search(text):
        problems.append('unit')
    return problems
