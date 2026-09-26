"""Local screen region -> OBS Virtual Camera. No network server or uploads."""
from __future__ import annotations

import ctypes
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

# Establish physical screen coordinates before creating Tk or screen capture.
if sys.platform == 'win32':
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

import tkinter as tk
from tkinter import ttk, messagebox
import mss
import numpy as np
from PIL import Image, ImageTk, ImageOps
import pyvirtualcam
from contextlib import ExitStack
from window_source import WindowTarget, WindowFrames, list_windows, crop_image

APP_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.getenv('LOCALAPPDATA', str(APP_DIR))) / 'MocapScreenBridge'
STATE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=STATE_DIR / 'app.log', level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s', encoding='utf8')
AGI_EXE = Path.home() / 'Downloads' / 'AGI_Mocap_终身测试' / 'AGI Mocap.exe'
BG, PANEL, TEXT, MUTED, ACCENT = '#0e1523', '#182235', '#edf4ff', '#9caec8', '#56e0bc'


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int

    def as_dict(self):
        return dict(left=self.left, top=self.top, width=self.width, height=self.height)

    def validate(self, bounds):
        if self.width < 24 or self.height < 24:
            raise ValueError('选区太小，请至少框选 24 × 24 像素。')
        if not (bounds['left'] <= self.left and bounds['top'] <= self.top
                and self.left + self.width <= bounds['left'] + bounds['width']
                and self.top + self.height <= bounds['top'] + bounds['height']):
            raise ValueError('屏幕布局已改变，请重新框选区域。')


def region_from_drag(x1, y1, x2, y2, bounds):
    x1, x2 = sorted((max(0, min(bounds['width'], x1)), max(0, min(bounds['width'], x2))))
    y1, y2 = sorted((max(0, min(bounds['height'], y1)), max(0, min(bounds['height'], y2))))
    region = Region(bounds['left'] + x1, bounds['top'] + y1, x2-x1, y2-y1)
    region.validate(bounds)
    return region


