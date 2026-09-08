"""Windows Common Item Dialog folder picker without helper processes."""
# 文件职责：负责操作系统原生能力中的 folder_picker 子模块。
# 逻辑关系：上层通过 native/folder_picker.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from collections.abc import Callable
from typing import Any


# 类职责：定义 _Guid 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class _Guid(ctypes.Structure):
    # 变量说明：_fields_ 表示当前步骤使用的 _fields_ 值。
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    # 函数职责：完成 parse 对应的业务处理。
    # 参数关系：value 表示当前字段或计算值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def parse(cls, value: str) -> "_Guid":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


# 变量说明：_CLSID_FILE_OPEN_DIALOG 表示当前步骤使用的 _CLSID_FILE_OPEN_DIALOG 值。
_CLSID_FILE_OPEN_DIALOG = _Guid.parse("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
# 变量说明：_IID_FILE_OPEN_DIALOG 表示当前步骤使用的 _IID_FILE_OPEN_DIALOG 值。
_IID_FILE_OPEN_DIALOG = _Guid.parse("D57C7288-D4AD-4768-BE02-9D969532D960")

# 变量说明：_CLSCTX_INPROC_SERVER 表示当前步骤使用的 _CLSCTX_INPROC_SERVER 值。
_CLSCTX_INPROC_SERVER = 0x1
# 变量说明：_COINIT_APARTMENTTHREADED 表示当前步骤使用的 _COINIT_APARTMENTTHREADED 值。
_COINIT_APARTMENTTHREADED = 0x2
# 变量说明：_COINIT_DISABLE_OLE1DDE 表示当前步骤使用的 _COINIT_DISABLE_OLE1DDE 值。
_COINIT_DISABLE_OLE1DDE = 0x4
# 变量说明：_ERROR_CANCELLED_HRESULT 表示当前步骤使用的 _ERROR_CANCELLED_HRESULT 值。
_ERROR_CANCELLED_HRESULT = ctypes.c_int32(0x800704C7).value

# 变量说明：_FOS_NOCHANGEDIR 表示当前步骤使用的 _FOS_NOCHANGEDIR 值。
_FOS_NOCHANGEDIR = 0x00000008
# 变量说明：_FOS_PICKFOLDERS 表示当前流程使用的 _FOS_PICKFOLDERS 集合。
_FOS_PICKFOLDERS = 0x00000020
# 变量说明：_FOS_FORCEFILESYSTEM 表示当前步骤使用的 _FOS_FORCEFILESYSTEM 值。
_FOS_FORCEFILESYSTEM = 0x00000040
# 变量说明：_FOS_PATHMUSTEXIST 表示当前步骤使用的 _FOS_PATHMUSTEXIST 值。
_FOS_PATHMUSTEXIST = 0x00000800
# 变量说明：_SIGDN_FILESYSPATH 表示当前步骤使用的 _SIGDN_FILESYSPATH 值。
_SIGDN_FILESYSPATH = 0x80058000


# 函数职责：完成 raise_for_hresult 对应的业务处理。
# 参数关系：result 表示本步骤产生的结果；operation 表示当前步骤使用的 operation 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _raise_for_hresult(result: int, operation: str) -> None:
    if result < 0:
        raise OSError(f"{operation} failed with HRESULT 0x{result & 0xFFFFFFFF:08X}")


# 函数职责：完成 method 对应的业务处理。
# 参数关系：instance 表示当前步骤使用的 instance 值；index 表示当前元素的位置索引；result_type 表示当前步骤使用的 result_type 值；argument_types 表示当前流程使用的 argument_types 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _method(
    instance: ctypes.c_void_p,
    index: int,
    result_type: Any,
    *argument_types: Any,
) -> Callable[..., Any]:
    # 变量说明：vtable 表示当前步骤使用的 vtable 值。
    vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    # 变量说明：prototype 表示当前步骤使用的 prototype 值。
    prototype = ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *argument_types)
    return prototype(vtable[index])


# 函数职责：完成 release 对应的业务处理。
# 参数关系：instance 表示当前步骤使用的 instance 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _release(instance: ctypes.c_void_p | None) -> None:
    if instance and instance.value:
        # 变量说明：release 表示当前步骤使用的 release 值。
        release = _method(instance, 2, ctypes.c_ulong)
        release(instance)


