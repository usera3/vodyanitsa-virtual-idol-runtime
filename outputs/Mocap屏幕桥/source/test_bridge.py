"""Functional regression tests and real screen -> DirectShow roundtrip."""
import json
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
import unittest

import numpy as np
from PIL import Image
from screen_bridge import Region, region_from_drag, fit_frame, CaptureWorker, App, RegionSelector


class GeometryTests(unittest.TestCase):
    def test_reverse_drag_negative_monitor(self):
        bounds=dict(left=-1920,top=-200,width=3840,height=1280)
        self.assertEqual(region_from_drag(500,400,100,50,bounds),Region(-1820,-150,400,350))

    def test_clipped_selection(self):
        b=dict(left=0,top=0,width=100,height=100)
        self.assertEqual(region_from_drag(-20,10,200,90,b),Region(0,10,100,80))

    def test_invalid_region(self):
        with self.assertRaises(ValueError):Region(0,0,10,10).validate(dict(left=0,top=0,width=100,height=100))
        with self.assertRaises(ValueError):Region(-1,0,30,30).validate(dict(left=0,top=0,width=100,height=100))

    def test_no_stretch_letterbox(self):
        out=np.asarray(fit_frame(Image.new('RGB',(100,200),'red'),(200,200)))
        self.assertTrue(np.all(out[:,0:50]==0))
        self.assertTrue(np.all(out[:,50:150,0]==255))
        self.assertTrue(np.all(out[:,150:]==0))

    def test_mirror(self):
        im=Image.new('RGB',(100,100),'red');im.paste('blue',(50,0,100,100))
        out=fit_frame(im,(100,100),True)
        self.assertEqual(out.getpixel((10,10)),(0,0,255))


def roundtrip(out_dir):
    out_dir=Path(out_dir);out_dir.mkdir(parents=True,exist_ok=True)
    root=tk.Tk();root.title('Mocap 屏幕桥 · 链路测试')
    root.geometry('640x360+40+40');root.attributes('-topmost',True)
    canvas=tk.Canvas(root,bg='red',highlightthickness=0);canvas.pack(fill='both',expand=True)
    root.update()
    region=Region(canvas.winfo_rootx(),canvas.winfo_rooty(),canvas.winfo_width(),canvas.winfo_height())
    worker=CaptureWorker(region,(640,360),30,True)
    worker.start()
    result={};done=threading.Event();start=time.perf_counter()
    def animate():
        phase=int((time.perf_counter()-start)*3)%3
        canvas.configure(bg=['#ff0000','#00ff00','#0000ff'][phase])
        canvas.delete('all')
        canvas.create_rectangle(40+int((time.perf_counter()-start)*80)%500,100,100+int((time.perf_counter()-start)*80)%500,200,fill='white',outline='white')
        if not done.is_set():root.after(25,animate)
    def receive():
        try:
            event=worker.events.get(timeout=10)
            if event[0]!='ready':raise RuntimeError(event)
            ffmpeg=shutil.which('ffmpeg')
            if not ffmpeg:raise RuntimeError('FFmpeg is needed only for this test')
            received=subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','dshow',
                '-video_size','640x360','-framerate','30','-i','video=OBS Virtual Camera',
                '-frames:v','45','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],capture_output=True,timeout=20,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if received.returncode:raise RuntimeError(received.stderr.decode(errors='replace'))
            data=np.frombuffer(received.stdout,dtype=np.uint8).reshape(-1,360,640,3)
            rgb=data[:,:50,:50].mean(axis=(1,2));dominant=rgb.argmax(axis=1)
            assert len(data)==45,(len(data),'frames')
            assert len(set(map(int,dominant)))==3,'Missing red/green/blue live updates'
            assert float(rgb.max(axis=1).mean())>200,'Frame is too dark'
            result.update(passed=True,received_frames=len(data),colors=list(map(int,sorted(set(dominant)))),
                          sender_fps=round(worker.actual_fps,2),device=worker.device,
                          region=region.as_dict(),test='Real MSS capture -> OBS Virtual Camera -> FFmpeg DirectShow')
            Image.fromarray(data[10]).save(out_dir/'virtual-camera-received.png')
        except Exception as exc:result.update(passed=False,error=str(exc))
        finally:
            worker.stop();worker.thread.join(timeout=4)
            result['worker_stopped']=not worker.thread.is_alive()
            done.set()
    threading.Thread(target=receive,daemon=True).start()
    animate()
    def finish():
        if done.is_set():root.destroy()
        else:root.after(100,finish)
    finish();root.mainloop()
    # A successful second producer proves the first released its device.
    if result.get('passed'):
        import pyvirtualcam
        with pyvirtualcam.Camera(width=640,height=360,fps=30,backend='obs') as camera:
            camera.send(np.zeros((360,640,3),dtype=np.uint8))
        result['reopen_after_stop']=True
    (out_dir/'roundtrip.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    if not result.get('passed'):raise RuntimeError(result)


def ui_smoke(out_dir):
    import mss
    from types import SimpleNamespace
    root=App();root.attributes('-topmost',True);root.update()
    root.select_btn.invoke()
    end=time.time()+1
    while time.time()<end:root.update();time.sleep(.01)
    selector=root.selector
    assert isinstance(selector,RegionSelector)
    b=selector.bounds
    # Exercise actual overlay drag callbacks in physical coordinates.
    selector.begin(SimpleNamespace(x=80,y=100))
    selector.move(SimpleNamespace(x=720,y=460))
    selector.end(SimpleNamespace(x=720,y=460))
    assert root.region==Region(b['left']+80,b['top']+100,640,360)
    end=time.time()+1
    while time.time()<end:root.update();time.sleep(.02)
    assert root.worker and root.worker.frames>3 and root.mode=='preview'
    root.stop_btn.invoke()
    end=time.time()+2
    while root.worker and time.time()<end:root.update();time.sleep(.02)
    assert root.worker is None
    # Settings unlock after stop; a second preview can start and close gracefully.
    assert str(root.size_box.cget('state'))=='readonly'
    root.update()
    with mss.mss() as capture:
        box=dict(left=root.winfo_rootx(),top=root.winfo_rooty(),width=root.winfo_width(),height=root.winfo_height())
        shot=capture.grab(box)
        Image.frombytes('RGB',shot.size,shot.bgra,'raw','BGRX').save(Path(out_dir)/'app-ui.png')
    root.preview_btn.invoke()
    end=time.time()+.5
    while time.time()<end:root.update();time.sleep(.02)
    worker=root.worker
    root.close_app()
    end=time.time()+3
    while worker.thread.is_alive() and time.time()<end:root.update();time.sleep(.02)
    assert not worker.thread.is_alive()
    try:root.destroy()
    except tk.TclError:pass
    print('UI_SMOKE_PASS: select, preview, stop, settings unlock, restart, close')


if __name__=='__main__':
    import sys
    if '--roundtrip' in sys.argv:roundtrip(sys.argv[-1])
    elif '--ui' in sys.argv:ui_smoke(sys.argv[-1])
    else:unittest.main()