def fit_frame(image, size, mirror=False):
    if mirror:
        image = ImageOps.mirror(image)
    # Letterboxing never stretches a person's proportions or crops head/feet.
    image = ImageOps.contain(image, size, Image.Resampling.BILINEAR)
    frame = Image.new('RGB', size, '#000000')
    frame.paste(image, ((size[0]-image.width)//2, (size[1]-image.height)//2))
    return frame


class CaptureWorker:
    def __init__(self, region, size=(1280,720), fps=30, stream=False, mirror=False, window=None, crop=None):
        self.region, self.size, self.fps, self.stream, self.mirror = region, size, fps, stream, mirror
        self.window,self.crop=window,crop
        self.latest_source=None;self.source_frames=0
        self.stop_event = threading.Event()
        self.preview = queue.Queue(maxsize=1)
        self.events = queue.Queue()
        self.thread = threading.Thread(target=self.run, name='screen-capture', daemon=True)
        self.frames = 0
        self.actual_fps = 0.0
        self.device = ''

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        camera = None
        try:
            with ExitStack() as stack:
                source=None;source_status=None
                if self.window:
                    source=WindowFrames(self.window,self.fps)
                    source.start();stack.callback(source.close)
                else:
                    screen=stack.enter_context(mss.mss())
                    self.region.validate(screen.monitors[0])
                if self.stream:
                    camera = pyvirtualcam.Camera(width=self.size[0], height=self.size[1],
                        fps=self.fps, fmt=pyvirtualcam.PixelFormat.RGB, backend='obs')
                    self.device = camera.device
                self.events.put(('ready', self.device))
                begin = last_preview = time.perf_counter()
                deadline = begin
                while not self.stop_event.is_set():
                    tick = time.perf_counter()
                    if source:
                        img,status=source.read()
                        self.source_frames=source.frames
                        if status!=source_status:
                            self.events.put(('source_status',status));source_status=status
                        self.latest_source=img
                        img=crop_image(img,self.crop) if img is not None else Image.new('RGB',self.size)
                    else:
                        shot = screen.grab(self.region.as_dict())
                        img = Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')
                    output = fit_frame(img, self.size, self.mirror)
                    if camera:
                        camera.send(np.asarray(output, dtype=np.uint8))
                    self.frames += 1
                    self.actual_fps = self.frames / max(time.perf_counter() - begin, 0.001)
                    if tick-last_preview >= 0.09:
                        small = ImageOps.contain(output, (760,440), Image.Resampling.BILINEAR)
                        try:
                            self.preview.get_nowait()
                        except queue.Empty:
                            pass
                        self.preview.put_nowait(small)
                        last_preview = tick
                    # No accumulating frames: late frames are dropped instead of queued.
                    deadline += 1/self.fps
                    now=time.perf_counter()
                    if now-deadline>1/self.fps:
                        deadline=now
                    self.stop_event.wait(max(0,deadline-now))
        except Exception as exc:
            logging.exception('Capture worker failed')
            self.events.put(('error', str(exc)))
        finally:
            if camera:
                try:
                    black = np.zeros((self.size[1],self.size[0],3),dtype=np.uint8)
                    for _ in range(3):
                        camera.send(black)
                        time.sleep(0.015)
                finally:
                    camera.close()
            self.events.put(('stopped', ''))


class RegionSelector(tk.Toplevel):
    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback, self.origin, self.rect = callback, None, None
        with mss.mss() as capture:
            self.bounds = dict(capture.monitors[0])
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        self.attributes('-alpha', 0.33)
        self.configure(bg='black', cursor='crosshair')
        self.geometry(f"{self.bounds['width']}x{self.bounds['height']}+0+0")
        self.canvas = tk.Canvas(self, bg='black', highlightthickness=0, cursor='crosshair')
        self.canvas.pack(fill='both', expand=True)
        self.update_idletasks()
        # Tk negative geometry means an offset from the right edge, not a negative monitor X.
        if sys.platform == 'win32':
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [ctypes.c_void_p]
            user32.GetParent.restype = ctypes.c_void_p
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
            user32.SetWindowPos.argtypes = [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_uint]
            user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), self.bounds['left'], self.bounds['top'],
                                self.bounds['width'],self.bounds['height'],0x0040)
        self.canvas.create_text(40,40,anchor='nw',text='拖动框选要送给 Mocap 的区域    ·    Esc 取消',
                                fill='white',font=('Microsoft YaHei UI',22,'bold'))
        self.canvas.bind('<ButtonPress-1>', self.begin)
        self.canvas.bind('<B1-Motion>', self.move)
        self.canvas.bind('<ButtonRelease-1>', self.end)
        self.bind('<Escape>', lambda e:self.finish(None))
        self.focus_force()
        self.grab_set()

    def begin(self, event):
        self.origin=(event.x,event.y)
        if self.rect:self.canvas.delete(self.rect)
        self.rect=self.canvas.create_rectangle(event.x,event.y,event.x,event.y,outline=ACCENT,width=4,fill='#b1ffe8')

    def move(self,event):
        if self.origin:self.canvas.coords(self.rect,*self.origin,event.x,event.y)

    def end(self,event):
        if not self.origin:return
        try:
            region=region_from_drag(*self.origin,event.x,event.y,self.bounds)
        except ValueError:
            self.canvas.delete(self.rect);self.origin=None
            return
        self.finish(region)

    def finish(self,region):
        self.grab_release();self.destroy();self.callback(region)


