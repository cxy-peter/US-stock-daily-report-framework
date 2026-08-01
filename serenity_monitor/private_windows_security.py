"""Owner-only Windows filesystem security without optional dependencies.

The private runtime must not rely on a cloud folder, Git ignore rules, or the
process umask to protect account data.  This module deliberately has a small
API and uses only documented Win32 security functions through :mod:`ctypes`.

All public failures contain a fixed code only.  Paths, SIDs, Win32 error text,
and exception details must never cross this boundary.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class PrivateWindowsSecurityError(RuntimeError):
    """A fail-closed Windows ACL error containing no private context."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().lower()
        if not _SAFE_ERROR_CODE.fullmatch(normalized):
            normalized = "windows_private_security_failed"
        self.code = normalized
        super().__init__(normalized)


def _fail(code: str) -> None:
    raise PrivateWindowsSecurityError(code) from None


def windows_security_available() -> bool:
    """Return whether the owner-only Windows implementation is available."""

    return os.name == "nt"


if os.name == "nt":  # pragma: win32 cover
    import ctypes
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _DWORD = wintypes.DWORD
    _LPVOID = wintypes.LPVOID
    _BOOL = wintypes.BOOL
    _HANDLE = wintypes.HANDLE
    _LPWSTR = wintypes.LPWSTR
    _WORD = wintypes.WORD
    _BYTE = wintypes.BYTE

    _INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    _FILE_SHARE_READ = 0x00000001
    _GENERIC_READ = 0x80000000
    _READ_CONTROL = 0x00020000
    _OPEN_EXISTING = 3
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PATH_NOT_FOUND = 3
    _ERROR_ALREADY_EXISTS = 183

    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SDDL_REVISION_1 = 1
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_DACL_PROTECTED = 0x1000
    _SE_DACL_PRESENT = 0x0004

    _ACCESS_ALLOWED_ACE_TYPE = 0x00
    _OBJECT_INHERIT_ACE = 0x01
    _CONTAINER_INHERIT_ACE = 0x02
    _INHERITED_ACE = 0x10
    _FILE_ALL_ACCESS = 0x001F01FF

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", _LPVOID), ("Attributes", _DWORD)]

    class _TOKEN_USER_VALUE(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", _DWORD),
            ("lpSecurityDescriptor", _LPVOID),
            ("bInheritHandle", _BOOL),
        ]

    class _ACL(ctypes.Structure):
        _fields_ = [
            ("AclRevision", _BYTE),
            ("Sbz1", _BYTE),
            ("AclSize", _WORD),
            ("AceCount", _WORD),
            ("Sbz2", _WORD),
        ]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", _BYTE), ("AceFlags", _BYTE), ("AceSize", _WORD)]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", _DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", _DWORD),
            ("nFileSizeHigh", _DWORD),
            ("nFileSizeLow", _DWORD),
            ("nNumberOfLinks", _DWORD),
            ("nFileIndexHigh", _DWORD),
            ("nFileIndexLow", _DWORD),
        ]

    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = _HANDLE
    _kernel32.CloseHandle.argtypes = [_HANDLE]
    _kernel32.CloseHandle.restype = _BOOL
    _kernel32.LocalFree.argtypes = [_LPVOID]
    _kernel32.LocalFree.restype = _LPVOID
    _kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetFileAttributesW.restype = _DWORD
    _kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SECURITY_ATTRIBUTES)]
    _kernel32.CreateDirectoryW.restype = _BOOL
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        _DWORD,
        _DWORD,
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        _DWORD,
        _DWORD,
        _HANDLE,
    ]
    _kernel32.CreateFileW.restype = _HANDLE
    _kernel32.GetFileInformationByHandle.argtypes = [
        _HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _kernel32.GetFileInformationByHandle.restype = _BOOL
    _kernel32.ReadFile.argtypes = [
        _HANDLE,
        _LPVOID,
        _DWORD,
        ctypes.POINTER(_DWORD),
        _LPVOID,
    ]
    _kernel32.ReadFile.restype = _BOOL

    _advapi32.OpenProcessToken.argtypes = [_HANDLE, _DWORD, ctypes.POINTER(_HANDLE)]
    _advapi32.OpenProcessToken.restype = _BOOL
    _advapi32.GetTokenInformation.argtypes = [
        _HANDLE,
        ctypes.c_int,
        _LPVOID,
        _DWORD,
        ctypes.POINTER(_DWORD),
    ]
    _advapi32.GetTokenInformation.restype = _BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [_LPVOID, ctypes.POINTER(_LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = _BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        _DWORD,
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_DWORD),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = _BOOL
    _advapi32.GetSecurityDescriptorOwner.argtypes = [
        _LPVOID,
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_BOOL),
    ]
    _advapi32.GetSecurityDescriptorOwner.restype = _BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        _LPVOID,
        ctypes.POINTER(_BOOL),
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = _BOOL
    _advapi32.SetNamedSecurityInfoW.argtypes = [
        _LPWSTR,
        _DWORD,
        _DWORD,
        _LPVOID,
        _LPVOID,
        _LPVOID,
        _LPVOID,
    ]
    _advapi32.SetNamedSecurityInfoW.restype = _DWORD
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        _LPWSTR,
        _DWORD,
        _DWORD,
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_LPVOID),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = _DWORD
    _advapi32.GetSecurityInfo.argtypes = [
        _HANDLE,
        _DWORD,
        _DWORD,
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_LPVOID),
        ctypes.POINTER(_LPVOID),
    ]
    _advapi32.GetSecurityInfo.restype = _DWORD
    _advapi32.GetSecurityDescriptorControl.argtypes = [
        _LPVOID,
        ctypes.POINTER(_WORD),
        ctypes.POINTER(_DWORD),
    ]
    _advapi32.GetSecurityDescriptorControl.restype = _BOOL
    _advapi32.IsValidSecurityDescriptor.argtypes = [_LPVOID]
    _advapi32.IsValidSecurityDescriptor.restype = _BOOL
    _advapi32.IsValidAcl.argtypes = [_LPVOID]
    _advapi32.IsValidAcl.restype = _BOOL
    _advapi32.IsValidSid.argtypes = [_LPVOID]
    _advapi32.IsValidSid.restype = _BOOL
    _advapi32.EqualSid.argtypes = [_LPVOID, _LPVOID]
    _advapi32.EqualSid.restype = _BOOL
    _advapi32.GetAce.argtypes = [_LPVOID, _DWORD, ctypes.POINTER(_LPVOID)]
    _advapi32.GetAce.restype = _BOOL


