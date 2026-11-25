"""Terminal progress for the stage 05b archive render."""
from datetime import timedelta
from threading import Lock
from time import monotonic

from rich.console import Group
from rich.panel import Panel
from rich.table import Table


class RenderMonitor:
    def __init__(self, sizes, local, genus):
        self.sizes = sizes  # (tape, channel) -> source frames
        self.slots = {(host, n): 'idle' for host, count in [('Proprius', local), ('Genus', genus)]
                      for n in range(1, count+1)}
        self.rendered = set()
        self.reused = set()
        self.published = set()
        self.previously_published = set()
        self.host_frames = {'Proprius': 0, 'Genus': 0}
        self.fractions = {}
        self.io = 'Preparing'
        self.started = monotonic()
        self.lock = Lock()

    def transfer(self, message):
        with self.lock:
            self.io = message

    def show_worker(self, slot, key=None, message='idle'):
        with self.lock:
            self.slots[slot] = f'{key[0]} CH{key[1]:02d}  {message}' if key else message
            if key and message.startswith('CH') and message.endswith('%'):
                self.fractions[key] = float(message.split()[-1][:-1])/100

    def finished(self, key, *, host=None, reused=False, published=False):
        with self.lock:
            self.rendered.add(key)
            self.fractions.pop(key, None)
            if reused:
                self.reused.add(key)
            if published:
                self.published.add(key)
                if reused:
                    self.previously_published.add(key)
            if host:
                self.host_frames[host] += self.sizes[key]

    def __rich__(self):
        with self.lock:
            elapsed = monotonic()-self.started
            done = sum(self.sizes[k] for k in self.rendered)
            active = sum(self.sizes[k]*v for k, v in self.fractions.items())
            session = done + active - sum(self.sizes[k] for k in self.reused)
            rate = session/max(1, elapsed)
            remaining = max(0, sum(self.sizes.values())-done-active)
            eta = str(timedelta(seconds=round(remaining/rate))) if rate else 'measuring'
            committed = sum(self.sizes[k] for k in self.published)
            new_frames = committed-sum(self.sizes[k] for k in self.previously_published)
            archive_eta = (str(timedelta(seconds=round((sum(self.sizes.values())-committed)*elapsed/new_frames)))
                           if new_frames else 'measuring')
            table = Table(expand=True, box=None, pad_edge=False)
            for host in ('Proprius', 'Genus'):
                workers = [v for (h, _), v in self.slots.items() if h == host]
                speed = self.host_frames[host]/max(1, elapsed)/8000
                table.add_column(f'{host}   {sum(v != "idle" for v in workers)}/{len(workers)} busy   {speed:.0f} h/h')
            for n in range(1, max(n for _, n in self.slots)+1):
                table.add_row(*(f'{n:2}  {self.slots[h,n]}' if (h,n) in self.slots else ''
                                for h in ('Proprius', 'Genus')))
            status = (f'{len(self.rendered)}/{len(self.sizes)} channels rendered   '
                      f'{len(self.published)} verified on D:   {rate/8000:.0f} audio h/h   '
                      f'Render ETA {eta}   Archive ETA {archive_eta}   Elapsed {timedelta(seconds=int(elapsed))}')
            return Panel(Group(status, self.io, table), title='Apollo 15 · Stage 05b de-flutter')
