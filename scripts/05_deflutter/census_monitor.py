"""Interactive terminal display for the stage 05 coordinator."""
from collections import Counter
from datetime import timedelta
from itertools import zip_longest
import time

from rich.console import Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text


def duration(seconds):
    return str(timedelta(seconds=max(0, int(seconds))))


class CensusMonitor:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.started = time.monotonic()
        self.activity = 'Preparing the census'
        self.state = {}
        self.tape_progress = ''

    def message(self, text):
        self.activity = text

    def update(self, state):
        total = Counter()
        done = Counter()
        for key, job in state['jobs'].items():
            tape = key.split('/')[0]
            total[tape] += 1
            if job['status'] == 'complete':
                done[tape] += 1
        finished = sum(done[t] == n for t, n in total.items())
        self.tape_progress = f'{finished}/{len(total)} tapes' + ''.join(
            f'    {t}  {done[t]}/{total[t]}' for t in state['active_tapes'])
        self.state = state

    def __rich__(self):
        s = self.state
        items = [Text(str(self.run_dir), style='dim'), Text(self.activity, style='bold cyan')]
        if s:
            counts = s['counts']
            complete, total = counts['complete'], len(s['jobs'])
            rate = s['tape_hours_per_wall_hour']
            eta = duration(s['remaining_tape_seconds']/rate) if rate > 0 else 'measuring'
            progress = Table.grid(expand=True)
            progress.add_row(ProgressBar(total=max(1, total), completed=complete))
            items.extend([
                progress,
                Text(f'{complete:,}/{total:,} blocks    {self.tape_progress}'),
                Text(f'{rate:.1f} tape h/h    ETA {eta}    Failures {len(s["failures"])}',
                     style='red' if s['failures'] else ''),
            ])
            hosts = Table(expand=True, box=None, pad_edge=False)
            columns = []
            for host, name in [('windows', 'Proprius'), ('genus', 'Genus')]:
                h = s['hosts'][host]
                hosts.add_column(f'{name}  {h["active"]}/{h["limit"]} busy   {h["tape_hours_per_wall_hour"]:.1f} h/h',
                                 ratio=1, overflow='ellipsis', no_wrap=True)
                rows = []
                for worker in s['workers']:
                    if worker['host'] != host:
                        continue
                    number = int(worker['name'].rsplit('-', 1)[1]) + 1
                    phase = worker['phase'] or 'starting'
                    if worker['job']:
                        channel = f' CH{worker["channel"]:02d}' if worker['channel'] else ''
                        detail = f'{worker["job"]}  {phase}{channel}  {worker["heartbeat_age"]:.0f}s'
                    else:
                        detail = 'idle' if worker['alive'] else 'exited'
                    rows.append(f'{number:>2}  {detail}')
                columns.append(rows)
            for pair in zip_longest(*columns, fillvalue=''):
                hosts.add_row(*pair)
            items.append(hosts)
            if s['failures']:
                items.append(Text(s['failures'][-1]['message'], style='bold red'))
        items.append(Text(f'Elapsed {duration(time.monotonic()-self.started)}    Ctrl+C drains active blocks', style='dim'))
        return Panel(Group(*items), title='Apollo 15  ·  Stage 05 flutter detection', border_style='blue')