def _require_windows() -> None:
    if os.name != "nt":
        _fail("windows_security_unavailable")


def _absolute_local_path(path: str | Path) -> Path:
    try:
        supplied = Path(path)
    except (OSError, TypeError, ValueError):
        _fail("windows_private_path_invalid")
    if not supplied.is_absolute():
        _fail("windows_private_path_not_absolute")
    text = os.fspath(supplied)
    if not text or "\x00" in text or text.startswith(("\\\\", "//")):
        _fail("windows_private_path_invalid")
    try:
        return Path(os.path.abspath(text))
    except (OSError, ValueError):
        _fail("windows_private_path_invalid")


def _attributes(path: Path, *, missing_ok: bool = False) -> int | None:
    _require_windows()
    value = int(_kernel32.GetFileAttributesW(os.fspath(path)))
    if value != _INVALID_FILE_ATTRIBUTES:
        return value
    code = int(ctypes.get_last_error())
    if missing_ok and code in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
        return None
    _fail("windows_private_path_unavailable")


def _reject_reparse_chain(path: Path, *, missing_final_ok: bool = False) -> None:
    absolute = _absolute_local_path(path)
    anchor = Path(absolute.anchor)
    relative_parts = absolute.parts[1:]
    candidate = anchor
    missing_seen = False
    for index, part in enumerate(relative_parts):
        candidate = candidate / part
        is_final = index == len(relative_parts) - 1
        attributes = _attributes(candidate, missing_ok=missing_final_ok or missing_seen)
        if attributes is None:
            missing_seen = True
            if not missing_final_ok and is_final:
                _fail("windows_private_path_unavailable")
            continue
        if missing_seen:
            _fail("windows_private_path_race_detected")
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            _fail("windows_private_reparse_forbidden")


