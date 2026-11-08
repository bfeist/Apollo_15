"""Run the frozen v2 rollout at below-normal Windows process priority."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import runpy


def main():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.SetPriorityClass.restype = wintypes.BOOL
    below_normal = 0x4000
    if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), below_normal):
        raise ctypes.WinError(ctypes.get_last_error())
    runpy.run_path(str(Path(__file__).with_name('denoise_v2_rollout.py')), run_name='__main__')


if __name__ == '__main__':
    main()
