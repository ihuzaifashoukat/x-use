"""Windows file DACLs using Win32 handles and standard-library ctypes.

State file DACLs permit the current user, SYSTEM and Administrators. SQLite
directories are verified without changing existing permissions; newly created
directories receive private inheritable permissions at creation. Existing files
must already belong to the user.
Previously granted handles and same-user malicious races are outside this
promise. Call through local_state.private_state_file for parent-path checks.
"""
import ctypes
from ctypes import wintypes as w
import os


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]


class _FileInfo(ctypes.Structure):
    _fields_ = [("attributes", w.DWORD), ("created", w.FILETIME),
                ("accessed", w.FILETIME), ("written", w.FILETIME),
                ("volume", w.DWORD), ("size_high", w.DWORD), ("size_low", w.DWORD),
                ("links", w.DWORD), ("index_high", w.DWORD), ("index_low", w.DWORD)]


class _Acl(ctypes.Structure):
    _fields_ = [("revision", w.BYTE), ("reserved", w.BYTE), ("size", w.WORD),
                ("count", w.WORD), ("reserved2", w.WORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", w.DWORD)]


class _Api:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.c_void_p
        out = ctypes.POINTER(pointer)
        self._bind(self.kernel, "GetCurrentProcess", [], w.HANDLE)
        self._bind(self.kernel, "CloseHandle", [w.HANDLE], w.BOOL)
        self._bind(self.kernel, "LocalFree", [pointer], pointer)
        self._bind(self.kernel, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD,
                   ctypes.POINTER(_SecurityAttributes), w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)
        self._bind(self.kernel, "CreateDirectoryW", [w.LPCWSTR, ctypes.POINTER(_SecurityAttributes)], w.BOOL)
        self._bind(self.kernel, "GetFileInformationByHandle", [w.HANDLE, ctypes.POINTER(_FileInfo)], w.BOOL)
        self._bind(self.security, "OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL)
        self._bind(self.security, "GetTokenInformation", [w.HANDLE, ctypes.c_int, pointer,
                   w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL)
        self._bind(self.security, "ConvertSidToStringSidW", [pointer, out], w.BOOL)
        self._bind(self.security, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                   [w.LPCWSTR, w.DWORD, out, ctypes.POINTER(w.DWORD)], w.BOOL)
        self._bind(self.security, "GetSecurityDescriptorOwner", [pointer, out, ctypes.POINTER(w.BOOL)], w.BOOL)
        self._bind(self.security, "GetSecurityDescriptorDacl",
                   [pointer, ctypes.POINTER(w.BOOL), out, ctypes.POINTER(w.BOOL)], w.BOOL)
        self._bind(self.security, "GetSecurityDescriptorControl",
                   [pointer, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)], w.BOOL)
        self._bind(self.security, "GetSecurityInfo", [w.HANDLE, ctypes.c_int, w.DWORD,
                   out, out, out, out, out], w.DWORD)
        self._bind(self.security, "SetSecurityInfo", [w.HANDLE, ctypes.c_int, w.DWORD,
                   pointer, pointer, pointer, pointer], w.DWORD)
        self._bind(self.security, "EqualSid", [pointer, pointer], w.BOOL)
        self._bind(self.security, "IsValidAcl", [pointer], w.BOOL)
        self._bind(self.security, "IsValidSid", [pointer], w.BOOL)
        self._bind(self.security, "GetAce", [pointer, w.DWORD, out], w.BOOL)

    @staticmethod
    def _bind(library, name, arguments, result):
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result

    @staticmethod
    def check(success):
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    def user_sid(self):
        token = w.HANDLE()
        self.check(self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)))
        try:
            needed = w.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if not needed.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(needed.value)
            self.check(self.security.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)))
            sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.sid
            result = ctypes.c_void_p()
            self.check(self.security.ConvertSidToStringSidW(sid, ctypes.byref(result)))
            try:
                return ctypes.wstring_at(result)
            finally:
                self.kernel.LocalFree(result)
        finally:
            self.kernel.CloseHandle(token)

    def descriptor(self, sddl):
        result = ctypes.c_void_p()
        self.check(self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(result), None))
        return result

    def acl_entries(self, acl):
        if not acl or not self.security.IsValidAcl(acl):
            raise OSError("Private state DACL is missing or invalid.")
        entries = []
        for index in range(ctypes.cast(acl, ctypes.POINTER(_Acl)).contents.count):
            ace = ctypes.c_void_p()
            self.check(self.security.GetAce(acl, index, ctypes.byref(ace)))
            size = ctypes.c_ushort.from_address(ace.value + 2).value
            entries.append(ctypes.string_at(ace, size))
        return sorted(entries)

    def sid_string(self, sid):
        result = ctypes.c_void_p()
        self.check(self.security.IsValidSid(sid))
        self.check(self.security.ConvertSidToStringSidW(sid, ctypes.byref(result)))
        try:
            return ctypes.wstring_at(result)
        finally:
            self.kernel.LocalFree(result)

    def verify_private_access(self, handle):
        """Inspect owner and every DACL ACE, accepting only private trustees.

        A null DACL allows everyone. Callback/object ACEs have different layouts
        and are refused rather than guessing which access they might grant.
        Deny ACEs cannot widen access, so ordinary deny entries are harmless.
        """
        descriptor, owner, acl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        error = self.security.GetSecurityInfo(handle, 1, 0x5,
            ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        try:
            allowed = {self.user_sid(), "S-1-5-18", "S-1-5-32-544"}
            if not owner.value or self.sid_string(owner) not in allowed:
                raise OSError("SQLite state directory or sidecar belongs to an unsupported owner.")
            for entry in self.acl_entries(acl):
                if len(entry) < 16 or entry[0] not in (0, 1):
                    raise OSError("SQLite state DACL contains an unsupported ACE.")
                # ACCESS_ALLOWED_ACE and ACCESS_DENIED_ACE contain a four-byte
                # header, four-byte mask, then a variable-length SID.
                if 16 + entry[9] * 4 != len(entry):
                    raise OSError("SQLite state DACL contains an invalid ACE.")
                if entry[0] == 1:
                    continue
                buffer = ctypes.create_string_buffer(entry)
                trustee = self.sid_string(ctypes.byref(buffer, 8))
                # OWNER RIGHTS applies only to the already verified object
                # owner; pytest's private Windows temp directories use it.
                if trustee not in allowed | {"S-1-3-0", "S-1-3-4"}:
                    raise OSError("SQLite state directory or sidecar grants access to other users.")
        finally:
            self.kernel.LocalFree(descriptor)

    def verify(self, handle, expected_owner, expected_acl, *, permissions=True):
        descriptor, owner, acl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        error = self.security.GetSecurityInfo(handle, 1, 0x5,
            ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        try:
            if not self.security.EqualSid(owner, expected_owner):
                raise OSError("Private state file belongs to another owner.")
            if permissions:
                control, revision = w.WORD(), w.DWORD()
                self.check(self.security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)))
                if not control.value & 0x1000 or self.acl_entries(acl) != self.acl_entries(expected_acl):
                    raise OSError("Private state file DACL could not be verified.")
        finally:
            self.kernel.LocalFree(descriptor)