def _reject_file_hardlink(path: Path, attributes: int) -> None:
    if attributes & _FILE_ATTRIBUTE_DIRECTORY:
        return
    try:
        links = int(path.stat(follow_symlinks=False).st_nlink)
    except (OSError, ValueError):
        _fail("windows_private_path_unavailable")
    if links != 1:
        _fail("windows_private_hardlink_forbidden")


def _token_user_sid_pointer() -> tuple[object, int]:
    _require_windows()
    token = _HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        _fail("windows_current_user_sid_unavailable")
    try:
        required = _DWORD(0)
        _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        if int(required.value) <= 0:
            _fail("windows_current_user_sid_unavailable")
        buffer = ctypes.create_string_buffer(int(required.value))
        if not _advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            ctypes.byref(buffer),
            required,
            ctypes.byref(required),
        ):
            _fail("windows_current_user_sid_unavailable")
        user = ctypes.cast(ctypes.byref(buffer), ctypes.POINTER(_TOKEN_USER_VALUE)).contents
        sid = int(ctypes.cast(user.User.Sid, ctypes.c_void_p).value or 0)
        if not sid or not _advapi32.IsValidSid(ctypes.c_void_p(sid)):
            _fail("windows_current_user_sid_unavailable")
        return buffer, sid
    finally:
        _kernel32.CloseHandle(token)


def current_user_sid() -> str:
    """Return the current process-token user SID in canonical string form."""

    _require_windows()
    buffer, sid_value = _token_user_sid_pointer()
    sid_text = _LPWSTR()
    try:
        if not _advapi32.ConvertSidToStringSidW(
            ctypes.c_void_p(sid_value), ctypes.byref(sid_text)
        ):
            _fail("windows_current_user_sid_unavailable")
        value = ctypes.wstring_at(sid_text)
        if not value.startswith("S-1-") or len(value) > 184:
            _fail("windows_current_user_sid_unavailable")
        return value
    finally:
        # Keep the token buffer alive until the SID conversion has completed.
        del buffer
        if sid_text:
            _kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))


