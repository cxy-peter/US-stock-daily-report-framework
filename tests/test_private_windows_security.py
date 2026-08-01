from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import serenity_monitor.private_windows_security as windows_security

from serenity_monitor.private_windows_security import (
    PrivateWindowsSecurityError,
    current_user_sid,
    read_owner_only_file,
    secure_create_owner_only_directory,
    tighten_and_verify_owner_only,
    tighten_owner_only,
    verify_owner_only_dacl,
    windows_security_available,
)


windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")


def _replace_dacl_with_everyone(path: Path) -> None:
    """Install one explicit Everyone full-control ACE for a negative test."""

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    size = wintypes.DWORD(0)
    dacl = ctypes.c_void_p()
    present = wintypes.BOOL()
    defaulted = wintypes.BOOL()
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    assert advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:P(A;;FA;;;WD)", 1, ctypes.byref(descriptor), ctypes.byref(size)
    )
    try:
        assert advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        )
        assert present and dacl
        mutable_path = ctypes.create_unicode_buffer(os.fspath(path))
        assert (
            advapi32.SetNamedSecurityInfoW(
                mutable_path,
                1,
                0x00000004 | 0x80000000,
                None,
                None,
                dacl,
                None,
            )
            == 0
        )
    finally:
        kernel32.LocalFree(descriptor)


@windows_only
def test_current_user_sid_is_canonical() -> None:
    assert windows_security_available() is True
    assert re.fullmatch(r"S-1-(?:\d+-)+\d+", current_user_sid())


@windows_only
def test_secure_directory_is_protected_owner_only_and_sidecar_inherits(tmp_path: Path) -> None:
    root = secure_create_owner_only_directory(tmp_path / "private")

    assert verify_owner_only_dacl(root) is True
    sidecar = root / "sidecar.sqlite3-wal"
    sidecar.write_bytes(b"private")
    assert verify_owner_only_dacl(sidecar) is True

    tighten_and_verify_owner_only(sidecar)
    assert verify_owner_only_dacl(sidecar, require_protected=True) is True


@windows_only
def test_owner_only_read_uses_one_handle_and_denies_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = secure_create_owner_only_directory(tmp_path / "private")
    source = root / "runtime.private.yaml"
    expected = b"schema_version: private_daily_runtime/v1.0.0\n"
    source.write_bytes(expected)
    tighten_owner_only(source)
    moved = root / "replaced.private.yaml"
    original_read = windows_security._read_exact_handle

    def attempt_replace_while_open(handle: object, size: int) -> bytes:
        with pytest.raises(PermissionError):
            os.replace(source, moved)
        return original_read(handle, size)

    monkeypatch.setattr(
        windows_security,
        "_read_exact_handle",
        attempt_replace_while_open,
    )

    assert read_owner_only_file(source, max_bytes=1024) == expected
    assert source.is_file()
    assert not moved.exists()


@windows_only
def test_owner_only_read_rejects_oversized_file_with_fixed_error(tmp_path: Path) -> None:
    root = secure_create_owner_only_directory(tmp_path / "private")
    source = root / "large.private.yaml"
    source.write_bytes(b"x" * 33)
    tighten_owner_only(source)

    with pytest.raises(PrivateWindowsSecurityError) as caught:
        read_owner_only_file(source, max_bytes=32)
    assert str(caught.value) == "windows_private_file_too_large"
    assert str(source) not in str(caught.value)


@windows_only
def test_owner_only_read_rejects_everyone_acl(tmp_path: Path) -> None:
    source = tmp_path / "broad.private.yaml"
    source.write_bytes(b"private")
    _replace_dacl_with_everyone(source)

    with pytest.raises(PrivateWindowsSecurityError) as caught:
        read_owner_only_file(source, max_bytes=1024)
    assert str(caught.value) == "windows_acl_not_owner_only"


@windows_only
def test_secure_recursive_creation_applies_acl_at_each_creation(tmp_path: Path) -> None:
    nested = secure_create_owner_only_directory(
        tmp_path / "private" / "reports" / "immutable",
        parents=True,
    )

    assert verify_owner_only_dacl(tmp_path / "private") is True
    assert verify_owner_only_dacl(tmp_path / "private" / "reports") is True
    assert verify_owner_only_dacl(nested) is True


@windows_only
def test_tighten_clears_broad_inherited_acl_on_existing_directory_and_file(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "initially-inherited"
    directory.mkdir()
    private_file = directory / "state.json"
    private_file.write_text("{}", encoding="utf-8")

    # The pytest temporary tree normally carries SYSTEM/Administrators/Users
    # inheritance.  Regardless of its exact host ACL, replacement must leave
    # one protected current-user ACE and clear Everyone or other inherited ACEs.
    tighten_owner_only(directory)
    tighten_owner_only(private_file)

    assert verify_owner_only_dacl(directory, require_protected=True) is True
    assert verify_owner_only_dacl(private_file, require_protected=True) is True


@windows_only
def test_existing_secure_directory_is_idempotent(tmp_path: Path) -> None:
    directory = secure_create_owner_only_directory(tmp_path / "private")

    assert secure_create_owner_only_directory(directory) == directory
    assert verify_owner_only_dacl(directory) is True


@windows_only
def test_relative_and_missing_paths_fail_with_fixed_codes(tmp_path: Path) -> None:
    with pytest.raises(PrivateWindowsSecurityError) as relative:
        verify_owner_only_dacl("private")
    assert str(relative.value) == "windows_private_path_not_absolute"

    missing = tmp_path / "sensitive-account-name" / "missing.json"
    with pytest.raises(PrivateWindowsSecurityError) as unavailable:
        verify_owner_only_dacl(missing)
    assert str(unavailable.value) == "windows_private_path_unavailable"
    assert "sensitive-account-name" not in str(unavailable.value)
    assert "WinError" not in str(unavailable.value)


@windows_only
def test_reparse_point_is_rejected_without_disclosing_path(tmp_path: Path) -> None:
    target = secure_create_owner_only_directory(tmp_path / "target")
    link = tmp_path / "private-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink creation is not available")

    with pytest.raises(PrivateWindowsSecurityError) as caught:
        verify_owner_only_dacl(link)
    assert str(caught.value) == "windows_private_reparse_forbidden"
    assert str(link) not in str(caught.value)


def test_non_windows_calls_are_explicitly_isolated(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("non-Windows isolation contract")

    assert windows_security_available() is False
    with pytest.raises(PrivateWindowsSecurityError) as caught:
        current_user_sid()
    assert str(caught.value) == "windows_security_unavailable"
    with pytest.raises(PrivateWindowsSecurityError) as create:
        secure_create_owner_only_directory(tmp_path / "private")
    assert str(create.value) == "windows_security_unavailable"
    with pytest.raises(PrivateWindowsSecurityError) as read:
        read_owner_only_file(tmp_path / "private", max_bytes=1)
    assert str(read.value) == "windows_security_unavailable"
