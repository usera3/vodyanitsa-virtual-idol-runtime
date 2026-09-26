"""Real WGC occlusion, window lifecycle and virtual-camera integration test."""
import json
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
import tkinter as tk
from PIL import Image
import numpy as np
import mss
from screen_bridge import CaptureWorker
from window_source import WindowTarget, WindowFrames, top_hwnd, window_pid, list_windows, crop_image

def run(out_dir,stream=False):
    out=Path(out_dir);out.mkdir(parents=True,exist_ok=True)
    app=tk.Tk();app.withdraw()
    target=tk.Toplevel(app);target.title('Mocap WGC animated source');target.geometry('640x360+40+40')
    canvas=tk.Canvas(target,bg='red',highlightthickness=0);canvas.pack(fill='both',expand=True)
    app.update()
    handle=top_hwnd(target)
    window=WindowTarget(handle,window_pid(handle),target.title(),'python.exe')
    assert any(w.hwnd==handle for w in list_windows(include_self=True))
    source=WindowFrames(window,30);source.start()
    cover=tk.Toplevel(app);cover.title('Occlusion verification');cover.geometry('800x520+10+10')
    cover.attributes('-topmost',True);cover.configure(bg='#090909')
    tk.Label(cover,text='窗口遮挡测试\n后面的色块仍在通过窗口接口更新',bg='#090909',fg='white').pack(pady=100)
    app.update()
    begin=time.perf_counter()
    def animate():
        if not target.winfo_exists():return
        phase=int((time.perf_counter()-begin)*4)%3
        canvas.configure(bg=['#ff0000','#00ff00','#0000ff'][phase])
        canvas.delete('all')
        x=100+int((time.perf_counter()-begin)*100)%350
        canvas.create_rectangle(x,180,x+35,240,fill='white',outline='white')
        app.after(35,animate)
    animate()
    def pump(seconds):
        until=time.perf_counter()+seconds
        while time.perf_counter()<until:app.update();time.sleep(.01)
    report={};worker=None
    try:
        pump(.7)
        colors=[];snapshots=[];first=source.frames
        for _ in range(30):
            pump(.08)
            image,status=source.read()
            if image is not None:
                rgb=np.asarray(image)[70:100,50:80].mean(axis=(0,1))
                colors.append(int(rgb.argmax()));snapshots.append(image)
                assert rgb.max()>200,('Captured occluder instead of source',rgb.tolist())
        assert set(colors)=={0,1,2},('Covered window did not keep updating',colors)
        assert source.frames-first>10
        snapshots[-1].save(out/'occluded-window-captured.png')
        with mss.mss() as desktop:
            shot=desktop.grab(dict(left=canvas.winfo_rootx()+50,top=canvas.winfo_rooty()+70,width=30,height=30))
            actual=np.frombuffer(shot.rgb,dtype=np.uint8).reshape(-1,3).mean(axis=0)
            assert actual.max()<50,('Source not actually occluded',actual.tolist())
        report['occluded_live_colors']=sorted(set(colors));report['covered_frames']=source.frames-first
        report['desktop_occluder_rgb']=actual.tolist()
        # Move, resize, and rename must keep the exact selected window, no title lookup.
        target.title('Renamed source');target.geometry('500x300+130+140');pump(.7)
        image,status=source.read();assert image is not None
        assert 490<=image.width<=530 and 290<=image.height<=350,image.size
        report['renamed_resized_frame']=image.size
        cropped=crop_image(image,(.1,.1,.8,.8));assert cropped.width<image.width
        report['crop_size']=cropped.size
        target.iconify();pump(.5)
        image,status=source.read();assert image is None and '最小化' in status,status
        report['minimized_status']=status
        target.deiconify();pump(.5)
        source.read();pump(.3)
        image,status=source.read();assert image is not None,status
        report['restored']=True
        source.close()
        target.iconify();pump(.2)
        source=WindowFrames(window,30);source.start()
        assert source.read()[0] is None
        target.deiconify();pump(.2);source.read();pump(.4)
        assert source.read()[0] is not None
        source.close();report['initially_minimized_recovers']=True
        if stream:
            # Same capture worker as the UI, still hidden behind the test occluder.
            cover.lift();worker=CaptureWorker(None,(640,360),30,True,window=window,crop=(.05,.15,.95,.95))
            worker.start();received={};done=threading.Event()
            def consume():
                try:
                    event=worker.events.get(timeout=10)
                    if event[0]!='ready':raise RuntimeError(event)
                    time.sleep(.5)
                    p=subprocess.run([shutil.which('ffmpeg'),'-hide_banner','-loglevel','error','-f','dshow',
                        '-video_size','640x360','-framerate','30','-i','video=OBS Virtual Camera',
                        '-frames:v','45','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],capture_output=True,timeout=15,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    if p.returncode:raise RuntimeError(p.stderr.decode(errors='replace'))
                    frames=np.frombuffer(p.stdout,dtype=np.uint8).reshape(-1,360,640,3)
                    rgb=frames[:,80:100,200:220].mean(axis=(1,2))
                    assert len(frames)==45 and set(map(int,rgb.argmax(axis=1)))=={0,1,2}
                    assert rgb.max(axis=1).mean()>200
                    received.update(frames=len(frames),colors=[0,1,2],fps=round(worker.actual_fps,2))
                except Exception as exc:received['error']=str(exc)
                finally:done.set()
            threading.Thread(target=consume,daemon=True).start()
            until=time.perf_counter()+20
            while not done.is_set() and time.perf_counter()<until:pump(.05)
            assert done.is_set() and 'error' not in received,received
            report['virtual_camera_under_occlusion']=received
            # Target closure must stop the producer, not capture another desktop/window.
            target.destroy()
            until=time.perf_counter()+5
            while worker.thread.is_alive() and time.perf_counter()<until:pump(.05)
            assert not worker.thread.is_alive(),'Producer did not stop after target closed'
            events=[]
            while not worker.events.empty():events.append(worker.events.get_nowait())
            assert any(e[0]=='error' and '窗口' in e[1] for e in events),events
            report['target_close_stops_output']=True
        report['passed']=True
    finally:
        if worker:worker.stop();worker.thread.join(timeout=4)
        source.close();app.destroy()
    (out/'window-capture-test.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False),flush=True)

def ui_window_flow(out_dir):
    from types import SimpleNamespace
    import screen_bridge as module
    app=module.App();app.attributes('-topmost',True);app.update()
    target=tk.Toplevel(app);target.title('Window flow source');target.geometry('420x260+20+20')
    target.configure(bg='#35b78d');app.update()
    def pump(seconds):
        until=time.perf_counter()+seconds
        while time.perf_counter()<until:app.update();time.sleep(.015)
    original=module.list_windows
    # The test window shares the test runner process. Production intentionally excludes self.
    module.list_windows=lambda:list_windows(include_self=True)
    try:
        app.window_btn.invoke();pump(.1)
        picker=app.selector;picker.search.set('Window flow source');picker.populate()
        rows=picker.tree.get_children();assert len(rows)==1
        picker.tree.selection_set(rows[0]);picker.choose();pump(.7)
        assert app.window_target.hwnd==top_hwnd(target) and app.worker.latest_source is not None
        app.crop_btn.invoke();pump(.4)
        cropper=app.selector;w,h=cropper.display_size
        cropper.begin(SimpleNamespace(x=int(w*.1),y=int(h*.2)))
        cropper.move(SimpleNamespace(x=int(w*.8),y=int(h*.9)))
        cropper.end(SimpleNamespace(x=int(w*.8),y=int(h*.9)));pump(.6)
        assert app.window_crop and app.worker.crop==app.window_crop
        app.reset_crop_btn.invoke();pump(.6)
        assert app.window_crop is None and app.worker.window.hwnd==top_hwnd(target)
        target.lower(app);app.lift();pump(.2)
        with mss.mss() as capture:
            box=dict(left=app.winfo_rootx(),top=app.winfo_rooty(),width=app.winfo_width(),height=app.winfo_height())
            shot=capture.grab(box)
            Image.frombytes('RGB',shot.size,shot.bgra,'raw','BGRX').save(Path(out_dir)/'window-mode-ui.png')
        app.stop_btn.invoke();pump(.4);assert app.worker is None
        app.close_app();pump(.15)
    finally:
        module.list_windows=original
        try:app.destroy()
        except tk.TclError:pass
    print('WINDOW_UI_PASS: choose from list, preview, crop, reset crop, stop, close',flush=True)

if __name__=='__main__':
    import sys
    if '--ui' in sys.argv:ui_window_flow(sys.argv[-1])
    else:run(sys.argv[-1],stream='--stream' in sys.argv)