class _SecurityDescriptor:
    """LocalAlloc-backed owner-only security descriptor."""

    def __init__(self, *, directory: bool) -> None:
        _require_windows()
        inheritance = "OICI" if directory else ""
        sid = current_user_sid()
        sddl = f"O:{sid}D:P(A;{inheritance};FA;;;{sid})"
        self.pointer = _LPVOID()
        size = _DWORD(0)
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            _SDDL_REVISION_1,
            ctypes.byref(self.pointer),
            ctypes.byref(size),
        ):
            _fail("windows_security_descriptor_failed")
        if not self.pointer or not _advapi32.IsValidSecurityDescriptor(self.pointer):
            self.close()
            _fail("windows_security_descriptor_failed")

    def owner_and_dacl(self) -> tuple[object, object]:
        owner = _LPVOID()
        dacl = _LPVOID()
        defaulted = _BOOL()
        present = _BOOL()
        if not _advapi32.GetSecurityDescriptorOwner(
            self.pointer, ctypes.byref(owner), ctypes.byref(defaulted)
        ):
            _fail("windows_security_descriptor_failed")
        if not _advapi32.GetSecurityDescriptorDacl(
            self.pointer,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            _fail("windows_security_descriptor_failed")
        if not present or not dacl:
            _fail("windows_security_descriptor_failed")
        return owner, dacl

    def close(self) -> None:
        pointer = getattr(self, "pointer", None)
        if pointer:
            _kernel32.LocalFree(pointer)
            self.pointer = _LPVOID()

    def __enter__(self) -> "_SecurityDescriptor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _set_owner_only(path: Path, *, directory: bool) -> None:
    with _SecurityDescriptor(directory=directory) as descriptor:
        owner, dacl = descriptor.owner_and_dacl()
        mutable_path = ctypes.create_unicode_buffer(os.fspath(path))
        result = int(
            _advapi32.SetNamedSecurityInfoW(
                mutable_path,
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION
                | _DACL_SECURITY_INFORMATION
                | _PROTECTED_DACL_SECURITY_INFORMATION,
                owner,
                None,
                dacl,
                None,
            )
        )
        if result != 0:
            _fail("windows_acl_apply_failed")


def _create_one_directory(path: Path) -> None:
    with _SecurityDescriptor(directory=True) as descriptor:
        attributes = _SECURITY_ATTRIBUTES(
            ctypes.sizeof(_SECURITY_ATTRIBUTES), descriptor.pointer, False
        )
        if not _kernel32.CreateDirectoryW(os.fspath(path), ctypes.byref(attributes)):
            if int(ctypes.get_last_error()) != _ERROR_ALREADY_EXISTS:
                _fail("windows_private_directory_create_failed")
            existing = _attributes(path)
            if existing is None or not existing & _FILE_ATTRIBUTE_DIRECTORY:
                _fail("windows_private_directory_create_failed")


def secure_create_owner_only_directory(
    path: str | Path,
    *,
    parents: bool = False,
) -> Path:
    """Create and verify a protected owner-only directory.

    Each directory created by this call receives its restrictive descriptor at
    creation time, avoiding a broad inherited-DACL window.  Existing ancestor
    directories are never silently rewritten.  If ``parents`` is false, the
    direct parent must already exist.
    """

    _require_windows()
    absolute = _absolute_local_path(path)
    _reject_reparse_chain(absolute, missing_final_ok=True)
    existing = _attributes(absolute, missing_ok=True)
    if existing is not None:
        if not existing & _FILE_ATTRIBUTE_DIRECTORY:
            _fail("windows_private_directory_create_failed")
        tighten_owner_only(absolute)
        return absolute

    parent = absolute.parent
    parent_attributes = _attributes(parent, missing_ok=True)
    if parent_attributes is None and not parents:
        _fail("windows_private_parent_missing")
    if parent_attributes is not None and not parent_attributes & _FILE_ATTRIBUTE_DIRECTORY:
        _fail("windows_private_parent_invalid")

    if parents:
        missing: list[Path] = []
        candidate = absolute
        while _attributes(candidate, missing_ok=True) is None:
            missing.append(candidate)
            if candidate == candidate.parent:
                _fail("windows_private_parent_missing")
            candidate = candidate.parent
        ancestor_attributes = _attributes(candidate)
        if ancestor_attributes is None or not ancestor_attributes & _FILE_ATTRIBUTE_DIRECTORY:
            _fail("windows_private_parent_invalid")
        for directory in reversed(missing):
            _create_one_directory(directory)
            _reject_reparse_chain(directory)
            verify_owner_only_dacl(directory, require_protected=True)
    else:
        _create_one_directory(absolute)

    _reject_reparse_chain(absolute)
    tighten_owner_only(absolute)
    return absolute


def tighten_owner_only(path: str | Path) -> None:
    """Replace an existing file or directory DACL and verify the result."""

    _require_windows()
    absolute = _absolute_local_path(path)
    _reject_reparse_chain(absolute)
    attributes = _attributes(absolute)
    if attributes is None:
        _fail("windows_private_path_unavailable")
    _reject_file_hardlink(absolute, attributes)
    _set_owner_only(absolute, directory=bool(attributes & _FILE_ATTRIBUTE_DIRECTORY))
    verify_owner_only_dacl(absolute, require_protected=True)


def _read_security_descriptor(path: Path) -> tuple[object, object, object]:
    owner = _LPVOID()
    dacl = _LPVOID()
    descriptor = _LPVOID()
    mutable_path = ctypes.create_unicode_buffer(os.fspath(path))
    result = int(
        _advapi32.GetNamedSecurityInfoW(
            mutable_path,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
    )
    if result != 0 or not descriptor:
        if descriptor:
            _kernel32.LocalFree(descriptor)
        _fail("windows_acl_read_failed")
    if not _advapi32.IsValidSecurityDescriptor(descriptor):
        _kernel32.LocalFree(descriptor)
        _fail("windows_acl_not_owner_only")
    return descriptor, owner, dacl


def _current_sid_equal(candidate: object) -> bool:
    buffer, sid_value = _token_user_sid_pointer()
    try:
        return bool(
            candidate
            and _advapi32.IsValidSid(candidate)
            and _advapi32.EqualSid(candidate, ctypes.c_void_p(sid_value))
        )
    finally:
        del buffer


def _assert_owner_only_descriptor(
    descriptor: object,
    owner: object,
    dacl: object,
    *,
    directory: bool,
    require_protected: bool,
) -> bool:
    """Validate one descriptor and return whether its DACL is protected."""

    if not owner or not _current_sid_equal(owner):
        _fail("windows_acl_not_owner_only")
    if not dacl or not _advapi32.IsValidAcl(dacl):
        _fail("windows_acl_not_owner_only")

    control = _WORD(0)
    revision = _DWORD(0)
    if not _advapi32.GetSecurityDescriptorControl(
        descriptor, ctypes.byref(control), ctypes.byref(revision)
    ):
        _fail("windows_acl_read_failed")
    protected = bool(int(control.value) & _SE_DACL_PROTECTED)
    if not int(control.value) & _SE_DACL_PRESENT:
        _fail("windows_acl_not_owner_only")
    if require_protected and not protected:
        _fail("windows_acl_not_owner_only")

    acl = ctypes.cast(dacl, ctypes.POINTER(_ACL)).contents
    if int(acl.AceCount) != 1:
        _fail("windows_acl_not_owner_only")
    ace_pointer = _LPVOID()
    if not _advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)) or not ace_pointer:
        _fail("windows_acl_not_owner_only")
    header = ctypes.cast(ace_pointer, ctypes.POINTER(_ACE_HEADER)).contents
    if int(header.AceType) != _ACCESS_ALLOWED_ACE_TYPE or int(header.AceSize) < 12:
        _fail("windows_acl_not_owner_only")
    mask = ctypes.c_uint32.from_address(int(ace_pointer.value) + 4).value
    ace_sid = ctypes.c_void_p(int(ace_pointer.value) + 8)
    if mask != _FILE_ALL_ACCESS or not _current_sid_equal(ace_sid):
        _fail("windows_acl_not_owner_only")

    flags = int(header.AceFlags)
    if directory:
        if not protected or flags != (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE):
            _fail("windows_acl_not_owner_only")
    elif protected:
        if flags != 0:
            _fail("windows_acl_not_owner_only")
    elif flags != _INHERITED_ACE:
        _fail("windows_acl_not_owner_only")
    return protected


