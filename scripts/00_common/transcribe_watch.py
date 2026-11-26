"""Read-only Rich dashboard for a stage-06a run: progress plus a scrolling feed of new transcript.

The watcher never touches the running worker. It tails the newest first-pass
session's ``progress.jsonl`` and, for every group that lands, reads the chunks
named in that group's ledger. Each group publishes several channel-hours at
once, so its segments are released gradually over the expected time to the next
group; when there are more than can be read, an even sample in time order is shown.
"""
from __future__ import annotations

import gzip
import json
import re
import time
import zlib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from mission_store import pid_alive, read_json

SOURCE_COLOURS = ('cyan', 'magenta', 'green', 'yellow', 'bright_blue', 'bright_red', 'bright_green',
                  'bright_magenta', 'bright_cyan', 'orange1', 'orchid', 'spring_green2')
# Segment flags meaning the text or its timing is questionable; shown dim, never hidden.
DIM_FLAGS = {'collapsed_timing', 'high_compression', 'no_word_times'}
WORDLIKE = re.compile(r'\w')


# ----------------------------------------------------------------- reading the store

def latest_session(evidence_root, session_id=None, look_back=12):
    """Folder of the requested session, or of the newest first-pass session."""
    base = Path(evidence_root) / 'sessions'
    if session_id:
        folder = base / session_id
        return folder if (folder / 'session.json').is_file() else None
    if not base.is_dir():
        return None
    best = None
    for folder in sorted(base.iterdir())[-look_back:]:
        try:
            record = read_json(folder / 'session.json')
        except (OSError, json.JSONDecodeError):
            continue
        if record.get('kind') != 'first_pass':
            continue
        key = (record.get('started_ns', 0), folder.name)
        if best is None or key > best[0]:
            best = (key, folder)
    return best[1] if best else None


class ProgressTail:
    """Incremental reader of a session's progress.jsonl; only complete lines are consumed."""

    def __init__(self, folder):
        self.path = Path(folder) / 'progress.jsonl'
        self.offset = 0

    def poll(self):
        if not self.path.is_file():
            return []
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            data = stream.read()
        end = data.rfind(b'\n') + 1
        self.offset += end
        rows = []
        for line in data[:end].splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows


def chunk_folder(transcript_root, settings_id, key, identity_id, index):
    return Path(transcript_root) / settings_id / 'sources' / Path(*key.split('/')) / identity_id / 'chunks' / f'c{index:05d}'


def short_label(key):
    """'tape/A15_T510093/CH05' -> 'T510093 CH05'; SG/DVD keys keep their trailing parts."""
    parts = key.split('/')[1:] or [key]
    return ' '.join(p[4:] if p.startswith('A15_') else p for p in parts[-2:])


def group_segments(transcript_root, settings_id, ledger, channels=None, min_words=1, pattern=None):
    """Segments of one progress group's chunks, in canonical-time order."""
    out = []
    for entry in ledger:
        key, identity_id, index = entry[0], entry[1], entry[2]
        if channels and not any(key.endswith(f'/CH{c:02d}') for c in channels):
            continue
        folder = chunk_folder(transcript_root, settings_id, key, identity_id, index)
        try:
            receipt = read_json(folder / 'chunk.json')
            with gzip.open(folder / 'segments.jsonl.gz', 'rt', encoding='utf-8') as stream:
                rows = [json.loads(line) for line in stream if line.strip()]
        except (OSError, json.JSONDecodeError, EOFError):
            continue
        rate, origin = receipt['sample_rate'], receipt.get('canonical_origin_s', 0.0)
        for row in rows:
            text = (row.get('text') or '').strip()
            frame = row.get('sf') if row.get('sf') is not None else row.get('seg_sf')
            if not WORDLIKE.search(text) or frame is None or len(text.split()) < min_words:
                continue
            if pattern and not pattern.search(text):
                continue
            out.append(dict(t=origin + frame / rate, key=key, text=text, flags=row.get('flags', [])))
    out.sort(key=lambda s: (s['t'], s['key']))
    return out


def even_sample(items, count):
    if count >= len(items):
        return list(items)
    if count <= 0:
        return []
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


# ----------------------------------------------------------------- feed pacing

