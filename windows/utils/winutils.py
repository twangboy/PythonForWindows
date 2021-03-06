import ctypes
import msvcrt
import os
import sys
import code
import datetime
from collections import namedtuple

import windows
from windows.dbgprint import dbgprint
from .. import winproxy
from ..generated_def import windef
from ..generated_def.winstructs import *


# Function resolution !
# should be in winproxy ?
def get_func_addr(dll_name, func_name):
        # Load the DLL
        ctypes.WinDLL(dll_name)
        modules = windows.current_process.peb.modules
        if not dll_name.lower().endswith(".dll"):
            dll_name += ".dll"
        mod = [x for x in modules if x.name == dll_name][0]
        return mod.pe.exports[func_name]


def get_remote_func_addr(target, dll_name, func_name):
        name_modules = [m for m in target.peb.modules if m.name == dll_name]
        if not len(name_modules):
            raise ValueError("Module <{0}> not loaded in target <{1}>".format(dll_name, target))
        mod = name_modules[0]
        return mod.pe.exports[func_name]


def is_wow_64(hProcess):
    try:
        fnIsWow64Process = get_func_addr("kernel32.dll", "IsWow64Process")
    except winproxy.Kernel32Error:
        return False
    IsWow64Process = ctypes.WINFUNCTYPE(BOOL, HANDLE, ctypes.POINTER(BOOL))(fnIsWow64Process)
    Wow64Process = BOOL()
    res = IsWow64Process(hProcess, ctypes.byref(Wow64Process))
    if res:
        return bool(Wow64Process)
    raise ctypes.WinError()


def create_file_from_handle(handle, mode="r"):
    """Return a Python :class:`file` around a ``Windows`` HANDLE"""
    fd = msvcrt.open_osfhandle(handle, os.O_TEXT)
    return os.fdopen(fd, mode, 0)


def get_handle_from_file(f):
    """Get the ``Windows`` HANDLE of a python :class:`file`"""
    return msvcrt.get_osfhandle(f.fileno())


def create_console():
    """Create a new console displaying STDOUT.
       Useful in injection of GUI process"""
    winproxy.AllocConsole()
    stdout_handle = winproxy.GetStdHandle(windef.STD_OUTPUT_HANDLE)
    console_stdout = create_file_from_handle(stdout_handle, "w")
    sys.stdout = console_stdout

    stdin_handle = winproxy.GetStdHandle(windef.STD_INPUT_HANDLE)
    console_stdin = create_file_from_handle(stdin_handle, "r+")
    sys.stdin = console_stdin

    stderr_handle = winproxy.GetStdHandle(windef.STD_ERROR_HANDLE)
    console_stderr = create_file_from_handle(stderr_handle, "w")
    sys.stderr = console_stderr


def create_process(path, args=None, dwCreationFlags=0, show_windows=True):
    """A convenient wrapper arround :func:`windows.winproxy.CreateProcessA`"""
    proc_info = PROCESS_INFORMATION()
    lpStartupInfo = None
    if show_windows:
        StartupInfo = STARTUPINFOA()
        StartupInfo.cb = ctypes.sizeof(StartupInfo)
        StartupInfo.dwFlags = 0
        lpStartupInfo = ctypes.byref(StartupInfo)
    lpCommandLine = None
    if args:
        lpCommandLine = (" ".join([str(a) for a in args]))
    windows.winproxy.CreateProcessA(path, lpCommandLine=lpCommandLine, dwCreationFlags=dwCreationFlags, lpProcessInformation=ctypes.byref(proc_info), lpStartupInfo=lpStartupInfo)
    dbgprint("CreateProcessA new process handle {:#x}".format(proc_info.hProcess), "HANDLE")
    dbgprint("CreateProcessA new thread handle {:#x}".format(proc_info.hThread), "HANDLE")
    dbgprint("Automatic close of thread handle {:#x}".format(proc_info.hThread), "HANDLE")
    windows.winproxy.CloseHandle(proc_info.hThread)  # Give access to a WinThread in addition of the WinProcess ?
    return windows.winobject.process.WinProcess(pid=proc_info.dwProcessId, handle=proc_info.hProcess)


def lookup_privilege_value(privilege_name):
    luid = LUID()
    winproxy.LookupPrivilegeValueA(None, privilege_name, byref(luid))
    return luid

def lookup_privilege_name(privilege_value):
    if isinstance(privilege_value, tuple):
        luid = LUID(privilege_value[1], privilege_value[0])
        privilege_value = luid
    size = DWORD(0x100)
    buff = ctypes.c_buffer(size.value)
    winproxy.LookupPrivilegeNameA(None, privilege_value, buff, size)
    return buff[:size.value]