def verify_owner_only_dacl(
    path: str | Path,
    *,
    require_protected: bool | None = None,
) -> bool:
    """Fail unless ``path`` grants full control solely to the current user.

    Directories always require a protected DACL and one explicit inheritable
    ``OI|CI`` ACE.  A file may, by default, contain the single owner ACE it
    inherited from such a directory; callers can require an explicit protected
    file DACL with ``require_protected=True``.
    """

    _require_windows()
    absolute = _absolute_local_path(path)
    _reject_reparse_chain(absolute)
    attributes = _attributes(absolute)
    if attributes is None:
        _fail("windows_private_path_unavailable")
    directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
    _reject_file_hardlink(absolute, attributes)
    descriptor, owner, dacl = _read_security_descriptor(absolute)
    try:
        must_be_protected = directory if require_protected is None else bool(require_protected)
        protected = _assert_owner_only_descriptor(
            descriptor,
            owner,
            dacl,
            directory=directory,
            require_protected=must_be_protected,
        )
        if not directory and not protected:
            # A sidecar may safely inherit the sole owner ACE from the protected
            # parent.  It must be an effective inherited ACE, not a new grant.
            verify_owner_only_dacl(absolute.parent, require_protected=True)
        return True
    finally:
        _kernel32.LocalFree(descriptor)


