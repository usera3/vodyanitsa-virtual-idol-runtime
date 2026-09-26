"""Blender-side subscriber: private ephemeral socket, lease, heartbeat and reconnect."""
import atexit,json,os,subprocess,threading,time,uuid
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler
from pose_router import blend_name

HUB_ROOT=Path(__file__).resolve().parent.parent
CONTROL='http://127.0.0.1:39538'

def request(path,body=None,base=CONTROL):
    data=json.dumps(body,ensure_ascii=False).encode('utf8') if body is not None else None
    req=Request(base+path,data=data,headers={'Content-Type':'application/json'})
    with build_opener(ProxyHandler({})).open(req,timeout=.7) as response:return json.load(response)

class Subscriber:
    def __init__(self,name,base=CONTROL,auto_start=True):
        self.name=name;self.base=base;self.auto_start=auto_start;self.client_id=uuid.uuid4().hex
        self.port=None;self.stop_event=threading.Event();self.last_start=0.;self.thread=None
        self.info={'subscribed':False,'topic':'agi/main','receiver_port':None,'error':None}
        atexit.register(self.close)

    def ensure_receiver(self,vmc,scene):
        # The original wire stream can use integer VRM blend presets. Normalize
        # process-locally before the addon builds its dispatcher. Mixed output
        # already uses the same canonical names; neither path changes bone math.
        if hasattr(vmc,'_on_vmc_blend_val') and not getattr(vmc,'_motion_bus_blends',False):
            original=vmc._on_vmc_blend_val
            def blend(address,*args):
                if args:original(address,blend_name(args[0]),*args[1:])
            vmc._on_vmc_blend_val=blend;vmc._motion_bus_blends=True
        if not vmc.is_running():
            # The addon UI has min port 1; a proxy lets the OS bind port 0 atomically.
            class EphemeralScene:
                vmc_link_port=0
                def __getattr__(_,key):return getattr(scene,key)
            vmc._start_server(EphemeralScene())
            actual_port=vmc._receiver_socket.getsockname()[1]
            # Update the displayed port without opening a replacement socket or
            # persisting a random port as another character's global default.
            receiver=vmc._receiver_socket
            persist=getattr(vmc,'_persist_connection_defaults',None)
            vmc._receiver_socket=None
            if persist:vmc._persist_connection_defaults=lambda *_:None
            try:scene.vmc_link_port=actual_port
            finally:
                vmc._receiver_socket=receiver
                if persist:vmc._persist_connection_defaults=persist
            assert scene.vmc_link_port==actual_port
        self.port=vmc._receiver_socket.getsockname()[1]
        self.info['receiver_port']=self.port
        if self.thread is None:
            self.thread=threading.Thread(target=self.run,name='mocap-subscription',daemon=True);self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                status=request('/api/status',base=self.base)
                if status.get('service')!='mocap-pubsub' or status.get('protocol')!=1:raise RuntimeError('该端口不是 Mocap 动作总线')
                response=request('/api/subscribe',{'client_id':self.client_id,'name':self.name,'pid':os.getpid(),
                    'port':self.port,'topics':['agi/main'],'lease_seconds':15},base=self.base)
                self.info.update(subscribed=True,hub_id=response['hub_id'],topic=response.get('topic','agi/main'),
                                 profile_id=response.get('profile_id'),error=None,last_heartbeat=time.time())
            except Exception as exc:
                self.info.update(subscribed=False,error=str(exc))
                if self.auto_start and time.monotonic()-self.last_start>10:
                    # Only start our own packaged hub, never replace an occupied service.
                    self.last_start=time.monotonic();exe=HUB_ROOT/'Mocap动作总线.exe'
                    if exe.exists():
                        subprocess.Popen([str(exe),'--state-dir',str(HUB_ROOT/'运行状态')],cwd=str(HUB_ROOT),
                                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.stop_event.wait(4 if self.info['subscribed'] else 1)

    def status(self):return dict(self.info)

    def close(self):
        if self.stop_event.is_set():return
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():self.thread.join(timeout=2)
        if self.port:
            try:request('/api/unsubscribe',{'client_id':self.client_id},base=self.base)
            except Exception:pass