def enable_privilege(lpszPrivilege, bEnablePrivilege):
    """
    Enable or disable a privilege::

        enable_privilege(SE_DEBUG_NAME, True)
    """
    tp = TOKEN_PRIVILEGES()
    luid = LUID()
    hToken = HANDLE()

    winproxy.OpenProcessToken(winproxy.GetCurrentProcess(), TOKEN_ALL_ACCESS, byref(hToken))
    winproxy.LookupPrivilegeValueA(None, lpszPrivilege, byref(luid))
    tp.PrivilegeCount = 1
    tp.Privileges[0].Luid = luid
    if bEnablePrivilege:
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
    else:
        tp.Privileges[0].Attributes = 0
    winproxy.AdjustTokenPrivileges(hToken, False, byref(tp), sizeof(TOKEN_PRIVILEGES))
    winproxy.CloseHandle(hToken)
    if winproxy.GetLastError() == windef.ERROR_NOT_ALL_ASSIGNED:
        raise ValueError("Failed to get privilege {0}".format(lpszPrivilege))
    return True


def check_is_elevated():
    """Return ``True`` if process is Admin"""
    hToken = HANDLE()
    elevation = TOKEN_ELEVATION()
    cbsize = DWORD()

    winproxy.OpenProcessToken(winproxy.GetCurrentProcess(), TOKEN_ALL_ACCESS, byref(hToken))
    winproxy.GetTokenInformation(hToken, TokenElevation, byref(elevation), sizeof(elevation), byref(cbsize))
    winproxy.CloseHandle(hToken)
    return elevation.TokenIsElevated


def check_debug():
    """Check that kernel is in debug mode (beware of NOUMEX):

       https://msdn.microsoft.com/en-us/library/windows/hardware/ff556253(v=vs.85).aspx#_______noumex______
    """
    hkresult = HKEY()
    cbsize = DWORD(1024)
    bufferres = (c_char * cbsize.value)()

    winproxy.RegOpenKeyExA(HKEY_LOCAL_MACHINE, "System\\CurrentControlSet\\Control", 0, KEY_READ, byref(hkresult))
    winproxy.RegGetValueA(hkresult, None, "SystemStartOptions", RRF_RT_REG_SZ, None, byref(bufferres), byref(cbsize))
    winproxy.RegCloseKey(hkresult)

    control = bufferres[:]
    if "DEBUG" not in control:
        # print "[-] Enable debug boot!"
        # print "> bcdedit /debug on"
        return False
    if "DEBUG=NOUMEX" not in control:
        pass
        # print "[*] Warning noumex not set!"
        # print "> bcdedit /set noumex on"
    return True


def datetime_from_filetime(filetime):
    """return a :class:`datetime.datetime` from a ``windows`` FILETIME int"""
    return datetime.datetime(1601,1,1) + datetime.timedelta(microseconds=filetime / 10)

def filetime_from_datetime(dtime):
    """Return the FILETIME value from a :class:`datetime.datetime` in a python :class:`int`"""
    return int((dtime - datetime.datetime(1601,1,1)).total_seconds() * 1000) * 10000


class FixedInteractiveConsole(code.InteractiveConsole):
    def raw_input(self, prompt=">>>"):
        sys.stdout.write(prompt)
        return raw_input("")


def pop_shell(locs=None):
    """Pop a console with an InterativeConsole"""
    if locs is None:
        locs = globals()
    create_console()
    FixedInteractiveConsole(locs).interact()

def get_kernel_modules():
    if windows.current_process.is_wow_64:
        return get_kernel_modules_syswow64()
    cbsize = DWORD()
    winproxy.NtQuerySystemInformation(SystemModuleInformation, None, 0, byref(cbsize))
    raw_buffer = (cbsize.value * c_char)()
    buffer = SYSTEM_MODULE_INFORMATION.from_address(ctypes.addressof(raw_buffer))
    winproxy.NtQuerySystemInformation(SystemModuleInformation, byref(raw_buffer), sizeof(raw_buffer), byref(cbsize))
    modules = (SYSTEM_MODULE * buffer.ModulesCount).from_address(addressof(buffer) + SYSTEM_MODULE_INFORMATION.Modules.offset)
    return list(modules)

