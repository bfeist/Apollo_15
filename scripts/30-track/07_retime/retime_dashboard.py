"""Live dashboard of the 07b render across Proprius and Genus (docs/23, "Running 07b").

The coordinator (``07b_retime.py --all-tapes --all-channels``) shows it in place of its log lines
when it runs in a terminal (``--plain`` prints the log instead; the log file is written either
way). From another terminal, on either host:

  uv run python scripts/run.py retime_dashboard.py            # Ctrl-C closes the view only
  uv run python scripts/run.py retime_dashboard.py --all-tapes

It reads files only, so it never touches the render:

* the channel inventory: every 05b ``deflutter.wav`` (its size is the work), and which channels
  have a published ``retime.json`` under the retimed root;
* the tape queue (``<retimed root>/.queue``): claims, done and failed tapes;
* each host's status (``.queue/hosts/<host>.json``, rewritten every 2 s by retime_cluster.HostStatus):
  the channels it holds with their phase and render progress.

Rates are measured from progress over the last ``WINDOW_S`` (published bytes plus the rendered
share of running channels), so ETAs need a few minutes to warm up.
"""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text

BYTES_PER_AUDIO_S = 8000 * 4   # 05b channels: 8 kHz 32-bit float mono
WINDOW_S = 900.0               # rate window
WARMUP_S = 120.0
DONE_RECHECK_S = 20.0
HOST_STALE_S = 60.0
PHASE_ORDER = {'rendering': 0, 'publishing': 1, 'staging in': 2, 'waiting for disk': 3, 'staged': 4}


