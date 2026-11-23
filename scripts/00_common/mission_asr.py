"""Chunk ownership, gain-assisted speech gating and batched Whisper decoding.

Every source is cut on a fixed grid of nominal boundaries. Each boundary moves
to the quietest 200 ms inside a short search window. The cut is computed only
from source samples inside that window, so both neighbouring chunks find the
same frame. Every source frame belongs to exactly one chunk and every word is
decoded from one chunk's audio. A stopped run republishes no partial chunk.

The speech detector sees a voice-band, locally gain-adjusted copy. The
recognizer reads the original samples from the retained windows. Each decoder
context contains windows from one chunk of one source. Independent contexts
from many sources share GPU batches.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import math
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, asdict, field

import numpy as np
from scipy import ndimage, signal

from audio_sources import Reader, Source, digest

ASR_RATE = 16_000
MODELS = {
    'distil-large-v3.5': ('distil-whisper/distil-large-v3.5-ct2', '9793ccc07920e0f830e1dba0343efcdf0ef8c903'),
    'large-v3': ('Systran/faster-whisper-large-v3', 'edaa852ec7e145841d8ffdb056a99866b5f0a478'),
}
VOICE_FILTER = signal.butter(4, (220, 3200), btype='bandpass', fs=ASR_RATE, output='sos')
LONG_WORD_SECONDS = 2.5


def boost_voice_band(waveform):
    """Boost quiet voice-band sections without changing their sample coordinates.

    Identical to the tested pilot detector input (research/mission_timing/vad_audio.py).
    """
    filtered = signal.sosfiltfilt(VOICE_FILTER, waveform).astype(np.float32)
    frame = ASR_RATE // 50
    count = (len(filtered) + frame - 1) // frame
    padded = np.pad(filtered, (0, count * frame - len(filtered)))
    frame_rms = np.sqrt(np.mean(padded.reshape(count, frame) ** 2, axis=1))
    mean_power = ndimage.uniform_filter1d(frame_rms ** 2, size=50, mode='nearest')
    smooth_rms = np.sqrt(np.maximum(mean_power, 0))
    gain = np.clip(0.045 / np.maximum(smooth_rms, 0.005), 1, 8)
    smoothed_gain = np.interp(np.arange(len(filtered)), np.arange(count) * frame + frame / 2, gain)
    return np.clip(filtered * smoothed_gain, -0.98, 0.98).astype(np.float32)


@dataclass(frozen=True)
class Settings:
    model: str = 'distil-large-v3.5'
    compute_type: str = 'float16'
    batch_size: int = 16
    beam_size: int = 1
    language: str = 'en'
    vad_threshold: float = 0.12
    vad_min_silence_ms: int = 300
    vad_speech_pad_ms: int = 400
    vad_max_speech_s: float = 28.0
    vad_input: str = 'voice_band_rms'
    context_max_s: float = 29.0
    chunk_seconds: float = 600.0
    cut_search_s: float = 8.0
    cut_window_s: float = 0.2
    vad_margin_s: float = 2.0

    def validate(self):
        if self.model not in MODELS:
            raise ValueError(f'Unknown model {self.model}')
        if not 0 < self.vad_threshold < 1 or self.batch_size < 1 or self.beam_size < 1:
            raise ValueError('Require 0 < VAD threshold < 1 and positive batch and beam sizes')
        if self.vad_input not in ('voice_band_rms', 'original'):
            raise ValueError('vad_input must be voice_band_rms or original')
        if not 0 < self.context_max_s <= 30 or self.vad_max_speech_s > self.context_max_s:
            raise ValueError('Decoder contexts must not exceed 30 s or be shorter than VAD windows')
        if self.chunk_seconds < 10 * self.cut_search_s:
            raise ValueError('Chunks must be much longer than the boundary search')
        return self

    def describe(self):
        model_id, revision = MODELS[self.model]
        boost = hashlib.sha256(inspect.getsource(boost_voice_band).encode()).hexdigest()
        versions = {}
        for package in ('faster-whisper', 'ctranslate2'):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        return dict(asdict(self), model_id=model_id, model_revision=revision, asr_sample_rate=ASR_RATE,
                    boost_source_sha256=boost, temperature=0.0, condition_on_previous_text=False,
                    word_timestamps=True, without_timestamps=False, resampler='scipy.signal.resample_poly',
                    packages=versions, purpose='interim transcript for tape timing', ownership='quietest-frame cuts on a fixed grid; window ownership by chunk')

    @property
    def settings_id(self):
        return 'asr-' + digest(self.describe(), 12)


# --------------------------------------------------------------------------- chunk plan

def boundaries(frames, rate, settings: Settings):
    step = round(settings.chunk_seconds * rate)
    search = round(settings.cut_search_s * rate)
    return [b for b in range(step, frames, step) if b + search + 1 < frames]


def quietest_frame(mono, rate, settings: Settings):
    """Index of the centre of the quietest window; first minimum wins."""
    width = max(1, round(settings.cut_window_s * rate))
    hop = max(1, round(0.01 * rate))
    if len(mono) <= width:
        return len(mono) // 2
    power = np.cumsum(np.r_[0.0, mono.astype(np.float64) ** 2])
    starts = np.arange(0, len(mono) - width + 1, hop)
    energy = power[starts + width] - power[starts]
    return int(starts[int(np.argmin(energy))] + width // 2)


def cut_at(reader: Reader, boundary, settings: Settings, cache=None):
    """Deterministic cut near a nominal boundary, from only that window's samples."""
    if cache is not None and boundary in cache:
        return cache[boundary]
    rate = reader.source.sample_rate
    search = round(settings.cut_search_s * rate)
    window, _ = reader.read(boundary - search, boundary + search)
    cut = boundary - search + quietest_frame(window, rate, settings)
    if cache is not None:
        cache[boundary] = cut
    return cut