class Feed:
    """Scrollback of displayed segments plus a paced queue of segments still to show."""

    def __init__(self, keep=500, max_per_second=6.0):
        self.lines = deque(maxlen=keep)
        self.max_per_second = max_per_second
        self.queue, self.released, self.began, self.duration = [], 0, 0.0, 1.0
        self.shown = self.sampled_out = 0

    def show_now(self, segments):
        self.lines.extend(segments)
        self.shown += len(segments)

    def schedule(self, segments, duration, now=None):
        """Release ``segments`` evenly over ``duration`` seconds, replacing any unreleased backlog."""
        now = time.monotonic() if now is None else now
        self.sampled_out += len(self.queue) - self.released
        duration = max(duration, 1.0)
        chosen = even_sample(segments, int(duration * self.max_per_second)) if self.max_per_second else segments
        self.sampled_out += len(segments) - len(chosen)
        self.queue, self.released, self.began, self.duration = chosen, 0, now, duration

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if not self.queue:
            return 0
        due = min(len(self.queue), int((now - self.began) / self.duration * len(self.queue)) + 1)
        fresh = self.queue[self.released:due]
        self.released = max(self.released, due)
        self.show_now(fresh)
        return len(fresh)

    @property
    def backlog(self):
        return len(self.queue) - self.released


# ----------------------------------------------------------------- rendering

def clock(seconds):
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{int(hours):d}:{int(minutes):02d}:{secs:04.1f}'


def duration(seconds):
    if seconds is None:
        return '-'
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    return f'{hours:d}h{rest // 60:02d}m' if hours else f'{rest // 60:d}m{rest % 60:02d}s'


def colour_for(key):
    return SOURCE_COLOURS[zlib.crc32(key.encode()) % len(SOURCE_COLOURS)]


class FeedView:
    """Newest segments at the bottom, as many as fit the region; one line each."""

    def __init__(self, feed):
        self.feed = feed

    def __rich_console__(self, console, options):
        height = options.height or console.size.height
        width = options.max_width
        lines = list(self.feed.lines)[-height:] if height > 0 else []
        for _ in range(height - len(lines)):
            yield Text('')
        for item in lines:
            dim = bool(DIM_FLAGS & set(item['flags']))
            line = Text(no_wrap=True, overflow='ellipsis')
            line.append(f'{clock(item["t"]):>11} ', style='dim' if dim else 'bold white')
            line.append(f'{short_label(item["key"]):<13} ', style=colour_for(item['key']))
            line.append(item['text'], style='dim italic' if dim else '')
            line.truncate(width, overflow='ellipsis')
            yield line


class State:
    def __init__(self):
        self.folder = None
        self.record = {}
        self.rows = []
        self.align = None      # latest alignment progress row (aligning published chunks)
        self.alive = False
        self.failures = 0
        self.last_group_at = None


def status_of(state):
    status = state.record.get('status', '?')
    if status == 'running' and not state.alive:
        return 'stopped (worker gone)', 'red'
    return status, {'running': 'green', 'complete': 'bright_blue', 'stopped': 'yellow'}.get(status, 'red')


def header(state, feed, now_wall=None):
    if state.folder is None:
        return Panel(Text('Waiting for a stage-06a first-pass session...', style='yellow'),
                     title='Stage 06a transcription', border_style='yellow')
    record, rows = state.record, state.rows
    now_wall = time.time() if now_wall is None else now_wall
    status, colour = status_of(state)
    started_ns = record.get('started_ns')
    elapsed = (now_wall - started_ns / 1e9) if started_ns else None
    if record.get('wall_seconds') and status not in ('running',):
        elapsed = record['wall_seconds']
    done = rows[-1]['done_hours'] if rows else 0.0
    remaining = rows[-1]['remaining_hours'] if rows else record.get('pending_channel_hours', 0.0)
    reused = record.get('reused_channel_hours', 0.0)
    selected = record.get('selected_channel_hours') or (reused + done + remaining) or 1.0
    run_elapsed = rows[-1]['elapsed_seconds'] if rows else None
    rate = done * 3600 / run_elapsed if run_elapsed else None
    last = rows[-1] if rows else None
    last_rate = last['source_hours'] * 3600 / max(last['wall_seconds'], 1e-9) if last else None
    retained = sum(r['retained_seconds'] for r in rows)
    words = sum(r['words'] for r in rows)
    flagged = sum(r['flagged_word_marks'] for r in rows)
    eta = remaining / rate * 3600 if rate and status == 'running' else None
    since = now_wall - state.last_group_at if state.last_group_at else None

    bar = Table.grid(expand=True)
    bar.add_column(ratio=1)
    bar.add_column(justify='right')
    complete = reused + done
    bar.add_row(ProgressBar(total=selected, completed=complete, complete_style='green', finished_style='bright_green'),
                Text(f' {complete:,.1f} / {selected:,.1f} ch-h  {complete / selected:5.1%}', style='bold'))

    grid = Table.grid(padding=(0, 3))
    for _ in range(4):
        grid.add_column(style='dim')
        grid.add_column()
    grid.add_row('status', Text(status, style=f'bold {colour}'), 'session', f'{done:,.2f} ch-h',
                 'rate', f'{rate:,.0f}x' if rate else '-', 'ETA', Text(duration(eta), style='bold'))
    grid.add_row('elapsed', duration(elapsed), 'remaining', f'{remaining:,.2f} ch-h',
                 'last group', f'{last_rate:,.0f}x' if last_rate else '-', 'since group', duration(since))
    grid.add_row('groups', f'{len(rows):,}', 'speech', f'{retained / (done * 3600):.1%}' if done else '-',
                 'words', f'{words:,}', 'flag marks', f'{flagged / words:.1%}' if words else '-')
    if state.align:
        a = state.align
        grid.add_row('aligned', f"{a['chunks']:,} / {record.get('unaligned_chunks', 0):,} chunks", 'aligned ch-h',
                     f"{a['aligned_source_hours']:,.1f}", 'align rate',
                     f"{a['aligned_source_hours'] * 3600 / max(a['elapsed_seconds'], 1e-9):,.0f}x",
                     'failed ctx', f"{a['failed_contexts']:,}")
    grid.add_row('reused', f'{reused:,.1f} ch-h', 'failures',
                 Text(f'{state.failures}', style='bold red' if state.failures else ''),
                 'feed', f'{feed.shown:,} shown', 'sampled out', f'{feed.sampled_out:,}')
    where = ''
    if last and last.get('ledger'):
        tapes = sorted({short_label(e[0]).split(' ')[0] for e in last['ledger']})
        where = f"  now on {', '.join(tapes)}"
    title = f"Stage 06a transcription - session {state.folder.name}{where}"
    return Panel(Group(bar, Text(''), grid), title=title, title_align='left', border_style=colour)