def get_kernel_modules_syswow64():
    cbsize = DWORD()
    windows.syswow64.NtQuerySystemInformation_32_to_64(SystemModuleInformation, None, 0, ctypes.addressof(cbsize))
    raw_buffer = (cbsize.value * c_char)()
    buffer = SYSTEM_MODULE_INFORMATION64.from_address(ctypes.addressof(raw_buffer))
    windows.syswow64.NtQuerySystemInformation_32_to_64(SystemModuleInformation, byref(raw_buffer), sizeof(raw_buffer), byref(cbsize))
    modules = (SYSTEM_MODULE64 * buffer.ModulesCount).from_address(addressof(buffer) + SYSTEM_MODULE_INFORMATION64.Modules.offset)
    return list(modules)


# String stuff
def ntstatus(code):
    return windows.generated_def.ntstatus.NtStatusException(code)


def get_long_path(path):
    """Return the long path form for ``path``

        :returns: :class:`str`
    """
    size = 0x1000
    buffer = ctypes.c_buffer(size)
    rsize = winproxy.GetLongPathNameA(path, buffer, size)
    return buffer[:rsize]

def get_short_path(path):
    """Return the short path form for ``path``

        :returns: :class:`str`
    """
    size = 0x1000
    buffer = ctypes.c_buffer(size)
    rsize = winproxy.GetShortPathNameA(path, buffer, size)
    return buffer[:rsize]

def get_shared_mapping(name, size=0x1000):
    # TODO: real code
    h = windows.winproxy.CreateFileMappingA(INVALID_HANDLE_VALUE, dwMaximumSizeLow=size, lpName=name)
    addr = windows.winproxy.MapViewOfFile(h, dwNumberOfBytesToMap=size)
    return addr

#def mapfile(file):
#    fhandle = get_handle_from_file(file)
#    h = windows.winproxy.CreateFileMappingA(fhandle, None, PAGE_READONLY, 0, 1, None)
#    addr = windows.winproxy.MapViewOfFile(h, dwDesiredAccess=FILE_MAP_READ, dwNumberOfBytesToMap=1)
#    return addr

def decompress_buffer(comptype, buffer, uncompress_size=None):
    if uncompress_size is None:
        uncompress_size = len(buffer) * 10
    result_size = DWORD()
    uncompressed = ctypes.c_buffer(uncompress_size)
    windows.winproxy.RtlDecompressBuffer(comptype, uncompressed, uncompress_size, buffer, len(buffer), result_size)
    return uncompressed[:result_size.value]


# sid.py + real SID type ?

def get_known_sid(sid_type):
    size = DWORD()
    try:
        windows.winproxy.CreateWellKnownSid(sid_type, None, None, size)
    except WindowsError:
        pass
    buffer = ctypes.c_buffer(size.value)
    windows.winproxy.CreateWellKnownSid(sid_type, None, buffer, size)
    return ctypes.cast(buffer, PSID)

UnloadEventTraceInfo = namedtuple("UnloadEventTraceInfo", ["size", "nb_elt", "array_ptr"])

def get_unload_event_trace():
    x = PULONG()
    y = PULONG()
    z = PVOID()
    windows.winproxy.RtlGetUnloadEventTraceEx(x, y, z)
    return UnloadEventTraceInfo(x[0], y[0], z.value)

class VirtualProtected(object):
    """
    A context manager usable like `VirtualProtect` that will restore the old protection at exit ::

        with utils.VirtualProtected(IATentry.addr, ctypes.sizeof(PVOID), windef.PAGE_EXECUTE_READWRITE):
            IATentry.value = 0x42424242
    """
    def __init__(self, addr, size, new_protect):
        if (addr % 0x1000):
            addr = addr - addr % 0x1000
        self.addr = addr
        self.size = size
        self.new_protect = new_protect

    def __enter__(self):
        self.old_protect = DWORD()
        winproxy.VirtualProtect(self.addr, self.size, self.new_protect, ctypes.byref(self.old_protect))
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        winproxy.VirtualProtect(self.addr, self.size, self.old_protect.value, ctypes.byref(self.old_protect))
        return False


class DisableWow64FsRedirection(object):
    """
    A context manager that disable the SysWow64 Filesystem Redirection ::

        if is_process_32_bits:
            def pop_calc_64():
                with windows.utils.DisableWow64FsRedirection():
                    return windows.utils.create_process(r"C:\Windows\system32\calc.exe", True)
    """
    def __enter__(self):
        if windows.current_process.bitness == 64:
            return self
        self.OldValue = PVOID()
        winproxy.Wow64DisableWow64FsRedirection(ctypes.byref(self.OldValue))
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if windows.current_process.bitness == 64:
            return False
        winproxy.Wow64RevertWow64FsRedirection(self.OldValue)
        return False