def duration(seconds):
    if seconds is None:
        return 'warming up'
    if seconds <= 0:
        return 'finishing'
    minutes = max(1, round(seconds / 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return f'~{days}d {hours}h {minutes:02d}m' if days else f'~{hours}h {minutes:02d}m'


def clock(seconds):
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    return f'{h}:{r // 60:02d}:{r % 60:02d}' if h else f'{r // 60}:{r % 60:02d}'


def finish_time(seconds):
    if not seconds or seconds <= 0:
        return ''
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(time.time() + seconds, ZoneInfo('America/New_York')).strftime('%a %b %d %I:%M %p %Z')
    except (KeyError, ValueError, ImportError):
        return datetime.fromtimestamp(time.time() + seconds).strftime('%a %b %d %H:%M')


def audio_hours(n_bytes):
    return n_bytes / BYTES_PER_AUDIO_S / 3600


def hours_text(n_bytes):
    h = audio_hours(n_bytes)
    return f'{h:,.1f}' if h < 10 else f'{h:,.0f}'


def rate_text(bytes_per_s):
    return 'warming up' if bytes_per_s is None else f'{audio_hours(bytes_per_s) * 3600:.1f} audio h/h'


class Rate:
    """Progress samples (time, bytes) over the last ``WINDOW_S``; reset when progress goes back."""

    def __init__(self):
        self.samples = deque()

    def observe(self, now, value):
        if self.samples and value < self.samples[-1][1] - 1:
            self.samples.clear()
        self.samples.append((now, value))
        while len(self.samples) > 2 and self.samples[1][0] < now - WINDOW_S:
            self.samples.popleft()

    def value(self):
        if len(self.samples) < 2:
            return None
        (t0, v0), (t1, v1) = self.samples[0], self.samples[-1]
        if t1 - t0 < WARMUP_S or v1 <= v0:
            return None
        return (v1 - v0) / (t1 - t0)


class Inventory:
    """Every channel to render and whether it is published (rechecked every ``DONE_RECHECK_S``)."""

    def __init__(self, defluttered_root, retimed_root, tapes=None):
        self.retimed_root = Path(retimed_root)
        root = Path(defluttered_root)
        names = tapes or sorted(p.name for p in root.glob('A15_T*') if any(p.glob('CH*/deflutter.wav')))
        self.channels = {}
        for tape in names:
            for wav in sorted((root / tape).glob('CH*/deflutter.wav')):
                self.channels[(tape, wav.parent.name)] = wav.stat().st_size
        self.tapes = sorted({t for t, _ in self.channels})
        self.done = set()
        self.checked = 0.0

    def refresh(self, force=False):
        if not force and time.time() - self.checked < DONE_RECHECK_S:
            return
        for key in self.channels:
            if key not in self.done and (self.retimed_root / key[0] / key[1] / 'retime.json').is_file():
                self.done.add(key)
        self.checked = time.time()


class Dashboard:
    def __init__(self, defluttered_root, retimed_root, tapes=None, all_tapes=False, log_lines=None, limits=None):
        self.inventory = Inventory(defluttered_root, retimed_root, tapes)
        self.queue = Path(retimed_root) / '.queue'
        self.all_tapes = all_tapes
        self.log_lines = log_lines          # a deque of recent log lines (the coordinator), or None
        self.limits = limits or {}
        self.rates = {}
        self.started = time.time()
        self.stopping = False

    def _hosts(self):
        hosts = {}
        for f in sorted((self.queue / 'hosts').glob('*.json')):
            try:
                hosts[f.stem] = json.loads(f.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
        return hosts

    def _queue(self):
        state = {}
        for tape in self.inventory.tapes:
            for kind in ('done', 'failed', 'claim'):
                f = self.queue / f'{tape}.{kind}'
                if f.is_file():
                    try:
                        state[tape] = (kind, json.loads(f.read_text(encoding='utf-8')))
                    except (OSError, ValueError):
                        state[tape] = (kind, {})
                    break
        return state

    def snapshot(self):
        now = time.time()
        inv = self.inventory
        inv.refresh()
        hosts, queue = self._hosts(), self._queue()
        failed_tapes = {t: r.get('failures', []) for t, (k, r) in queue.items() if k == 'failed'}
        running = {}
        for label, h in hosts.items():
            live = h.get('state') == 'running' and now - h.get('updated', 0) < HOST_STALE_S
            h['live'] = live
            h['partial'] = 0.0
            for job in h.get('jobs', []) if live else []:
                size = inv.channels.get((job['tape'], job['channel']), job.get('bytes') or 0)
                pct = job.get('percent') if job.get('phase') == 'rendering' else (100.0 if job.get('phase') == 'publishing' else 0.0)
                job['fraction'] = (pct or 0.0) / 100.0
                h['partial'] += job['fraction'] * size
                running[(job['tape'], job['channel'])] = (label, job)
        total = sum(inv.channels.values())
        done = sum(inv.channels[k] for k in inv.done)
        blocked = sum(b for (t, _), b in inv.channels.items() if t in failed_tapes and (t, _) not in inv.done)
        partial = sum(h['partial'] for h in hosts.values())
        self.rates.setdefault('all', Rate()).observe(now, done + partial)
        for label, h in hosts.items():
            if h['live']:
                self.rates.setdefault(label, Rate()).observe(now, h.get('done_bytes', 0) + h['partial'])
        rate = self.rates['all'].value()
        remaining = max(0.0, total - done - blocked - partial)
        tapes = {}
        for (tape, ch), size in inv.channels.items():
            row = tapes.setdefault(tape, dict(channels=0, done=0, bytes=0, remaining=0.0, running=0, hosts=set()))
            row['channels'] += 1
            row['bytes'] += size
            if (tape, ch) in inv.done:
                row['done'] += 1
            else:
                frac = running[(tape, ch)][1]['fraction'] if (tape, ch) in running else 0.0
                row['remaining'] += size * (1 - frac)
            if (tape, ch) in running:
                row['running'] += 1
                row['hosts'].add(running[(tape, ch)][0])
        for tape, row in tapes.items():
            kind, rec = queue.get(tape, ('open', {}))
            row['state'] = kind
            if kind == 'claim':
                row['hosts'].add(str(rec.get('worker', '?')).split(':')[0].lower())
        return dict(now=now, total=total, done=done, remaining=remaining, blocked=blocked, rate=rate,
                    eta=remaining / rate if rate else None, hosts=hosts, tapes=tapes, running=running,
                    failed=failed_tapes, channels=len(inv.channels), channels_done=len(inv.done))

    def render(self):
        s = self.snapshot()
        tapes_done = sum(1 for r in s['tapes'].values() if r['done'] == r['channels'])
        headline = Text.assemble(
            ('Apollo 15 07b retime  ', 'bold'),
            f"{s['channels_done']}/{s['channels']} channels  |  {tapes_done}/{len(s['tapes'])} tapes complete",
            (f"  |  {len(s['failed'])} tapes blocked" if s['failed'] else '', 'bold red'))
        eta = 'stopping' if self.stopping else ('complete' if s['remaining'] <= 0 else duration(s['eta']))
        finish = '' if self.stopping else finish_time(s['eta'])
        pct = 100 * s['done'] / s['total'] if s['total'] else 0
        overall = Text(f"Overall {pct:.1f}% of {hours_text(s['total'])} audio h  |  combined rate "
                       f"{rate_text(s['rate'])}  |  ETA {eta}" + (f'  ({finish})' if finish else '')
                       + f"  |  {hours_text(s['remaining'])} audio h remaining")

        machines = Table('Machine', 'State', 'Render workers', 'Staged / disk', 'Channels done', 'Rate', 'Tapes held',
                         'Update', title='Machines', title_justify='left')
        for label, h in s['hosts'].items():
            jobs = h.get('jobs', []) if h['live'] else []
            rendering = sum(1 for j in jobs if j.get('phase') == 'rendering')
            waiting = sum(1 for j in jobs if j.get('phase') in ('staged', 'staging in', 'waiting for disk', 'publishing'))
            age = s['now'] - h.get('updated', 0)
            state = h.get('state', '?') if h['live'] or h.get('state') != 'running' else 'no update (stopped?)'
            machines.add_row(label, Text(state, style='green' if h['live'] else 'yellow'),
                             f"{rendering}/{h.get('workers', '?')}", str(waiting),
                             f"{h.get('done_channels', 0)}" + (f" ({h.get('failed_channels')} failed)" if h.get('failed_channels') else ''),
                             rate_text(self.rates[label].value()) if h['live'] and label in self.rates else '-',
                             ', '.join(sorted({j['tape'].replace('A15_', '') for j in jobs})) or '-',
                             f'{age:.0f} s ago')

        tapes = Table('Tape', 'State', 'Host', 'Channels done', 'Running', 'Audio remaining', title='Tapes',
                      title_justify='left')
        for tape, row in s['tapes'].items():
            if not self.all_tapes and row['state'] != 'claim' and not row['running']:
                continue
            state = {'claim': 'rendering', 'done': 'done', 'failed': 'BLOCKED', 'open': 'queued'}[row['state']]
            tapes.add_row(tape.replace('A15_', ''), state, ', '.join(sorted(row['hosts'])) or '-',
                          f"{row['done']}/{row['channels']}", str(row['running']), f"{audio_hours(row['remaining']):.1f} h")

        workers = Table('Machine', 'Tape / channel', 'Phase', 'Progress', 'Elapsed', 'Channel ETA', 'Last line',
                        title='Channels in flight', title_justify='left')
        flight = sorted(s['running'].values(), key=lambda x: (x[0], PHASE_ORDER.get(x[1].get('phase'), 9), x[1]['tape'], x[1]['channel']))
        for label, job in flight:
            elapsed = s['now'] - job.get('since', s['now'])
            frac = job['fraction']
            ceta = '' if job.get('phase') != 'rendering' or not frac else duration(elapsed * (1 - frac) / frac)
            progress = f"{100 * frac:.0f}%" if job.get('phase') == 'rendering' and job.get('percent') is not None else ''
            workers.add_row(label, f"{job['tape'].replace('A15_', '')} {job['channel']}", job.get('phase', '?'), progress,
                            clock(elapsed), ceta, (job.get('text') or '')[:48])
        parts = [headline, overall, machines, tapes, workers]
        if s['failed']:
            blocked = Table('Tape', 'Reason', title='Blocked tapes (retried by the next run)', title_justify='left')
            for tape, reasons in s['failed'].items():
                blocked.add_row(tape.replace('A15_', ''), '; '.join(reasons)[:150])
            parts.append(blocked)
        if self.log_lines is not None:
            parts.append(Text('\n'.join(list(self.log_lines)[-10:]), style='dim'))
        parts.append(Text('Stopping both machines...' if self.stopping else
                          'Ctrl+C stops both machines; rerunning the same command resumes.' if self.log_lines is not None
                          else 'Viewer only: Ctrl+C closes this view and leaves the render running.', style='italic'))
        return Group(*parts)


def main():
    from rich.live import Live
    from sg_inputs import DEFLUTTERED_ROOT, TAPE_DATA
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--defluttered-root', type=Path, default=DEFLUTTERED_ROOT)
    p.add_argument('--retimed-root', type=Path, default=TAPE_DATA / 'retimed')
    p.add_argument('--tapes', help='comma-separated tapes (default: every tape with 05b channels)')
    p.add_argument('--all-tapes', action='store_true', help='also list queued, done and blocked tapes')
    args = p.parse_args()
    tapes = [t.strip() for t in args.tapes.split(',')] if args.tapes else None
    board = Dashboard(args.defluttered_root, args.retimed_root, tapes, args.all_tapes)
    try:
        with Live(get_renderable=board.render, refresh_per_second=0.5):
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