def dashboard(state, feed):
    layout = Layout()
    layout.split_column(Layout(header(state, feed), name='header', size=9 if state.align else 8),
                        Layout(Panel(FeedView(feed), title='Recent transcript (canonical tape time, source)',
                                     title_align='left', subtitle='dim = collapsed timing / high compression; '
                                     'Ctrl+C closes the watcher only', subtitle_align='right',
                                     border_style='grey50'), name='feed'))
    return layout


# ----------------------------------------------------------------- loop

def watch(transcript_root, evidence_root, *, session_id=None, channels=None, min_words=1, grep=None,
          max_per_second=6.0, poll_seconds=2.0, seed_groups=1, screen=True, console=None):
    console = console or Console()
    pattern = re.compile(grep, re.IGNORECASE) if grep else None
    feed = Feed(max_per_second=max_per_second)
    state = State()
    tail = None
    next_poll = 0.0

    def refresh_session():
        nonlocal tail
        folder = latest_session(evidence_root, session_id)
        if folder is None:
            return
        if folder != state.folder:
            state.folder, state.rows, state.last_group_at = folder, [], None
            tail = ProgressTail(folder)
            first = tail.poll()
            state.align = next((r for r in reversed(first) if r.get('kind') == 'align'), None)
            first = [r for r in first if r.get('kind') != 'align']
            state.rows.extend(first)
            record = read_json(folder / 'session.json')
            for row in first[-seed_groups:] if seed_groups else []:
                feed.show_now(group_segments(transcript_root, record['settings_id'], row.get('ledger', []),
                                             channels, min_words, pattern)[-200:])
            if first:
                state.last_group_at = parse_time(first[-1]['t'])
        try:
            state.record = read_json(folder / 'session.json')
        except (OSError, json.JSONDecodeError):
            pass
        state.alive = pid_alive(int(state.record.get('pid', -1)))
        failures = folder / 'failures.jsonl'
        state.failures = sum(1 for _ in failures.open('rb')) if failures.is_file() else 0
        new = tail.poll()
        for row in new:
            if row.get('kind') == 'align':
                state.align = row
                continue
            state.rows.append(row)
            state.last_group_at = parse_time(row['t']) or time.time()
            segments = group_segments(transcript_root, state.record['settings_id'], row.get('ledger', []),
                                      channels, min_words, pattern)
            # Spread this group over roughly the time the next one is expected to take.
            feed.schedule(segments, 0.95 * row.get('wall_seconds', 30.0))

    with Live(dashboard(state, feed), console=console, screen=screen, refresh_per_second=8,
              transient=False) as live:
        try:
            while True:
                now = time.monotonic()
                if now >= next_poll:
                    refresh_session()
                    next_poll = now + poll_seconds
                feed.tick()
                live.update(dashboard(state, feed))
                time.sleep(0.125)
        except KeyboardInterrupt:
            pass
    return 0


def parse_time(stamp):
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