@dataclass
class ChunkSpec:
    source: Source
    index: int
    nominal_start: int
    nominal_end: int

    @property
    def name(self):
        return f'c{self.index:05d}'


def chunk_specs(source: Source, settings: Settings, window=None):
    """All chunks of a source, or those whose nominal range meets [start, end) seconds.

    ``window`` uses canonical seconds (tape) or source seconds (SG/DVD).
    """
    edges = [0, *boundaries(source.frames, source.sample_rate, settings), source.frames]
    specs = [ChunkSpec(source, i, a, b) for i, (a, b) in enumerate(zip(edges, edges[1:]))]
    if window:
        start, end = window
        rate, origin = source.sample_rate, source.canonical_origin_s
        lo = -math.inf if start is None else (start - origin) * rate
        hi = math.inf if end is None else (end - origin) * rate
        specs = [s for s in specs if s.nominal_end > lo and s.nominal_start < hi]
    return specs


# --------------------------------------------------------------------------- preparation

@dataclass
class Span:
    packed_start: int
    packed_end: int
    chunk: int            # position in the group's chunk list
    source_start: float   # source frame (fractional) of packed_start


@dataclass
class Prepared:
    spec: ChunkSpec
    cut_start: int
    cut_end: int
    owned_sha256: str
    audio: np.ndarray            # 16 kHz original samples of the read range
    read_start: int              # source frame of audio[0]
    windows: list                # [(asr_start, asr_end, flags)] owned, in 16 kHz indices of audio
    prepare_seconds: float
    vad_seconds: float
    contexts: list = field(default_factory=list)

    @property
    def rate(self):
        return self.spec.source.sample_rate

    def to_source(self, index16):
        return self.read_start + index16 * self.rate / ASR_RATE

    @property
    def owned_seconds(self):
        return (self.cut_end - self.cut_start) / self.rate

    @property
    def retained_seconds(self):
        return sum(b - a for a, b, _ in self.windows) / ASR_RATE


