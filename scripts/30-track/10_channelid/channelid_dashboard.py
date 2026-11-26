"""Rich console dashboard for stage 10: progress, live GLM streams and per-tape results."""
from __future__ import annotations

import collections
import threading
import time

from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class State:
    def __init__(self, model, tapes, workers):
        self.lock = threading.Lock()
        self.model = model
        self.t0 = time.monotonic()
        self.tapes = list(tapes)
        self.workers = workers
        self.tape_status = {t: 'queued' for t in tapes}
        self.jobs_total = 0
        self.jobs_done = 0
        self.jobs_cached = 0
        self.jobs_failed = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.llm_seconds = 0.0
        self.slots = {}                    # worker name -> dict
        self.tables = {}                   # tape -> {d: row}
        self.focus = None
        self.results = collections.deque(maxlen=60)
        self.events = collections.deque(maxlen=8)

    # ---- updates (thread-safe)
    def event(self, text):
        with self.lock:
            self.events.append(f'{time.strftime("%H:%M:%S")} {text}')

    def tape(self, tape, status):
        with self.lock:
            self.tape_status[tape] = status
            if status in ('assessing',):
                self.focus = tape

    def add_jobs(self, tape, channels, labels):
        with self.lock:
            self.jobs_total += len(channels)
            self.tables[tape] = {d: dict(label=labels.get(d, ''), status='queued') for d in channels}

    def start(self, tape, d):
        name = threading.current_thread().name
        with self.lock:
            self.slots[name] = dict(tape=tape, d=d, reasoning='', content='', t=time.monotonic())
            self.tables[tape][d]['status'] = 'running'
            self.focus = tape

    def delta(self, kind, text):
        name = threading.current_thread().name
        with self.lock:
            slot = self.slots.get(name)
            if slot is not None:
                key = 'content' if kind == 'content' else 'reasoning'
                slot[key] = (slot[key] + text)[-4000:]

    def finish(self, tape, d, record, cached, sheet):
        name = threading.current_thread().name
        with self.lock:
            self.jobs_done += 1
            self.jobs_cached += bool(cached)
            if not cached:
                usage = record.get('meta', {}).get('usage') or {}
                self.tokens_in += usage.get('prompt_tokens', 0)
                self.tokens_out += usage.get('completion_tokens', 0)
                self.llm_seconds += record.get('meta', {}).get('elapsed_s', 0)
            top = record['parsed']['candidates'][0]
            row = self.tables[tape][d]
            row.update(status='cached' if cached else 'done', top=top['channel'], p=top['p'],
                       name=sheet.get(top['channel'], {}).get('label', 'none') if top['channel'] else 'none',
                       summary=record['parsed']['summary'])
            if name in self.slots:
                self.slots[name]['done'] = True

    def fail(self, tape, d, exc):
        with self.lock:
            self.jobs_done += 1
            self.jobs_failed += 1
            self.tables[tape][d]['status'] = 'FAILED'
            self.events.append(f'{time.strftime("%H:%M:%S")} {tape} CH{d:02d} failed: {exc}')

    def result(self, tape, line):
        with self.lock:
            self.results.append((tape, line))
            self.tape_status[tape] = 'solved'

    # ---- rendering
    def __rich__(self):
        with self.lock:
            return self._render()

    def _render(self):
        el = time.monotonic() - self.t0
        live = self.jobs_done - self.jobs_cached
        rate = live / el if el > 0 else 0
        remaining = self.jobs_total - self.jobs_done
        eta = remaining / rate if rate > 0 else 0
        solved = sum(s == 'solved' for s in self.tape_status.values())
        head = Text.assemble(
            ('Stage 10 channel identification  ', 'bold cyan'), (f'model {self.model}  ', 'magenta'),
            f'tapes {solved}/{len(self.tapes)}  channels {self.jobs_done}/{self.jobs_total} '
            f'(cached {self.jobs_cached}, failed {self.jobs_failed})  tokens in {self.tokens_in:,} out '
            f'{self.tokens_out:,}  elapsed {el / 60:.1f} min  ETA {eta / 60:.1f} min')

        layout = Layout()
        layout.split_column(Layout(Panel(head), size=3), Layout(name='mid'), Layout(name='bottom', size=14))
        layout['mid'].split_row(Layout(self._table(), name='table', ratio=5), Layout(self._streams(), ratio=6))
        layout['bottom'].split_row(Layout(self._results(), ratio=5), Layout(self._events(), ratio=6))
        return layout

    def _table(self):
        tape = self.focus
        table = Table(expand=True, box=None, padding=(0, 1), show_edge=False)
        for col, kw in (('dlv', {}), ('file label', {}), ('GLM says', {}), ('p', {'justify': 'right'}),
                        ('summary', {'ratio': 1, 'no_wrap': True})):
            table.add_column(col, **kw)
        if tape and tape in self.tables:
            for d, row in sorted(self.tables[tape].items()):
                if 'top' in row:
                    style = 'green' if row['p'] >= 0.7 else 'yellow' if row['p'] >= 0.4 else 'red'
                    table.add_row(f'{d:02d}', row['label'], f'{row["top"]:>2} {row["name"]}',
                                  Text(f'{row["p"]:.2f}', style=style), row['summary'][:200])
                else:
                    table.add_row(f'{d:02d}', row['label'], Text(row['status'], style='dim'), '', '')
        return Panel(table, title=f'[bold]{tape or "-"}[/] delivered channel -> GLM true-channel guess',
                     border_style='cyan')

    def _streams(self):
        parts = []
        active = [(n, s) for n, s in sorted(self.slots.items()) if not s.get('done')]
        shown = active[: self.workers] or sorted(self.slots.items())[-1:]
        for name, slot in shown:
            reasoning = slot['reasoning'][-600:].replace('\n', ' ')
            content = slot['content'][-500:].replace('\n', ' ')
            age = time.monotonic() - slot['t']
            body = Text.assemble((reasoning or '…', 'italic dim'), '\n', (content, 'white'))
            parts.append(Panel(body, title=f'{slot["tape"]} CH{slot["d"]:02d} ({age:.0f}s)', height=9,
                               border_style='magenta' if not slot.get('done') else 'dim'))
        return Panel(Group(*parts) if parts else Text('waiting for work', style='dim'),
                     title='GLM conversation (reasoning dim, answer white)', border_style='magenta')

    def _results(self):
        table = Table(expand=True, box=None, padding=(0, 1), show_edge=False)
        table.add_column('tape')
        table.add_column('result', ratio=1, no_wrap=True)
        for tape, line in list(self.results)[-11:]:
            table.add_row(tape, line)
        return Panel(table, title='Solved tapes', border_style='green')

    def _events(self):
        status = collections.Counter(self.tape_status.values())
        lines = [', '.join(f'{k} {v}' for k, v in sorted(status.items()))] + list(self.events)
        return Panel(Text('\n'.join(lines)), title='Events', border_style='blue')