class WindowPicker(tk.Toplevel):
    def __init__(self,parent,callback):
        super().__init__(parent)
        self.callback=callback;self.items={};self.windows=[]
        self.title('选择正在运行的应用窗口');self.geometry('940x530');self.configure(bg=BG)
        self.transient(parent);self.grab_set()
        tk.Label(self,text='选择具体窗口 · 被其他软件盖住也可以捕获',bg=BG,fg=TEXT,
                 font=('Microsoft YaHei UI',13,'bold')).pack(anchor='w',padx=20,pady=16)
        row=tk.Frame(self,bg=BG);row.pack(fill='x',padx=20)
        self.search=tk.StringVar()
        tk.Entry(row,textvariable=self.search,font=('Microsoft YaHei UI',11),bg=PANEL,fg=TEXT,
                 insertbackground=TEXT).pack(side='left',fill='x',expand=True)
        tk.Button(row,text='刷新窗口列表',command=self.refresh).pack(side='left',padx=(12,0))
        self.tree=ttk.Treeview(self,columns=('app','title','state'),show='headings',selectmode='browse')
        for name,title,width in [('app','应用',170),('title','窗口标题',520),('state','状态',100)]:
            self.tree.heading(name,text=title);self.tree.column(name,width=width)
        self.tree.pack(fill='both',expand=True,padx=20,pady=14)
        tk.Label(self,text='同一个应用可以有多个窗口，请按标题选择。关闭的窗口会自动停止推送。',bg=BG,fg=MUTED).pack(anchor='w',padx=20)
        tk.Button(self,text='使用选中的窗口',command=self.choose,bg=ACCENT,fg=BG,bd=0,padx=24,pady=10).pack(anchor='e',padx=20,pady=16)
        self.tree.bind('<Double-1>',lambda e:self.choose())
        self.search.trace_add('write',lambda *a:self.populate())
        self.protocol('WM_DELETE_WINDOW',lambda:self.finish(None))
        self.bind('<Escape>',lambda e:self.finish(None))
        self.refresh()

    def refresh(self):
        self.windows=list_windows();self.populate()

    def populate(self):
        self.tree.delete(*self.tree.get_children());self.items={}
        search=self.search.get().casefold()
        for window in self.windows:
            if search not in (window.title+' '+window.executable).casefold():continue
            item=self.tree.insert('','end',values=(window.executable,window.title,'已最小化' if window.minimized else '可选择'))
            self.items[item]=window

    def choose(self):
        selection=self.tree.selection()
        if selection:self.finish(self.items[selection[0]])

    def finish(self,result):
        self.grab_release();self.destroy();self.callback(result)


