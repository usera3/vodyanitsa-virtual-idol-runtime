"""Loopback-only live VMC publisher/subscriber hub. Packets stay byte-identical."""
from __future__ import annotations
import argparse,json,os,socket,threading,time,uuid,sys,select
from pose_router import Router,parse,message,bundle
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from dataclasses import dataclass

SERVICE='mocap-pubsub'
TOPIC='agi/main'

@dataclass
class Subscription:
    client_id:str
    name:str
    port:int
    pid:int
    expires:float
    topics:tuple
    profile_id:str|None=None
    forwarded:int=0
    errors:int=0

class Hub:
    def __init__(self,input_port=39539,control_port=39538,state_dir=None,source_ports=None):
        self.input_port=input_port;self.control_port=control_port
        self.hub_id=uuid.uuid4().hex;self.lock=threading.RLock();self.stop_event=threading.Event()
        self.subscribers={};self.received=0;self.bytes_received=0;self.invalid=0
        self.last_input=0.;self.started=time.monotonic();self.state_dir=Path(state_dir) if state_dir else None
        self.udp=None;self.http=None;self.threads=[]
        self.router=Router(state_dir);self.inputs={};self.playbacks={};self.last_frame=0.;self.mixed_ticks=0
        self.source_ports=source_ports or {
            'kimodo':39541 if input_port==39539 else 0,
            'ardy':39542 if input_port==39539 else 0,
            'voice':39543 if input_port==39539 else 0,
            'behavior':39544 if input_port==39539 else 0,
        }
        self.app_root=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent.parent
        self.clips={}
        manifest=self.app_root/'clips'/'manifest.json'
        if manifest.exists():self.clips=json.loads(manifest.read_text(encoding='utf8'))
        catalog=self.app_root/'clips'/'action_catalog.json'
        self.action_catalog={entry['id']:{**entry,'source':self.clips[entry['clip']]['source'],
            'duration_seconds':round(self.clips[entry['clip']]['frames']/self.clips[entry['clip']]['fps'],2)}
            for entry in json.loads(catalog.read_text(encoding='utf8'))} if catalog.exists() else {}

    def start(self):
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        try:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024*1024)
            sock.bind(('127.0.0.1',self.input_port));sock.settimeout(.05)
            self.udp=sock;self.input_port=sock.getsockname()[1]
            self.inputs[sock]='agi';self.router.sources['agi'].meta['port']=self.input_port
            for source,port in self.source_ports.items():
                extra=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
                self.inputs[extra]=source
                if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):extra.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                extra.bind(('127.0.0.1',port));extra.setblocking(False)
                self.router.sources[source].meta['port']=extra.getsockname()[1]
            hub=self
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def send_json(self,status,body):
                    data=json.dumps(body,ensure_ascii=False).encode('utf8')
                    self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
                    self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers()
                    self.wfile.write(data)
                def do_GET(self):
                    if self.path=='/api/status':self.send_json(200,hub.status());return
                    if self.path=='/':
                        data=Path(__file__).with_name('dashboard_v3.html').read_bytes()
                        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
                        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
                    self.send_json(404,{'error':'not found'})
                def do_POST(self):
                    origin=self.headers.get('Origin')
                    if origin and origin not in {f'http://127.0.0.1:{hub.control_port}',f'http://localhost:{hub.control_port}'}:
                        self.send_json(403,{'error':'origin rejected'});return
                    try:
                        size=int(self.headers.get('Content-Length','0'))
                        if not 0<size<=65536:raise ValueError('invalid request size')
                        if not self.headers.get('Content-Type','').startswith('application/json'):raise ValueError('JSON required')
                        request=json.loads(self.rfile.read(size))
                        if not isinstance(request,dict):raise ValueError('object required')
                        if self.path=='/api/subscribe':result=hub.subscribe(request)
                        elif self.path=='/api/unsubscribe':result=hub.unsubscribe(request)
                        elif self.path=='/api/profile':
                            with hub.lock:result=hub.router.save(request)
                        elif self.path=='/api/playback':result=hub.playback(request)
                        elif self.path=='/api/action':result=hub.action_request(request)
                        elif self.path=='/api/generated-action':result=hub.generated_action(request)
                        else:self.send_json(404,{'error':'not found'});return
                        self.send_json(200,result)
                    except (ValueError,TypeError,KeyError) as exc:self.send_json(400,{'error':str(exc)})
            self.http=ThreadingHTTPServer(('127.0.0.1',self.control_port),Handler)
            self.control_port=self.http.server_address[1]
            for target in [self.http.serve_forever,self.receive_loop]:
                thread=threading.Thread(target=target,daemon=True);thread.start();self.threads.append(thread)
            return self
        except Exception:
            for item in self.inputs:item.close()
            sock.close()
            if self.http:self.http.server_close()
            raise

    def subscribe(self,request):
        client_id=str(request['client_id']);name=str(request.get('name','Unnamed'))
        port=int(request['port']);pid=int(request.get('pid',0));lease=float(request.get('lease_seconds',15))
        topics=request.get('topics',[TOPIC])
        if not client_id or len(client_id)>100 or len(name)>100:raise ValueError('invalid identity')
        if request.get('host','127.0.0.1')!='127.0.0.1':raise ValueError('destinations must be loopback')
        if not 1024<=port<=65535 or port in {self.control_port,*[s.meta['port'] for s in self.router.sources.values()]}:raise ValueError('invalid destination port')
        if topics!=[TOPIC]:raise ValueError('supported topic: '+TOPIC)
        if not 2<=lease<=60:raise ValueError('lease must be 2-60 seconds')
        with self.lock:
            profile=self.router.profile_for(name,request.get('profile_id'))
            self.expire()
            if len(self.subscribers)>=32 and client_id not in self.subscribers:raise ValueError('subscriber limit reached')
            if any(s.port==port and s.client_id!=client_id for s in self.subscribers.values()):raise ValueError('port already subscribed')
            old=self.subscribers.get(client_id)
            if old:
                old.name=name;old.port=port;old.pid=pid;old.expires=time.monotonic()+lease;old.topics=tuple(topics);old.profile_id=profile
            else:self.subscribers[client_id]=Subscription(client_id,name,port,pid,time.monotonic()+lease,tuple(topics),profile)
        return {'service':SERVICE,'hub_id':self.hub_id,'subscribed':True,'topic':'character/'+profile if profile else TOPIC,'profile_id':profile,'port':port,'lease_seconds':lease}

    def unsubscribe(self,request):
        with self.lock:self.subscribers.pop(str(request['client_id']),None)
        return {'unsubscribed':True,'hub_id':self.hub_id}

    def expire(self):
        now=time.monotonic()
        for key in [key for key,s in self.subscribers.items() if s.expires<=now]:self.subscribers.pop(key,None)

    def status(self):
        with self.lock:
            self.expire();now=time.monotonic()
            return {'service':SERVICE,'protocol':1,'hub_id':self.hub_id,'pid':os.getpid(),
                    'input_port':self.input_port,'control_port':self.control_port,'topic':TOPIC,
                    'uptime_seconds':round(now-self.started,1),'received_packets':self.received,
                    'received_bytes':self.bytes_received,'invalid_packets':self.invalid,
                    'source_active':bool(self.last_input and now-self.last_input<3),
                    'source_age_seconds':round(now-self.last_input,2) if self.last_input else None,
                    'clips':self.clips,'action_catalog':list(self.action_catalog.values()),**self.router.status(now),
                    'mixed_ticks':self.mixed_ticks,
                    'subscribers':[{'client_id':s.client_id,'name':s.name,'port':s.port,'pid':s.pid,'topics':s.topics,'profile_id':s.profile_id,
                        'lease_seconds_left':round(s.expires-now,1),'forwarded_packets':s.forwarded,'send_errors':s.errors}
                        for s in self.subscribers.values()]}

    def playback(self,request):
        source=request['source']
        if source not in ('kimodo','ardy'):raise ValueError('only generated-motion sources support playback')
        with self.lock:
            state=self.router.sources[source]
            if request.get('action')=='stop':
                self.playbacks.pop(source,None);state.last=0;state.bones.clear();state.blends.clear();state.clip=None;state.mode='live'
                return {'stopped':True}
            clip_id=request['clip'];meta=self.clips.get(clip_id)
            if not meta or meta['source']!=source:raise ValueError('unknown clip')
            if state.mode=='live' and state.last and time.monotonic()-state.last<2:raise ValueError('此来源正在实时发布，不能用回放覆盖')
            path=(self.app_root/'clips'/meta['file']).resolve()
            if path.parent!=(self.app_root/'clips').resolve():raise ValueError('invalid clip path')
            data=json.loads(path.read_text(encoding='utf8'))
            state.bones.clear();state.blends.clear();state.mode='replay';state.clip=meta['label']
            self.playbacks[source]={'data':data,'start':time.monotonic(),'frame':-1}
            return {'playing':True,'mode':'replay','clip':meta['label']}

    def action_request(self,request):
        actor=request['actor']
        with self.lock:
            if request.get('command')=='stop':return self.router.stop_action(actor)
            action=self.action_catalog.get(request['id'])
            if not action:raise ValueError('unknown action label')
            meta=self.clips[action['clip']]
            path=(self.app_root/'clips'/meta['file']).resolve()
            if path.parent!=(self.app_root/'clips').resolve():raise ValueError('invalid action clip path')
            clip=json.loads(path.read_text(encoding='utf8'))
            return self.router.play_action(actor,action['id'],action['label'],clip,
                                           mask=request.get('mask','full'),loop=request.get('loop',False))

    def generated_action(self,request):
        """Play one generated clip from clips/generated without catalog mutation."""
        actor=str(request['actor']);name=str(request['file'])
        if not name or Path(name).name!=name or not name.endswith('.json') or len(name)>180:
            raise ValueError('invalid generated clip name')
        root=(self.app_root/'clips'/'generated').resolve();path=(root/name).resolve()
        if path.parent!=root or not path.is_file() or path.stat().st_size>64*1024*1024:
            raise ValueError('generated clip is unavailable')
        clip=json.loads(path.read_text(encoding='utf8'))
        label=str(request.get('label') or 'ARDY 生成动作').strip()
        if not label or len(label)>100:raise ValueError('invalid generated action label')
        action_id='generated-'+uuid.uuid4().hex[:12]
        return self.router.play_action(actor,action_id,label,clip,
                                       mask=request.get('mask','full'),loop=request.get('loop',False))

    def receive_loop(self):
        output=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);output.setblocking(False)
        last_status=0.
        try:
            while not self.stop_event.is_set():
                delay=max(0.,min(.008,self.last_frame+1/30-time.monotonic()))
                try:ready,_,_=select.select(list(self.inputs),[],[],delay)
                except (OSError,ValueError):break
                for sock in ready:
                    try:data,address=sock.recvfrom(65535)
                    except (socket.timeout,BlockingIOError,OSError):continue
                    source=self.inputs[sock]
                    with self.lock:
                        now=time.monotonic()
                        try:
                            if source in self.playbacks:continue
                            self.router.sources[source].ingest(parse(data),now)
                        except (ValueError,TypeError,OverflowError):self.invalid+=1;continue
                        self.received+=1;self.bytes_received+=len(data)
                        if source=='agi':
                            self.last_input=now
                            for target in list(self.subscribers.values()):
                                if target.expires>now and self.router.pure_agi(target.profile_id,now):self.forward(output,target,data)
                now=time.monotonic()
                if now-self.last_frame>=1/30:
                    # Keep phase against a monotonic clock. Resetting to 'now'
                    # every tick drifts toward ~22 Hz on Windows' coarse wakeups.
                    if not self.last_frame:self.last_frame=now
                    else:self.last_frame+=max(1,int((now-self.last_frame)*30))/30
                    self.mixed_ticks+=1
                    with self.lock:
                        for source,replay in self.playbacks.items():
                            clip=replay['data'];index=int((now-replay['start'])*clip['fps'])%len(clip['frames'])
                            # Emit even a one-frame clip each tick so the lease stays fresh.
                            self.router.sources[source].ingest([('/VMC/Ext/Bone/Pos',[name,*raw]) for name,raw in clip['frames'][index].items()],now)
                        packets=self.router.evaluate(now)
                        for target in list(self.subscribers.values()):
                            if target.expires>now and target.profile_id in packets and not self.router.pure_agi(target.profile_id,now):self.forward(output,target,packets[target.profile_id])
                if time.monotonic()-last_status>=1:
                    last_status=time.monotonic();status=self.status()
                    if self.state_dir:
                        self.state_dir.mkdir(parents=True,exist_ok=True)
                        temp=self.state_dir/'hub-status.tmp'
                        temp.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf8')
                        temp.replace(self.state_dir/'hub-status.json')
        finally:output.close()

    @staticmethod
    def forward(output,target,data):
        try:output.sendto(data,('127.0.0.1',target.port));target.forwarded+=1
        except OSError:target.errors+=1

    def close(self):
        self.stop_event.set()
        for sock in self.inputs:sock.close()
        if self.http:self.http.shutdown();self.http.server_close()
        for thread in self.threads:thread.join(timeout=2)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input-port',type=int,default=39539);parser.add_argument('--control-port',type=int,default=39538)
    app_root=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent.parent
    parser.add_argument('--state-dir',default=str(app_root/'运行状态'))
    args=parser.parse_args();hub=Hub(args.input_port,args.control_port,args.state_dir)
    try:
        from urllib.request import build_opener,ProxyHandler
        with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{args.control_port}/api/status',timeout=.3) as response:
            existing=json.load(response)
        if existing.get('service')==SERVICE and existing.get('input_port')==args.input_port:return
    except Exception:pass
    try:
        hub.start()
        while not hub.stop_event.wait(1):pass
    except KeyboardInterrupt:pass
    except OSError as exc:
        directory=Path(args.state_dir);directory.mkdir(parents=True,exist_ok=True)
        (directory/'last-start-error.txt').write_text(str(exc),encoding='utf8')
        raise
    finally:hub.close()

if __name__=='__main__':main()
