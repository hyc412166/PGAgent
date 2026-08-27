"""Windows Common Item Dialog folder picker without helper processes."""

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from collections.abc import Callable
from typing import Any


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> "_Guid":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


_CLSID_FILE_OPEN_DIALOG = _Guid.parse("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
_IID_FILE_OPEN_DIALOG = _Guid.parse("D57C7288-D4AD-4768-BE02-9D969532D960")

_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
_COINIT_DISABLE_OLE1DDE = 0x4
_ERROR_CANCELLED_HRESULT = ctypes.c_int32(0x800704C7).value

_FOS_NOCHANGEDIR = 0x00000008
_FOS_PICKFOLDERS = 0x00000020
_FOS_FORCEFILESYSTEM = 0x00000040
_FOS_PATHMUSTEXIST = 0x00000800
_SIGDN_FILESYSPATH = 0x80058000


def _raise_for_hresult(result: int, operation: str) -> None:
    if result < 0:
        raise OSError(f"{operation} failed with HRESULT 0x{result & 0xFFFFFFFF:08X}")


def _method(
    instance: ctypes.c_void_p,
    index: int,
    result_type: Any,
    *argument_types: Any,
) -> Callable[..., Any]:
    vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *argument_types)
    return prototype(vtable[index])


def _release(instance: ctypes.c_void_p | None) -> None:
    if instance and instance.value:
        release = _method(instance, 2, ctypes.c_ulong)
        release(instance)


def _show_windows_dialog(title: str) -> str | None:
    ole32 = ctypes.OleDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_Guid),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_Guid),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None

    initialized = ole32.CoInitializeEx(
        None,
        _COINIT_APARTMENTTHREADED | _COINIT_DISABLE_OLE1DDE,
    )
    _raise_for_hresult(initialized, "CoInitializeEx")

    dialog = ctypes.c_void_p()
    shell_item = ctypes.c_void_p()
    path_pointer = ctypes.c_void_p()
    try:
        created = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_FILE_OPEN_DIALOG),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_IID_FILE_OPEN_DIALOG),
            ctypes.byref(dialog),
        )
        _raise_for_hresult(created, "CoCreateInstance(CLSID_FileOpenDialog)")

        options = ctypes.c_uint32()
        get_options = _method(dialog, 10, ctypes.c_long, ctypes.POINTER(ctypes.c_uint32))
        _raise_for_hresult(get_options(dialog, ctypes.byref(options)), "IFileDialog.GetOptions")

        set_options = _method(dialog, 9, ctypes.c_long, ctypes.c_uint32)
        selected_options = (
            options.value
            | _FOS_NOCHANGEDIR
            | _FOS_PICKFOLDERS
            | _FOS_FORCEFILESYSTEM
            | _FOS_PATHMUSTEXIST
        )
        _raise_for_hresult(set_options(dialog, selected_options), "IFileDialog.SetOptions")

        set_title = _method(dialog, 17, ctypes.c_long, ctypes.c_wchar_p)
        _raise_for_hresult(set_title(dialog, title), "IFileDialog.SetTitle")

        show = _method(dialog, 3, ctypes.c_long, ctypes.c_void_p)
        shown = show(dialog, None)
        if shown == _ERROR_CANCELLED_HRESULT:
            return None
        _raise_for_hresult(shown, "IFileDialog.Show")

        get_result = _method(dialog, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
        _raise_for_hresult(get_result(dialog, ctypes.byref(shell_item)), "IFileDialog.GetResult")

        get_display_name = _method(
            shell_item,
            5,
            ctypes.c_long,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        )
        _raise_for_hresult(
            get_display_name(shell_item, _SIGDN_FILESYSPATH, ctypes.byref(path_pointer)),
            "IShellItem.GetDisplayName",
        )
        return ctypes.wstring_at(path_pointer)
    finally:
        if path_pointer.value:
            ole32.CoTaskMemFree(path_pointer)
        _release(shell_item)
        _release(dialog)
        ole32.CoUninitialize()


def show_folder_picker(title: str) -> str | None:
    """Show the Windows folder picker on a dedicated STA thread."""

    if os.name != "nt":
        raise OSError("Native folder selection is available only on Windows")

    selected: list[str | None] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            selected.append(_show_windows_dialog(title))
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run, name="pgagent-folder-picker")
    thread.start()
    thread.join()

    if failures:
        raise failures[0]
    return selected[0]
