"""Dispatch ready channels across two resident tapes using one disk-transfer lane."""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def run_queue(tapes, slots, stage, render, publish, stop):
    """Stage returns (all tape records, pending channels). Publish only a finished tape."""
    upcoming = deque(tapes)
    free = deque(slots)
    channels = deque()
    finished_tapes = deque()
    records = {}
    outstanding = {}
    running = {}
    transfer = None
    with ThreadPoolExecutor(max_workers=1) as disk, ThreadPoolExecutor(max_workers=len(free)) as workers:
        try:
            while not stop.is_set():
                if transfer is None:
                    # Keep a second tape available when possible. Publication frees its
                    # slot only after every channel is verified and scratch is released.
                    if upcoming and len(records) < 2:
                        tape = upcoming.popleft()
                        transfer = disk.submit(stage, tape)
                        running[transfer] = ('stage', tape, None)
                    elif finished_tapes:
                        tape = finished_tapes.popleft()
                        transfer = disk.submit(publish, tape, records[tape])
                        running[transfer] = ('publish', tape, None)

                while channels and free and not stop.is_set():
                    tape, channel = channels.popleft()
                    slot = free.popleft()
                    future = workers.submit(render, slot, tape, channel)
                    running[future] = ('render', tape, slot)
                if not running:
                    break

                completed, _ = wait(running, timeout=1, return_when=FIRST_COMPLETED)
                for future in completed:
                    operation, tape, slot = running.pop(future)
                    result = future.result()
                    if future is transfer:
                        transfer = None
                    if operation == 'stage':
                        records[tape], pending = result
                        outstanding[tape] = len(pending)
                        channels.extend((tape, channel) for channel in pending)
                        if not pending:
                            finished_tapes.append(tape)
                    elif operation == 'render':
                        free.append(slot)
                        outstanding[tape] -= 1
                        if outstanding[tape] == 0:
                            finished_tapes.append(tape)
                    else:
                        del records[tape], outstanding[tape]
            # On Ctrl+C active channels and transfers run to their verified checkpoints.
            for future in running:
                future.result()
        except BaseException:
            stop.set()
            raise
