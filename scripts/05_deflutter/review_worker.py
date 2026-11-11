"""One background review worker that cannot hold the CLI open after Ctrl+C.

ThreadPoolExecutor joins even running tasks at interpreter exit, regardless of
shutdown(wait=False). Review jobs may be hashing hours of audio at that point.
This daemon worker permits prompt exit; unfinished review caches remain unapproved.
"""
from queue import Queue, Empty
import threading


class ReviewWorker:
    def __init__(self):
        self.tasks = Queue()
        self.lock = threading.Lock()
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True, name='deflutter-review')
        self.thread.start()

    def _run(self):
        while True:
            task = self.tasks.get()
            if task is None:
                return
            task()

    def submit(self, task):
        with self.lock:
            if self.closed:
                raise RuntimeError('Review worker is shutting down')
            self.tasks.put(task)

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self.lock:
            if not self.closed:
                self.closed = True
                if cancel_futures:
                    while True:
                        try:
                            self.tasks.get_nowait()
                        except Empty:
                            break
                self.tasks.put(None)
        if wait:
            self.thread.join()
