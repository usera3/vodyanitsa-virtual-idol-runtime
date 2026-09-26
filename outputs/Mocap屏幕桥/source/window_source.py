"""Windows Graphics Capture by stable HWND, never falls back to desktop pixels."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
import threading
import time
from PIL import Image
import numpy as np
from windows_capture import WindowsCapture

user32=ctypes.WinDLL('user32',use_last_error=True)
kernel32=ctypes.WinDLL('kernel32',use_last_error=True)
CALLBACK=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
user32.EnumWindows.argtypes=[CALLBACK,wintypes.LPARAM]
user32.IsWindow.argtypes=[wintypes.HWND]
user32.IsWindowVisible.argtypes=[wintypes.HWND]
user32.IsIconic.argtypes=[wintypes.HWND]
user32.GetWindowTextLengthW.argtypes=[wintypes.HWND]
user32.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
user32.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
user32.GetAncestor.restype=wintypes.HWND
kernel32.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
kernel32.OpenProcess.restype=wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes=[wintypes.HANDLE]

def window_pid(hwnd):
    value=wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd,ctypes.byref(value))
    return value.value

def window_title(hwnd):
    n=user32.GetWindowTextLengthW(hwnd)
    buffer=ctypes.create_unicode_buffer(n+1)
    user32.GetWindowTextW(hwnd,buffer,n+1)
    return buffer.value

def top_hwnd(widget):
    return int(user32.GetAncestor(widget.winfo_id(),2))

def process_name(pid):
    handle=kernel32.OpenProcess(0x1000,False,pid)
    if not handle:return f'PID {pid}'
    try:
        buffer=ctypes.create_unicode_buffer(32768);length=wintypes.DWORD(32768)
        if kernel32.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(length)):
            return os.path.basename(buffer.value)
        return f'PID {pid}'
    finally:kernel32.CloseHandle(handle)

@dataclass(frozen=True)
class WindowTarget:
    hwnd:int
    pid:int
    title:str
    executable:str

    @property
    def alive(self):
        return bool(user32.IsWindow(self.hwnd)) and window_pid(self.hwnd)==self.pid

    @property
    def minimized(self):
        return bool(user32.IsIconic(self.hwnd))

def list_windows(include_self=False):
    result=[]
    @CALLBACK
    def visit(hwnd,param):
        if not user32.IsWindowVisible(hwnd):return True
        pid=window_pid(hwnd)
        if not include_self and pid==os.getpid():return True
        title=window_title(hwnd).strip()
        if not title or title=='Program Manager':return True
        # Ignore windows cloaked by virtual desktops / shell infrastructure.
        cloaked=wintypes.DWORD()
        try:
            ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd),14,ctypes.byref(cloaked),ctypes.sizeof(cloaked))
            if cloaked.value:return True
        except OSError:pass
        result.append(WindowTarget(int(hwnd),pid,title,process_name(pid)))
        return True
    user32.EnumWindows(visit,0)
    return sorted(result,key=lambda w:(w.executable.lower(),w.title.lower(),w.hwnd))

def crop_image(image, crop):
    if crop is None:return image
    left,top,right,bottom=crop
    if not (0<=left<right<=1 and 0<=top<bottom<=1):raise ValueError('窗口裁剪范围无效，请重新裁剪。')
    w,h=image.size
    box=(int(left*w),int(top*h),max(int(left*w)+1,int(right*w)),max(int(top*h)+1,int(bottom*h)))
    return image.crop(box)

class WindowFrames:
    def __init__(self,target,fps=30):
        self.target=target;self.fps=fps;self.control=None;self.capture=None
        self.lock=threading.Lock();self.latest=None;self.last_time=0;self.frames=0
        self.closed=threading.Event();self.error=None;self.started=0;self.resume_after=0
        self.was_minimized=False

    def start(self):
        if not self.target.alive:raise RuntimeError('目标窗口已关闭，请重新选择。')
        self.started=time.perf_counter()
        if not self.target.minimized:self._start_native()

    def _start_native(self):
        self.started=time.perf_counter()
        self.capture=WindowsCapture(cursor_capture=False,draw_border=True,
            minimum_update_interval=max(1,int(1000/self.fps)),window_hwnd=self.target.hwnd)
        @self.capture.event
        def on_frame_arrived(frame,control):
            try:
                # Native mapped memory expires with the callback; retain our own RGB copy.
                rgb=np.ascontiguousarray(frame.frame_buffer[:,:,:3][:,:,::-1])
                image=Image.fromarray(rgb)
                with self.lock:
                    self.latest=image;self.last_time=time.perf_counter();self.frames+=1
            except Exception as exc:
                self.error=str(exc);control.stop()
        @self.capture.event
        def on_closed():self.closed.set()
        self.control=self.capture.start_free_threaded()

    def read(self):
        if not self.target.alive or self.closed.is_set():
            raise RuntimeError('目标窗口已关闭或停止提供画面，请重新选择窗口。')
        if self.error:raise RuntimeError(self.error)
        if self.target.minimized:
            self.was_minimized=True
            return None,'目标窗口已最小化 · 恢复窗口后自动继续'
        if not user32.IsWindowVisible(self.target.hwnd):
            self.was_minimized=True
            return None,'目标窗口已隐藏 · 恢复窗口后自动继续'
        if self.control is None:self._start_native()
        if self.was_minimized:
            self.resume_after=time.perf_counter();self.was_minimized=False
        with self.lock:image,last=self.latest,self.last_time
        if image is None or last<self.resume_after:
            if image is None and time.perf_counter()-self.started>10:
                raise RuntimeError('窗口没有提供可捕获画面，请恢复窗口或换用其他应用。')
            return None,'等待目标窗口画面…'
        return image,''

    def close(self):
        if self.control:
            self.control.stop();self.control=None
        with self.lock:self.latest=None