# 函数职责：完成 show_windows_dialog 对应的业务处理。
# 参数关系：title 表示当前步骤使用的 title 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _show_windows_dialog(title: str) -> str | None:
    # 变量说明：ole32 表示当前步骤使用的 ole32 值。
    ole32 = ctypes.OleDLL("ole32")
    # 变量说明：argtypes 表示当前流程使用的 argtypes 集合。
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    # 变量说明：restype 表示当前步骤使用的 restype 值。
    ole32.CoInitializeEx.restype = ctypes.c_long
    # 变量说明：argtypes 表示当前流程使用的 argtypes 集合。
    ole32.CoUninitialize.argtypes = []
    # 变量说明：restype 表示当前步骤使用的 restype 值。
    ole32.CoUninitialize.restype = None
    # 变量说明：argtypes 表示当前流程使用的 argtypes 集合。
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_Guid),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_Guid),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    # 变量说明：restype 表示当前步骤使用的 restype 值。
    ole32.CoCreateInstance.restype = ctypes.c_long
    # 变量说明：argtypes 表示当前流程使用的 argtypes 集合。
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    # 变量说明：restype 表示当前步骤使用的 restype 值。
    ole32.CoTaskMemFree.restype = None

    # 变量说明：initialized 表示当前步骤使用的 initialized 值。
    initialized = ole32.CoInitializeEx(
        None,
        _COINIT_APARTMENTTHREADED | _COINIT_DISABLE_OLE1DDE,
    )
    _raise_for_hresult(initialized, "CoInitializeEx")

    # 变量说明：dialog 表示当前步骤使用的 dialog 值。
    dialog = ctypes.c_void_p()
    # 变量说明：shell_item 表示当前步骤使用的 shell_item 值。
    shell_item = ctypes.c_void_p()
    # 变量说明：path_pointer 表示当前步骤使用的 path_pointer 值。
    path_pointer = ctypes.c_void_p()
    try:
        # 变量说明：created 表示当前步骤使用的 created 值。
        created = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_FILE_OPEN_DIALOG),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_IID_FILE_OPEN_DIALOG),
            ctypes.byref(dialog),
        )
        _raise_for_hresult(created, "CoCreateInstance(CLSID_FileOpenDialog)")

        # 变量说明：options 表示当前流程使用的 options 集合。
        options = ctypes.c_uint32()
        # 变量说明：get_options 表示当前流程使用的 get_options 集合。
        get_options = _method(dialog, 10, ctypes.c_long, ctypes.POINTER(ctypes.c_uint32))
        _raise_for_hresult(get_options(dialog, ctypes.byref(options)), "IFileDialog.GetOptions")

        # 变量说明：set_options 表示当前流程使用的 set_options 集合。
        set_options = _method(dialog, 9, ctypes.c_long, ctypes.c_uint32)
        # 变量说明：selected_options 表示当前流程使用的 selected_options 集合。
        selected_options = (
            options.value
            | _FOS_NOCHANGEDIR
            | _FOS_PICKFOLDERS
            | _FOS_FORCEFILESYSTEM
            | _FOS_PATHMUSTEXIST
        )
        _raise_for_hresult(set_options(dialog, selected_options), "IFileDialog.SetOptions")

        # 变量说明：set_title 表示当前步骤使用的 set_title 值。
        set_title = _method(dialog, 17, ctypes.c_long, ctypes.c_wchar_p)
        _raise_for_hresult(set_title(dialog, title), "IFileDialog.SetTitle")

        # 变量说明：show 表示当前步骤使用的 show 值。
        show = _method(dialog, 3, ctypes.c_long, ctypes.c_void_p)
        # 变量说明：shown 表示当前步骤使用的 shown 值。
        shown = show(dialog, None)
        if shown == _ERROR_CANCELLED_HRESULT:
            return None
        _raise_for_hresult(shown, "IFileDialog.Show")

        # 变量说明：get_result 表示当前步骤使用的 get_result 值。
        get_result = _method(dialog, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
        _raise_for_hresult(get_result(dialog, ctypes.byref(shell_item)), "IFileDialog.GetResult")

        # 变量说明：get_display_name 表示当前步骤使用的 get_display_name 值。
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


# 函数职责：完成 show_folder_picker 对应的业务处理。
# 参数关系：title 表示当前步骤使用的 title 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def show_folder_picker(title: str) -> str | None:
    """Show the Windows folder picker on a dedicated STA thread."""

    if os.name != "nt":
        raise OSError("Native folder selection is available only on Windows")

    # 变量说明：selected 表示当前步骤使用的 selected 值。
    selected: list[str | None] = []
    # 变量说明：failures 表示当前流程使用的 failures 集合。
    failures: list[BaseException] = []

    # 函数职责：完成 run 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def run() -> None:
        try:
            selected.append(_show_windows_dialog(title))
        except BaseException as exc:
            failures.append(exc)

    # 变量说明：thread 表示当前步骤使用的 thread 值。
    thread = threading.Thread(target=run, name="pgagent-folder-picker")
    thread.start()
    thread.join()

    if failures:
        raise failures[0]
    return selected[0]