def resample(mono, rate):
    if rate == ASR_RATE:
        return mono.astype(np.float32)
    g = math.gcd(ASR_RATE, rate)
    return signal.resample_poly(mono, ASR_RATE // g, rate // g).astype(np.float32)


def vad_windows(audio16, settings: Settings):
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    detector_input = boost_voice_band(audio16) if settings.vad_input == 'voice_band_rms' else audio16
    options = VadOptions(threshold=settings.vad_threshold, min_silence_duration_ms=settings.vad_min_silence_ms,
                         speech_pad_ms=settings.vad_speech_pad_ms, max_speech_duration_s=settings.vad_max_speech_s)
    return [(int(w['start']), int(w['end'])) for w in get_speech_timestamps(detector_input, options)]


def prepare(spec: ChunkSpec, settings: Settings, cut_cache=None, detector=vad_windows):
    began = time.perf_counter()
    source = spec.source
    reader = Reader(source)
    rate = source.sample_rate
    cut_start = 0 if spec.nominal_start == 0 else cut_at(reader, spec.nominal_start, settings, cut_cache)
    cut_end = source.frames if spec.nominal_end == source.frames else cut_at(reader, spec.nominal_end, settings, cut_cache)
    margin = round(settings.vad_margin_s * rate)
    read_start, read_end = max(0, cut_start - margin), min(source.frames, cut_end + margin)
    mono, raw = reader.read(read_start, read_end, raw_bytes=True)
    # The receipt binds the exact owned source samples (raw PCM, or decoded stream samples).
    width = reader.frame_bytes()
    owned_sha256 = hashlib.sha256(memoryview(raw)[(cut_start - read_start) * width:(cut_end - read_start) * width]).hexdigest()
    del raw
    audio = resample(mono, rate)
    lo = math.ceil((cut_start - read_start) * ASR_RATE / rate)
    hi = min(len(audio), math.ceil((cut_end - read_start) * ASR_RATE / rate))
    vad_began = time.perf_counter()
    windows = []
    for a, b in detector(audio, settings):
        flags = []
        if a < lo:
            a, flags = lo, flags + ['clipped_at_chunk_start']
        if b > hi:
            b, flags = hi, flags + ['clipped_at_chunk_end']
        if b - a >= 160:  # at least 10 ms
            windows.append((a, b, flags))
    vad_seconds = time.perf_counter() - vad_began
    prepared = Prepared(spec, cut_start, cut_end, owned_sha256, audio, read_start, windows,
                        time.perf_counter() - began, vad_seconds)
    prepared.contexts = pack_contexts(windows, settings)
    return prepared


def pack_contexts(windows, settings: Settings):
    """Group consecutive retained windows of one chunk into decoder contexts."""
    limit = round(settings.context_max_s * ASR_RATE)
    contexts, current, length = [], [], 0
    for a, b, _ in windows:
        size = b - a
        if current and length + size > limit:
            contexts.append(current)
            current, length = [], 0
        current.append((a, b))
        length += size
    if current:
        contexts.append(current)
    return contexts


# --------------------------------------------------------------------------- decoding

def pack_group(prepared: list[Prepared]):
    """Concatenate retained samples; return packed audio, spans and context bounds."""
    pieces, spans, contexts = [], [], []
    offset = 0
    for position, item in enumerate(prepared):
        for windows in item.contexts:
            begin = offset
            for a, b in windows:
                pieces.append(item.audio[a:b])
                spans.append(Span(offset, offset + b - a, position, item.to_source(a)))
                offset += b - a
            contexts.append((begin, offset, position))
    audio = np.concatenate(pieces) if pieces else np.zeros(0, np.float32)
    return audio, spans, contexts


def locate(packed, spans, starts, lower, upper, end=False):
    """Map one packed sample index to a source frame inside the context's spans.

    A start belongs to the span with ``packed_start <= x < packed_end``; an end
    to the span with ``packed_start < x <= packed_end``. A word ending exactly
    where the next retained window begins therefore stays in its own window.
    """
    bounded = min(max(packed, lower), upper)
    if end:
        index = bisect_left(starts, bounded) - 1
    else:
        index = bisect_right(starts, bounded) - 1
        if bounded == upper:
            index = bisect_left(starts, bounded) - 1
    first = bisect_left(starts, lower)
    index = max(first, index)
    span = spans[index]
    return index, span, min(max(bounded, span.packed_start), span.packed_end)


def restore(generated, spans, contexts, prepared):
    """Map words and segments to source frames through their own context.

    Nothing is discarded. Questionable endpoints carry flags:
    ``clipped`` (bounded to the context), ``zero_duration``, ``long``,
    ``spans_vad_gap`` (endpoints in different retained windows),
    ``invalid_time`` (non-finite or inverted) and ``outside_context``.
    """
    starts = [s.packed_start for s in spans]
    by_seek = {}
    for index, (begin, end, position) in enumerate(contexts):
        key = int(begin / ASR_RATE * 100)
        if key in by_seek:
            raise ValueError('Two decoder contexts start within one model frame')
        by_seek[key] = index
    out = {i: [] for i in range(len(prepared))}
    for segment in generated:
        try:
            context = by_seek[segment.seek]
        except KeyError as error:
            raise ValueError(f'Unknown decoder context seek {segment.seek}') from error
        begin, end, position = contexts[context]
        item = prepared[position]
        rate = item.rate

        def place(value, is_end=False, lower=begin, upper=end):
            flags = []
            if value is None or not math.isfinite(value):
                return None, None, ['invalid_time']
            packed = round(value * ASR_RATE)
            if packed < lower or packed > upper:
                flags.append('clipped')
            index, span, bounded = locate(packed, spans, starts, lower, upper, is_end)
            frame = span.source_start + (bounded - span.packed_start) * rate / ASR_RATE
            return round(frame), index, flags

        words = []
        for word in segment.words or []:
            s_frame, s_span, s_flags = place(word.start)
            e_frame, e_span, e_flags = place(word.end, True)
            flags = sorted(set(s_flags + e_flags))
            raw = [word.start, word.end]
            if s_frame is not None and e_frame is not None:
                if word.end < word.start:
                    flags.append('invalid_time')
                    e_frame = s_frame
                if s_span != e_span:
                    flags.append('spans_vad_gap')
                if e_frame == s_frame:
                    flags.append('zero_duration')
                elif (e_frame - s_frame) / rate > LONG_WORD_SECONDS:
                    flags.append('long')
                mid = (word.start + word.end) / 2 * ASR_RATE
                if mid < begin or mid > end:
                    flags.append('outside_context')
            words.append(dict(w=word.word, sf=s_frame, ef=e_frame, p=round(float(word.probability), 4),
                              raw=[round(x - begin / ASR_RATE, 3) if x is not None and math.isfinite(x) else None
                                   for x in raw], flags=sorted(set(flags))))
        placed = [w for w in words if w['sf'] is not None]
        seg_start, _, seg_start_flags = place(segment.start)
        seg_end, _, seg_end_flags = place(segment.end, True)
        flags = sorted(set(seg_start_flags + seg_end_flags))
        if placed:
            first, last = min(w['sf'] for w in placed), max(w['ef'] for w in placed)
        else:
            first, last = seg_start, seg_end
            flags.append('no_word_times')
        zero = sum('zero_duration' in w['flags'] for w in words)
        if words and zero / len(words) >= 0.5 and len(words) >= 2:
            flags.append('collapsed_timing')
        if segment.compression_ratio > 2.4:
            flags.append('high_compression')
        if any('spans_vad_gap' in w['flags'] for w in words):
            flags.append('word_spans_vad_gap')
        out[position].append(dict(
            sf=first, ef=last, seg_sf=seg_start, seg_ef=seg_end, text=segment.text, words=words,
            context=context, avg_logprob=round(float(segment.avg_logprob), 4),
            no_speech_prob=round(float(segment.no_speech_prob), 4),
            compression_ratio=round(float(segment.compression_ratio), 4), flags=sorted(set(flags))))
    for rows in out.values():
        rows.sort(key=lambda r: (r['sf'] if r['sf'] is not None else -1, r['context']))
    return out


class WhisperRecognizer:
    """faster-whisper BatchedInferencePipeline over explicit clip boundaries."""

    def __init__(self, settings: Settings, device_index=0, cpu_threads=4):
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        from huggingface_hub import snapshot_download

        model_id, revision = MODELS[settings.model]
        began = time.perf_counter()
        path = snapshot_download(repo_id=model_id, revision=revision, allow_patterns=[
            'config.json', 'model.bin', 'tokenizer.json', 'vocabulary.*', 'preprocessor_config.json'])
        self.pipeline = BatchedInferencePipeline(WhisperModel(
            path, device='cuda', device_index=device_index, compute_type=settings.compute_type, cpu_threads=cpu_threads))
        self.settings = settings
        self.load_seconds = time.perf_counter() - began

    def __call__(self, audio, contexts):
        if not contexts:
            return []
        # Whisper truncates second-based clip boundaries; nudge by a quarter sample.
        clips = [{'start': (a + 0.25) / ASR_RATE, 'end': (b + 0.25) / ASR_RATE} for a, b, _ in contexts]
        generated, _ = self.pipeline.transcribe(
            audio, language=self.settings.language, beam_size=self.settings.beam_size,
            batch_size=self.settings.batch_size, temperature=0.0, word_timestamps=True,
            without_timestamps=False, condition_on_previous_text=False, clip_timestamps=clips, vad_filter=False)
        return list(generated)


def decode_group(recognizer, prepared: list[Prepared]):
    began = time.perf_counter()
    audio, spans, contexts = pack_group(prepared)
    generated = recognizer(audio, contexts)
    rows = restore(generated, spans, contexts, prepared)
    return rows, time.perf_counter() - began, len(contexts)