def ensure_private_file(path):
    """Create or harden one ordinary, singly linked current-user state file."""
    api = _Api()
    sid = api.user_sid()
    descriptor = api.descriptor(f"O:{sid}D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)")
    handle = None
    try:
        owner, acl = ctypes.c_void_p(), ctypes.c_void_p()
        defaulted, present = w.BOOL(), w.BOOL()
        api.check(api.security.GetSecurityDescriptorOwner(descriptor, ctypes.byref(owner), ctypes.byref(defaulted)))
        api.check(api.security.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)))
        if not present.value or not acl.value:
            raise OSError("Private state DACL could not be constructed.")
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        # OPEN_ALWAYS never truncates. OPEN_REPARSE_POINT inspects the leaf
        # itself; metadata and the ACL are inspected/changed through this handle.
        handle = api.kernel.CreateFileW(str(path), 0x60080, 0x7,
            ctypes.byref(attributes), 4, 0x200080, None)
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        info = _FileInfo()
        api.check(api.kernel.GetFileInformationByHandle(handle, ctypes.byref(info)))
        if info.attributes & (0x400 | 0x10) or info.links != 1:
            raise OSError("Private state requires an ordinary file with one link.")
        api.verify(handle, owner, acl, permissions=False)
        error = api.security.SetSecurityInfo(handle, 1, 0x80000004, None, None, acl, None)
        if error:
            raise ctypes.WinError(error)
        api.verify(handle, owner, acl)
        current = os.stat(path, follow_symlinks=False)
        if current.st_ino != (info.index_high << 32 | info.index_low) or current.st_nlink != 1:
            raise OSError("Private state path changed during preparation.")
    finally:
        if handle is not None:
            api.kernel.CloseHandle(handle)
        api.kernel.LocalFree(descriptor)


def verify_private_sqlite_path(path, *, directory):
    """Verify an existing directory/sidecar through a handle; never change it."""
    api = _Api()
    handle = api.kernel.CreateFileW(str(path), 0x20080, 0x7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _FileInfo()
        api.check(api.kernel.GetFileInformationByHandle(handle, ctypes.byref(info)))
        if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
            raise OSError("SQLite state requires ordinary directories and files.")
        if not directory and info.links != 1:
            raise OSError("SQLite state sidecars must have one link.")
        api.verify_private_access(handle)
    finally:
        api.kernel.CloseHandle(handle)


def create_private_directory(path):
    """Atomically create a private inheritable directory, or verify a peer's."""
    api = _Api()
    sid = api.user_sid()
    descriptor = api.descriptor(f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        if not api.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error != 183:  # ERROR_ALREADY_EXISTS: inspect, never harden it.
                raise ctypes.WinError(error)
    finally:
        api.kernel.LocalFree(descriptor)
    verify_private_sqlite_path(path, directory=True)