class WindowCropper(tk.Toplevel):
    def __init__(self,parent,image,callback):
        super().__init__(parent)
        self.callback=callback;self.original=image;self.origin=None;self.rect=None
        self.title('裁剪窗口画面');self.configure(bg=BG);self.transient(parent);self.grab_set()
        tk.Label(self,text='在画面里拖动选区 · 只推送框内内容 · Esc 取消',bg=BG,fg=TEXT,
                 font=('Microsoft YaHei UI',12)).pack(padx=20,pady=14)
        thumb=ImageOps.contain(image,(960,560),Image.Resampling.BILINEAR)
        self.photo=ImageTk.PhotoImage(thumb);self.display_size=thumb.size
        self.canvas=tk.Canvas(self,width=thumb.width,height=thumb.height,bg='black',highlightthickness=0)
        self.canvas.pack(padx=20,pady=(0,20));self.canvas.create_image(0,0,image=self.photo,anchor='nw')
        self.canvas.bind('<ButtonPress-1>',self.begin);self.canvas.bind('<B1-Motion>',self.move)
        self.canvas.bind('<ButtonRelease-1>',self.end)
        self.protocol('WM_DELETE_WINDOW',lambda:self.finish(None));self.bind('<Escape>',lambda e:self.finish(None))

    def point(self,e):
        return max(0,min(self.display_size[0],e.x)),max(0,min(self.display_size[1],e.y))

    def begin(self,e):
        self.origin=self.point(e)
        if self.rect:self.canvas.delete(self.rect)
        self.rect=self.canvas.create_rectangle(*self.origin,*self.origin,outline=ACCENT,width=3)

    def move(self,e):
        if self.origin:self.canvas.coords(self.rect,*self.origin,*self.point(e))

    def end(self,e):
        if not self.origin:return
        x1,y1=self.origin;x2,y2=self.point(e);x1,x2=sorted((x1,x2));y1,y2=sorted((y1,y2))
        w,h=self.display_size
        if (x2-x1)/w*self.original.width<24 or (y2-y1)/h*self.original.height<24:return
        self.finish((x1/w,y1/h,x2/w,y2/h))

    def finish(self,result):
        self.grab_release();self.destroy();self.callback(result)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # This app uses physical pixel coordinates; keep its typography aligned
        # with the fixed-pixel layout on high-DPI Windows desktops.
        self.tk.call('tk','scaling',1.3333333333)
        self.title('Mocap 屏幕桥 · 窗口捕获版')
        self.geometry('1160x840');self.minsize(1000,780);self.configure(bg=BG)
        self.worker=None;self.region=None;self.pending=None;self.closing=False;self.selector=None
        self.window_target=None;self.window_crop=None;self.source_kind='window'
        self.photo=None;self.mode='idle';self.error=''
        self.size_var=tk.StringVar(value='1280 × 720')
        self.fps_var=tk.StringVar(value='30')
        self.mirror_var=tk.BooleanVar(value=False)
        self.status_var=tk.StringVar(value='待选择应用窗口')
        self.region_var=tk.StringVar(value='请选择要捕获的应用窗口\n窗口被遮挡也可采集')
        self.stats_var=tk.StringVar(value='仅本机传输  ·  不保存屏幕录像')
        self.tip_var=tk.StringVar(value='推荐选择应用窗口：遮挡、移动窗口不会改变采集目标。最小化时等待恢复。')
        style=ttk.Style(self);style.theme_use('clam')
        style.configure('TCombobox',fieldbackground='#24344c',background='#24344c',foreground=TEXT,
                        arrowcolor=TEXT,padding=7)
        style.map('TCombobox',fieldbackground=[('readonly','#24344c')],foreground=[('readonly',TEXT)])
        self.option_add('*TCombobox*Listbox.background',PANEL)
        self.option_add('*TCombobox*Listbox.foreground',TEXT)
        self.build_ui();self.load_config()
        self.protocol('WM_DELETE_WINDOW',self.close_app)
        self.bind('<Escape>',lambda e:self.stop_capture())
        self.after(80,self.poll)

    def label(self,parent,text='',**kw):
        return tk.Label(parent,text=text,bg=kw.pop('bg',parent.cget('bg')),fg=kw.pop('fg',TEXT),
                        font=kw.pop('font',('Microsoft YaHei UI',10)),**kw)

    def button(self,parent,text,command,primary=False):
        return tk.Button(parent,text=text,command=command,bg=ACCENT if primary else '#273a54',
            fg=BG if primary else TEXT,activebackground='#8cebd0' if primary else '#355070',
            activeforeground=BG if primary else TEXT,disabledforeground='#718197',font=('Microsoft YaHei UI',10,'bold'),
            bd=0,padx=18,pady=11,cursor='hand2')

    def build_ui(self):
        shell=tk.Frame(self,bg=BG,padx=28,pady=22);shell.pack(fill='both',expand=True)
        header=tk.Frame(shell,bg=BG);header.pack(fill='x')
        self.label(header,'Mocap 屏幕桥',font=('Microsoft YaHei UI',24,'bold')).pack(side='left')
        self.label(header,textvariable=self.status_var,fg=ACCENT).pack(side='right')
        self.label(shell,'直接捕获应用窗口，遮挡后也能继续推送。',fg=MUTED).pack(anchor='w',pady=(7,18))
        steps=tk.Frame(shell,bg=PANEL,padx=15,pady=12);steps.pack(fill='x',pady=(0,15))
        self.label(steps,'① 选择应用窗口      →      ② 可选裁剪 + 开始推送      →      ③ Mocap 选择 OBS Virtual Camera',fg=TEXT).pack(anchor='w')
        sourcebar=tk.Frame(shell,bg=BG);sourcebar.pack(fill='x',pady=(0,14))
        self.window_btn=self.button(sourcebar,'选择应用窗口',self.select_window,True);self.window_btn.pack(side='left')
        self.select_btn=self.button(sourcebar,'屏幕区域',self.select_region);self.select_btn.pack(side='left',padx=10)
        self.crop_btn=self.button(sourcebar,'裁剪窗口画面',self.select_crop);self.crop_btn.pack(side='left')
        self.reset_crop_btn=self.button(sourcebar,'恢复完整窗口',self.reset_crop);self.reset_crop_btn.pack(side='left',padx=10)
        body=tk.Frame(shell,bg=BG);body.pack(fill='both',expand=True)
        side=tk.Frame(body,bg=PANEL,width=260,padx=18,pady=18);side.pack(side='left',fill='y',padx=(0,16));side.pack_propagate(False)
        self.button(side,'打开 AGI Mocap',self.open_mocap).pack(fill='x',side='bottom')
        self.label(side,'当前来源',font=('Microsoft YaHei UI',12,'bold')).pack(anchor='w')
        self.label(side,textvariable=self.region_var,fg=MUTED,wraplength=220,justify='left').pack(anchor='w',pady=(8,12))
        self.label(side,'输出分辨率',fg=MUTED).pack(anchor='w',pady=(24,7))
        self.size_box=ttk.Combobox(side,textvariable=self.size_var,state='readonly',values=['1280 × 720','720 × 1280','1920 × 1080','1080 × 1920'])
        self.size_box.pack(fill='x')
        self.label(side,'目标帧率',fg=MUTED).pack(anchor='w',pady=(16,7))
        self.fps_box=ttk.Combobox(side,textvariable=self.fps_var,state='readonly',values=['15','30','60']);self.fps_box.pack(fill='x')
        self.mirror_box=tk.Checkbutton(side,text='左右镜像',variable=self.mirror_var,bg=PANEL,fg=TEXT,
            selectcolor=BG,activebackground=PANEL,activeforeground=TEXT,font=('Microsoft YaHei UI',10))
        self.mirror_box.pack(anchor='w',pady=15)
        self.label(side,'等比缩放 · 保持人物比例',fg=MUTED).pack(anchor='w',pady=(0,8))
        right=tk.Frame(body,bg=PANEL,padx=14,pady=14);right.pack(side='left',fill='both',expand=True)
        self.label(right,'实时预览',font=('Microsoft YaHei UI',12,'bold')).pack(anchor='w')
        self.preview_canvas=tk.Canvas(right,bg='#080d16',highlightthickness=0);self.preview_canvas.pack(fill='both',expand=True,pady=12)
        self.preview_canvas.bind('<Configure>',lambda e:self.draw_preview())
        self.last_image=None
        self.label(right,textvariable=self.stats_var,fg=ACCENT).pack(anchor='w')
        self.label(shell,textvariable=self.tip_var,fg=MUTED,wraplength=980,justify='left').pack(anchor='w',pady=(15,12))
        footer=tk.Frame(shell,bg=BG);footer.pack(fill='x')
        self.start_btn=self.button(footer,'开始推送',self.start_stream,True);self.start_btn.pack(side='left')
        self.stop_btn=self.button(footer,'停止采集 / 推送',self.stop_capture);self.stop_btn.pack(side='left',padx=10)
        self.preview_btn=self.button(footer,'仅预览',lambda:self.start_capture(False));self.preview_btn.pack(side='left')
        self.label(footer,'Esc 随时停止',fg=MUTED).pack(side='right')

    def load_config(self):
        try:
            cfg=json.loads((STATE_DIR/'settings.json').read_text(encoding='utf8'))
            if cfg.get('size') in self.size_box['values']:self.size_var.set(cfg['size'])
            if str(cfg.get('fps')) in self.fps_box['values']:self.fps_var.set(str(cfg['fps']))
            self.mirror_var.set(bool(cfg.get('mirror',False)))
        except (OSError,ValueError):pass
        # Deliberately never restore or start screen capture automatically.

    def save_config(self):
        try:
            (STATE_DIR/'settings.json').write_text(json.dumps(dict(size=self.size_var.get(),fps=self.fps_var.get(),mirror=self.mirror_var.get())),encoding='utf8')
        except OSError:logging.exception('Cannot save preferences')

    def after_stop(self,callback):
        self.pending=callback
        if self.worker:
            self.worker.stop();self.status_var.set('正在停止…')
        else:
            self.pending=None;callback()

    def select_region(self):
        def choose():
            self.withdraw()
            self.after(200,lambda:setattr(self,'selector',RegionSelector(self,self.on_region)))
        self.after_stop(choose)

    def select_window(self):
        self.after_stop(lambda:setattr(self,'selector',WindowPicker(self,self.on_window)))

    def on_window(self,target):
        self.selector=None
        if target:
            self.source_kind='window';self.region=None;self.window_target=target;self.window_crop=None
            self.update_source_label();self.start_capture(False)

    def update_source_label(self):
        if self.window_target:
            title=self.window_target.title
            if len(title)>32:title=title[:31]+'…'
            self.region_var.set('应用窗口 · '+('已裁剪' if self.window_crop else '完整画面')+'\n'+self.window_target.executable+'\n'+title)

    def select_crop(self):
        if self.source_kind!='window' or not self.worker or self.worker.latest_source is None:
            self.tip_var.set('请先选择应用窗口并启动预览，等画面显示后再裁剪。');return
        image=self.worker.latest_source.copy()
        self.after_stop(lambda:setattr(self,'selector',WindowCropper(self,image,self.on_crop)))

    def on_crop(self,crop):
        self.selector=None
        if crop is not None:self.window_crop=crop
        self.update_source_label()
        if not self.closing:self.start_capture(False)

    def reset_crop(self):
        if self.source_kind!='window' or not self.window_target:return
        def reset():
            self.window_crop=None;self.update_source_label();self.start_capture(False)
        self.after_stop(reset)

    def on_region(self,region):
        self.selector=None;self.deiconify();self.lift()
        if region:
            self.source_kind='screen';self.window_target=None;self.window_crop=None
            self.region=region
            self.region_var.set(f'{region.width} × {region.height} 像素\n位置 {region.left}, {region.top}')
            self.start_capture(False)
        else:self.status_var.set('已取消框选')

    def start_stream(self):
        self.start_capture(True)

    def start_capture(self,stream):
        if (self.source_kind=='window' and not self.window_target) or (self.source_kind=='screen' and not self.region):
            self.tip_var.set('请先选择应用窗口，或框选屏幕区域。');return
        def start():
            self.error='';self.mode='stream' if stream else 'preview'
            size=tuple(map(int,self.size_var.get().split(' × ')))
            self.save_config()
            self.worker=CaptureWorker(self.region,size,int(self.fps_var.get()),stream,self.mirror_var.get(),
                window=self.window_target if self.source_kind=='window' else None,crop=self.window_crop)
            self.worker.start();self.status_var.set('正在连接虚拟摄像头…' if stream else '正在采集…')
            if self.source_kind=='window':
                self.tip_var.set('窗口可以被遮挡或移动。最小化会暂停有效画面，恢复后自动继续。'+('在 Mocap 选择 OBS Virtual Camera。' if stream else '可在预览中裁剪，只推送人物区域。'))
            else:self.tip_var.set('屏幕区域模式会捕获遮挡，请把其他窗口移到选区外。'+('在 Mocap 选择 OBS Virtual Camera。' if stream else '点击“开始推送”后 Mocap 才能收到画面。'))
            self.set_controls(True)
        self.after_stop(start)

    def set_controls(self,busy):
        for c in [self.size_box,self.fps_box]:c.configure(state='disabled' if busy else 'readonly')
        self.mirror_box.configure(state='disabled' if busy else 'normal')
        self.start_btn.configure(state='disabled' if busy and self.mode=='stream' else 'normal')
        self.preview_btn.configure(state='disabled' if busy and self.mode=='preview' else 'normal')

    def stop_capture(self):
        self.pending=None
        if self.worker:self.worker.stop();self.status_var.set('正在停止…')
        else:self.status_var.set('已停止')

    def draw_preview(self):
        self.preview_canvas.delete('all')
        w,h=self.preview_canvas.winfo_width(),self.preview_canvas.winfo_height()
        if self.last_image and w>5 and h>5:
            img=ImageOps.contain(self.last_image,(w,h),Image.Resampling.BILINEAR)
            self.photo=ImageTk.PhotoImage(img)
            self.preview_canvas.create_image(w//2,h//2,image=self.photo)
        else:
            self.preview_canvas.create_text(w//2,h//2,text='选择应用窗口或屏幕区域\n\n画面将在这里显示',fill=MUTED,font=('Microsoft YaHei UI',14),justify='center')

    def poll(self):
        if self.worker:
            worker=self.worker
            try:
                self.last_image=worker.preview.get_nowait();self.draw_preview()
            except queue.Empty:pass
            self.stats_var.set(f'{worker.actual_fps:.1f} fps  ·  {worker.size[0]} × {worker.size[1]}  ·  '+('OBS Virtual Camera' if worker.stream else '仅预览'))
            while not worker.events.empty():
                event,detail=worker.events.get_nowait()
                if event=='ready':self.status_var.set('● 正在推送' if worker.stream else '● 预览中')
                elif event=='source_status':self.status_var.set(detail or ('● 正在推送' if worker.stream else '● 预览中'))
                elif event=='error':
                    self.error=detail;self.status_var.set('连接失败' if worker.stream else '采集失败')
                    self.tip_var.set('无法继续：'+detail+'。若为摄像头错误，请确认 OBS 没有同时输出；若目标关闭，请重新选择窗口。')
            if not worker.thread.is_alive():
                self.worker=None;self.set_controls(False);self.last_image=None;self.draw_preview()
                if not self.error:self.status_var.set('已停止')
                self.stats_var.set('已停止采集，虚拟摄像头已释放')
                callback,self.pending=self.pending,None
                if callback:callback()
        if self.closing and self.worker is None:
            self.destroy();return
        self.after(80,self.poll)

    def open_mocap(self):
        if not AGI_EXE.exists():
            messagebox.showerror('未找到 Mocap',f'未找到已确认的版本：\n{AGI_EXE}');return
        # Unity normally enforces its own single instance; avoid duplicate launches explicitly.
        running=subprocess.run(['tasklist','/FI','IMAGENAME eq AGI Mocap.exe','/FO','CSV','/NH'],
            capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout
        if b'AGI Mocap.exe' in running:
            self.tip_var.set('AGI Mocap 已在运行，请切换到它并选择 OBS Virtual Camera。');return
        subprocess.Popen([str(AGI_EXE)],cwd=str(AGI_EXE.parent))

    def close_app(self):
        self.closing=True;self.pending=None;self.save_config()
        if self.selector:self.selector.finish(None)
        self.stop_capture()


def main():
    mutex=None
    if sys.platform=='win32':
        kernel=ctypes.windll.kernel32
        kernel.CreateMutexW.restype=ctypes.c_void_p
        mutex=kernel.CreateMutexW(None,False,'Local\\MocapScreenBridge')
        if kernel.GetLastError()==183:
            ctypes.windll.user32.MessageBoxW(None,'Mocap 屏幕桥已经打开，请切换到已有窗口。','Mocap 屏幕桥',0x40)
            return
    try:
        if '--self-check' in sys.argv:
            from test_bridge import roundtrip, ui_smoke
            from test_window_capture import run as window_test, ui_window_flow
            destination=Path(sys.argv[sys.argv.index('--self-check')+1])
            roundtrip(destination);ui_smoke(destination)
            window_test(destination,stream=True);ui_window_flow(destination)
            (destination/'packaged-check.json').write_text(json.dumps({'passed':True,'frozen':bool(getattr(sys,'frozen',False))}),encoding='utf8')
        else:
            App().mainloop()
    finally:
        if mutex:
            ctypes.windll.kernel32.CloseHandle.argtypes=[ctypes.c_void_p]
            ctypes.windll.kernel32.CloseHandle(mutex)


if __name__=='__main__':
    main()
