"""Process I/O counters for throughput estimates (logical OS reads, not disk misses)."""
import os


def read_bytes():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_operations', 'write_operations', 'other_operations',
                         'read_bytes', 'write_bytes', 'other_bytes')]
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.GetCurrentProcess.restype = wintypes.HANDLE
        api.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters)]
        counters = Counters()
        if api.GetProcessIoCounters(api.GetCurrentProcess(), ctypes.byref(counters)):
            return int(counters.read_bytes)
    else:
        try:
            with open('/proc/self/io', encoding='ascii') as stream:
                rows = dict(line.split(':', 1) for line in stream)
            return int(rows['rchar'])
        except (OSError, KeyError, ValueError):
            pass
    return None


def difference(before):
    after = read_bytes()
    return max(0, after-before) if before is not None and after is not None else None
