"""Run the frozen v2 rollout at below-normal Windows process priority."""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import ctypes
from ctypes import wintypes
from _bootstrap import command
import subprocess


def lower_priority():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.SetPriorityClass.restype = wintypes.BOOL
    below_normal = 0x4000
    if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), below_normal):
        raise ctypes.WinError(ctypes.get_last_error())


def main():
    lower_priority()
    import sys
    return subprocess.call(command('denoise_v2_rollout.py', *sys.argv[1:]))


if __name__ == '__main__':
    raise SystemExit(main())
