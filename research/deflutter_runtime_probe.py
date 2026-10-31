"""Read-only immediate Windows job limits; never alters limits or processes."""
import ctypes
from ctypes import wintypes
from deflutter_campaign import ROOT,write,stamp


class Basic(ctypes.Structure):
    _fields_=[('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),
        ('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),
        ('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),
        ('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]


class IO(ctypes.Structure):
    _fields_=[(k,ctypes.c_uint64) for k in ('ReadOperationCount','WriteOperationCount','OtherOperationCount',
                                        'ReadTransferCount','WriteTransferCount','OtherTransferCount')]


class Extended(ctypes.Structure):
    _fields_=[('BasicLimitInformation',Basic),('IoInfo',IO)]+[(k,ctypes.c_size_t) for k in
        ('ProcessMemoryLimit','JobMemoryLimit','PeakProcessMemoryUsed','PeakJobMemoryUsed')]


def run():
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    fn=k.QueryInformationJobObject
    fn.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    fn.restype=wintypes.BOOL
    data=Extended();returned=wintypes.DWORD()
    ok=bool(fn(None,9,ctypes.byref(data),ctypes.sizeof(data),ctypes.byref(returned)))
    result=dict(at=stamp(),read_only=True,scope='Immediate job of this diagnostic subprocess; outer jobs/host policies and prior lost workers may differ',
        success=ok,win32_error=None if ok else ctypes.get_last_error(),structure_bytes=ctypes.sizeof(data),returned_bytes=returned.value,
        documentation=['https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject',
            'https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information'])
    if ok:
        flags=data.BasicLimitInformation.LimitFlags
        result.update(limit_flags=hex(flags),process_memory_limit=data.ProcessMemoryLimit if flags&0x100 else None,
            job_memory_limit=data.JobMemoryLimit if flags&0x200 else None,
            process_user_time_limit_seconds=data.BasicLimitInformation.PerProcessUserTimeLimit/1e7 if flags&2 else None,
            job_user_time_limit_seconds=data.BasicLimitInformation.PerJobUserTimeLimit/1e7 if flags&4 else None,
            kill_on_job_handle_close=bool(flags&0x2000),peak_process_memory_used=data.PeakProcessMemoryUsed,
            peak_job_memory_used=data.PeakJobMemoryUsed)
    write(ROOT/'runtime_job_probe.json',result);print(result)


if __name__=='__main__':run()