def _open_private_read_handle(path: Path) -> object:
    handle = _kernel32.CreateFileW(
        os.fspath(path),
        _GENERIC_READ | _READ_CONTROL,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    value = int(handle or 0)
    if not value or value == _INVALID_HANDLE_VALUE:
        _fail("windows_private_file_open_failed")
    return handle


def _handle_file_information(handle: object) -> object:
    information = _BY_HANDLE_FILE_INFORMATION()
    if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        _fail("windows_private_file_inspection_failed")
    attributes = int(information.dwFileAttributes)
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        _fail("windows_private_reparse_forbidden")
    if attributes & _FILE_ATTRIBUTE_DIRECTORY:
        _fail("windows_private_file_required")
    if int(information.nNumberOfLinks) != 1:
        _fail("windows_private_hardlink_forbidden")
    return information


def _verify_owner_only_handle(handle: object) -> None:
    owner = _LPVOID()
    dacl = _LPVOID()
    descriptor = _LPVOID()
    result = int(
        _advapi32.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
    )
    if result != 0 or not descriptor:
        if descriptor:
            _kernel32.LocalFree(descriptor)
        _fail("windows_acl_read_failed")
    try:
        if not _advapi32.IsValidSecurityDescriptor(descriptor):
            _fail("windows_acl_not_owner_only")
        # A private input is made explicit and protected before use.  Requiring
        # that state here prevents a parent DACL update from changing access
        # while this handle is reading the file.
        _assert_owner_only_descriptor(
            descriptor,
            owner,
            dacl,
            directory=False,
            require_protected=True,
        )
    finally:
        _kernel32.LocalFree(descriptor)


def _read_exact_handle(handle: object, size: int) -> bytes:
    content = bytearray()
    remaining = size
    while remaining:
        request = min(remaining, 64 * 1024)
        buffer = ctypes.create_string_buffer(request)
        received = _DWORD(0)
        if not _kernel32.ReadFile(
            handle,
            buffer,
            request,
            ctypes.byref(received),
            None,
        ):
            _fail("windows_private_file_read_failed")
        count = int(received.value)
        if count <= 0 or count > request:
            _fail("windows_private_file_changed")
        content.extend(buffer.raw[:count])
        remaining -= count

    # Share-write is denied for the lifetime of the handle.  Still check EOF so
    # unusual filesystems cannot silently return a different byte sequence.
    probe = ctypes.create_string_buffer(1)
    received = _DWORD(0)
    if not _kernel32.ReadFile(handle, probe, 1, ctypes.byref(received), None):
        _fail("windows_private_file_read_failed")
    if int(received.value) != 0:
        _fail("windows_private_file_changed")
    return bytes(content)


def read_owner_only_file(path: str | Path, max_bytes: int) -> bytes:
    """Read one protected owner-only file from a single non-replaceable handle.

    The handle refuses write/delete sharing and opens a reparse point itself
    rather than following it.  File type, link count, DACL, size, and bytes are
    therefore checked while the same kernel object remains open.
    """

    _require_windows()
    if type(max_bytes) is not int or max_bytes < 0 or max_bytes > 0x7FFFFFFF:
        _fail("windows_private_read_limit_invalid")
    absolute = _absolute_local_path(path)
    _reject_reparse_chain(absolute)
    handle = _open_private_read_handle(absolute)
    try:
        information = _handle_file_information(handle)
        size = (int(information.nFileSizeHigh) << 32) | int(information.nFileSizeLow)
        if size > max_bytes:
            _fail("windows_private_file_too_large")
        _verify_owner_only_handle(handle)
        return _read_exact_handle(handle, size)
    except PrivateWindowsSecurityError:
        raise
    except Exception:
        _fail("windows_private_file_read_failed")
    finally:
        _kernel32.CloseHandle(handle)


def tighten_and_verify_owner_only(path: str | Path) -> None:
    """Convenience wrapper used after creating a private runtime sidecar."""

    tighten_owner_only(path)


__all__ = [
    "PrivateWindowsSecurityError",
    "current_user_sid",
    "read_owner_only_file",
    "secure_create_owner_only_directory",
    "tighten_and_verify_owner_only",
    "tighten_owner_only",
    "verify_owner_only_dacl",
    "windows_security_available",
]
