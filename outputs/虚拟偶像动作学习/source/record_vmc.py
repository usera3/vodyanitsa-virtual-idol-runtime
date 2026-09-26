"""Explicit local AGI VMC pose recorder; no screen pixels or automatic recording.

Usage: python record_vmc.py --label "walking and waving" --seconds 30
Run only while AGI Mocap is publishing into the local motion hub.
"""
from __future__ import annotations
import argparse,datetime,hashlib,json,math,socket,sys,time,uuid
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[3]
PRODUCT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'outputs'/'Mocap动作总线'/'source'))
from pose_router import blend_name,parse

CONTROL='http://127.0.0.1:39538'
opener=build_opener(ProxyHandler({}))

def request(path,body=None):
    data=json.dumps(body,ensure_ascii=False).encode('utf8') if body is not None else None
    headers={'Content-Type':'application/json'} if body is not None else {}
    with opener.open(Request(CONTROL+path,data=data,headers=headers),timeout=2) as response:return json.load(response)

class FrameAssembler:
    def __init__(self,include_face=False,min_bones=20):
        self.include_face=include_face;self.min_bones=min_bones
        self.bones={};self.blends={};self.frames=0;self.dropped=0;self.bad_bones=0
    def finish(self,at):
        if not self.bones:return None
        required={'Hips','LeftUpperLeg','RightUpperLeg','LeftUpperArm','RightUpperArm'}
        row={'t':round(at,6),'bones':self.bones}
        if self.include_face:row['blends']=self.blends
        self.bones={};self.blends={}
        if len(row['bones'])<self.min_bones or not required.issubset(row['bones']):
            self.dropped+=1;return None
        self.frames+=1;return row
    def ingest(self,address,args,at):
        completed=None
        if address=='/VMC/Ext/Bone/Pos' and len(args)==8 and isinstance(args[0],str):
            name=args[0];raw=[float(x) for x in args[1:]]
            q=raw[3:7]
            if not all(math.isfinite(x) for x in raw) or sum(x*x for x in q)<1e-8:
                self.bad_bones+=1;return None
            if name=='Hips' and 'Hips' in self.bones and len(self.bones)>=self.min_bones:
                completed=self.finish(at)
            self.bones[name]=raw
        elif address=='/VMC/Ext/Blend/Val' and self.include_face and len(args)==2:
            self.blends[blend_name(args[0])]=float(args[1])
        elif address=='/VMC/Ext/OK':
            completed=self.finish(at)
        return completed

def record(label,seconds,source_note='',include_face=False):
    if not label.strip() or len(label)>120:raise ValueError('A short action label is required')
    if not 1<=seconds<=600:raise ValueError('seconds must be 1..600')
    status=request('/api/status')
    if status.get('service')!='mocap-pubsub' or not status.get('source_active'):
        raise RuntimeError('Start AGI Mocap and its local motion hub before recording')
    captures=PRODUCT/'captures';captures.mkdir(parents=True,exist_ok=True)
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    path=captures/f'agi_{stamp}_{uuid.uuid4().hex[:6]}.jsonl';temp=path.with_suffix('.tmp')
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024*1024)
    sock.bind(('127.0.0.1',0));sock.settimeout(.25)
    client=uuid.uuid4().hex
    lease={'client_id':client,'name':'qwen-motion-recorder-'+client[:8],
           'port':sock.getsockname()[1],'topics':['agi/main'],'lease_seconds':15}
    builder=FrameAssembler(include_face);received=0;bad_packets=0;times=[]
    start=time.monotonic();next_renew=start
    try:
        with temp.open('w',encoding='utf8') as out:
            while time.monotonic()-start<seconds:
                now=time.monotonic()
                if now>=next_renew:
                    sub=request('/api/subscribe',lease)
                    if sub.get('profile_id') is not None:raise RuntimeError('Recorder must receive raw AGI stream')
                    next_renew=now+4
                try:data,_=sock.recvfrom(65535)
                except socket.timeout:continue
                received+=1
                try:messages=parse(data)
                except (ValueError,TypeError):bad_packets+=1;continue
                at=time.monotonic()-start
                for address,args in messages:
                    row=builder.ingest(address,args,at)
                    if row:out.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n');times.append(row['t'])
            row=builder.finish(time.monotonic()-start)
            if row:out.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n');times.append(row['t'])
        if not times:raise RuntimeError('No complete AGI pose frames were received')
        temp.replace(path)
        manifest={'schema_version':1,'contract':'agi-vmc-unity-world-raw-v1','path':str(path),
                  'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                  'label':label.strip(),'source_note':source_note.strip(),
                  'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'seconds_requested':seconds,'seconds_observed':round(times[-1]-times[0],2),
                  'frames':builder.frames,'estimated_fps':round((len(times)-1)/max(.001,times[-1]-times[0]),2),
                  'dropped_incomplete_frames':builder.dropped,'invalid_bones':builder.bad_bones,
                  'invalid_packets':bad_packets,'udp_packets':received,'includes_face':include_face,
                  'training_status':'raw_capture_requires_review_and_core27_retarget'}
        path.with_suffix('.manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
        return manifest
    finally:
        try:request('/api/unsubscribe',{'client_id':client})
        except Exception:pass
        sock.close()
        if temp.exists():temp.unlink()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--label',required=True,help='Human-verified action description')
    parser.add_argument('--seconds',type=int,default=30)
    parser.add_argument('--source-note',default='',help='Optional rights/provenance note')
    parser.add_argument('--include-face',action='store_true',help='Off by default; no images ever stored')
    args=parser.parse_args()
    print(json.dumps(record(args.label,args.seconds,args.source_note,args.include_face),ensure_ascii=False,indent=2))
