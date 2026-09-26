"""Typed, parent-local pose routing. No Blender or third-party dependencies.

Wire contract: AGI-compatible VMC Unity world rotations, local bone offsets.
Core humanoid rotations are mixed in parent-local space. Legacy finger/eye
channels remain independent rotations, matching the installed receiver.
"""
import copy, json, math, struct, time
from pathlib import Path

IDENTITY=(1.,0.,0.,0.)
# This host's AGI sends OSC ,if using VRM.BlendShapePreset, verified in its
# local metadata dump (enum values 0..17), not arbitrary shape-key indices.
AGI_BLEND_PRESETS=('Unknown','Neutral','A','I','U','E','O','Blink','Joy','Angry','Sorrow','Fun','LookUp','LookDown','LookLeft','LookRight','Blink_L','Blink_R')
def blend_name(value):
    if isinstance(value,int) and 0<=value<len(AGI_BLEND_PRESETS):return AGI_BLEND_PRESETS[value]
    return str(value)
PARENTS={'Hips':None,'Spine':'Hips','Chest':'Spine','UpperChest':'Chest','Neck':'Chest','Head':'Neck'}
for side in ('Left','Right'):
    PARENTS.update({side+'Shoulder':'UpperChest',side+'UpperArm':side+'Shoulder',side+'LowerArm':side+'UpperArm',side+'Hand':side+'LowerArm',side+'UpperLeg':'Hips',side+'LowerLeg':side+'UpperLeg',side+'Foot':side+'LowerLeg'})
PARTS={
 'root':('骨盆 / 根姿态',['Hips']), 'torso':('躯干',['Spine','Chest','UpperChest']),
 'head':('颈部 / 头部',['Neck','Head']),
 'left_arm':('左臂',['LeftShoulder','LeftUpperArm','LeftLowerArm']),
 'right_arm':('右臂',['RightShoulder','RightUpperArm','RightLowerArm']),
 'left_wrist':('左手腕',['LeftHand']), 'right_wrist':('右手腕',['RightHand']),
 'left_fingers':('左手指',[]), 'right_fingers':('右手指',[]),
 'left_leg':('左腿',['LeftUpperLeg','LeftLowerLeg','LeftFoot','LeftToes']),
 'right_leg':('右腿',['RightUpperLeg','RightLowerLeg','RightFoot','RightToes']),
 'eyes':('眼睛 / 眨眼',['LeftEye','RightEye']), 'mouth':('嘴型',[]), 'expression':('其他表情',[])}
BONE_PART={bone:part for part,(_,bones) in PARTS.items() for bone in bones}
SOURCES={'agi':{'label':'AGI Mocap','port':39539,'capabilities':list(PARTS)},
 'kimodo':{'label':'Kimodo','port':39541,'capabilities':[p for p in PARTS if p not in ('left_fingers','right_fingers','eyes','mouth','expression')]},
 'ardy':{'label':'ARDY','port':39542,'capabilities':[p for p in PARTS if p not in ('left_fingers','right_fingers','eyes','mouth','expression')]},
 'voice':{'label':'语音口型','port':39543,'capabilities':['mouth']},
 'behavior':{'label':'拟人微动作','port':39544,'capabilities':['root','torso','head','left_arm','right_arm','left_wrist','right_wrist','left_leg','right_leg','eyes','expression']}}
ACTION_MASKS={
 'full':('全身',('root','torso','head','left_arm','right_arm','left_wrist','right_wrist','left_leg','right_leg')),
 'upper':('上半身',('torso','head','left_arm','right_arm','left_wrist','right_wrist')),
 'arms':('双臂',('left_arm','right_arm','left_wrist','right_wrist'))}

def normalize(q):
    n=math.sqrt(sum(x*x for x in q))
    if n<1e-8 or not math.isfinite(n):raise ValueError('invalid quaternion')
    return tuple(x/n for x in q)
def mul(a,b):
    w,x,y,z=a;v,i,j,k=b
    return (w*v-x*i-y*j-z*k,w*i+x*v+y*k-z*j,w*j-x*k+y*v+z*i,w*k+x*j-y*i+z*v)
def inv(q):return (q[0],-q[1],-q[2],-q[3])
def slerp(a,b,t):
    dot=sum(x*y for x,y in zip(a,b))
    if dot<0:b=tuple(-x for x in b);dot=-dot
    if dot>.9995:return normalize(tuple(x+(y-x)*t for x,y in zip(a,b)))
    angle=math.acos(min(1,dot));den=math.sin(angle)
    return tuple((math.sin((1-t)*angle)*x+math.sin(t*angle)*y)/den for x,y in zip(a,b))
def mirror(q):return (q[0],q[1],-q[2],-q[3])

def osc_string(s):
    b=s.encode('utf8')+b'\0';return b+b'\0'*(-len(b)%4)
def message(address,*args):
    tags=',';payload=b''
    for arg in args:
        if isinstance(arg,str):tags+='s';payload+=osc_string(arg)
        elif isinstance(arg,int):tags+='i';payload+=struct.pack('>i',arg)
        else:tags+='f';payload+=struct.pack('>f',float(arg))
    return osc_string(address)+osc_string(tags)+payload
def bundle(messages):return b'#bundle\0'+struct.pack('>Q',1)+b''.join(struct.pack('>I',len(m))+m for m in messages)
def parse(data,depth=0):
    if depth>8:raise ValueError('OSC nesting limit')
    if data.startswith(b'#bundle\0'):
        if len(data)<16:raise ValueError('short bundle')
        result=[];i=16
        while i<len(data):
            if i+4>len(data):raise ValueError('short bundle size')
            n=struct.unpack_from('>I',data,i)[0];i+=4
            if not n or i+n>len(data):raise ValueError('bad bundle element')
            result.extend(parse(data[i:i+n],depth+1));i+=n
        return result
    def string(i):
        end=data.index(b'\0',i);value=data[i:end].decode('utf8');return value,(end+4)//4*4
    try:
        address,i=string(0);tags,i=string(i)
        if not address.startswith('/VMC/') or not tags.startswith(','):raise ValueError('VMC OSC required')
        args=[]
        for tag in tags[1:]:
            if tag=='s':value,i=string(i)
            elif tag in 'if':value=struct.unpack_from('>'+tag,data,i)[0];i+=4
            elif tag in 'TF':value=tag=='T'
            else:raise ValueError('unsupported OSC type')
            if isinstance(value,float) and not math.isfinite(value):raise ValueError('non-finite number')
            args.append(value)
        if i!=len(data):raise ValueError('OSC trailing bytes')
        return [(address,args)]
    except (IndexError,UnicodeError,struct.error) as exc:raise ValueError('invalid OSC') from exc

def part_of(name,blend=False):
    if blend:
        key=name.lower()
        if any(x in key for x in ('blink','look','eye')):return 'eyes'
        if key in ('a','i','u','e','o','aa','ih','ou','ee','oh') or any(x in key for x in ('mouth','jaw','lip','viseme','tongue')):return 'mouth'
        return 'expression'
    if name in BONE_PART:return BONE_PART[name]
    if any(x in name.lower() for x in ('thumb','index','middle','ring','little','pinky')):
        return 'left_fingers' if name.startswith('Left') or name.endswith('_L') else 'right_fingers'
    return None
def remap(name,target_part,source_part):
    if target_part==source_part:return name
    if target_part.startswith('left_') and source_part.startswith('right_'):return name.replace('Left','Right').replace('_L','_R')
    if target_part.startswith('right_') and source_part.startswith('left_'):return name.replace('Right','Left').replace('_R','_L')
    return name

def swap_sides(name):
    return name.replace('Left','{LEFT}').replace('Right','Left').replace('{LEFT}','Right').replace('_L','{L}').replace('_R','_L').replace('{L}','_R')

class Source:
    def __init__(self,key,meta):
        self.key=key;self.meta=meta;self.bones={};self.blends={};self.root=None;self.last=0.;self.packets=0;self.mode='live';self.clip=None
    def ingest(self,messages,now):
        # Validate the whole datagram before changing source state.
        bones={};blends={};root=None
        for address,a in messages:
            if address=='/VMC/Ext/Bone/Pos':
                if len(a)!=8 or not isinstance(a[0],str) or len(a[0])>100:raise ValueError('invalid bone')
                raw=tuple(float(v) for v in a[1:]);normalize((raw[6],*raw[3:6]));bones[a[0]]=(raw,now)
            elif address=='/VMC/Ext/Blend/Val':
                if len(a)!=2 or not isinstance(a[0],(str,int)):raise ValueError('invalid blend')
                name=blend_name(a[0]) if self.key=='agi' else str(a[0])
                blends[name]=(max(0.,min(1.,float(a[1]))),now)
            elif address=='/VMC/Ext/Root/Pos':
                if len(a)!=8 or not isinstance(a[0],str):raise ValueError('invalid root')
                root=(a,now)
        if len(set(self.bones)|set(bones))>256 or len(set(self.blends)|set(blends))>256:raise ValueError('channel limit')
        self.bones.update(bones);self.blends.update(blends)
        if root:self.root=root
        self.last=now;self.packets+=1
    def local(self,name,now,timeout):
        value=self.bones.get(name)
        if not value or now-value[1]>timeout:return None
        raw=value[0];q=normalize((raw[6],*raw[3:6]));parent=PARENTS.get(name)
        if parent:
            p=self.bones.get(parent)
            if not p or now-p[1]>timeout:return None
            p=p[0];q=mul(inv(normalize((p[6],*p[3:6]))),q)
        return q

def default_profile(key,label):
    return {'id':key,'label':label,'revision':0,'enabled':True,'base_source':'agi','transition_ms':250,'timeout_ms':1000,'routes':{},'bones':{}}

class Router:
    def __init__(self,state_dir=None):
        self.path=Path(state_dir)/'routing.json' if state_dir else None
        self.sources={key:Source(key,dict(meta)) for key,meta in SOURCES.items()}
        self.profiles={key:default_profile(key,label) for key,label in [('vesna','薇斯纳'),('vodyanitsa','沃雅妮莎')]}
        self.states={};self.actions={};self.load_error=None
        if self.path and self.path.exists():
            try:
                data=json.loads(self.path.read_text(encoding='utf8'));profiles=data['profiles']
                self.validate_all(profiles);self.profiles=profiles
            except (ValueError,KeyError,TypeError) as exc:self.load_error=str(exc)
    def validate_all(self,profiles):
        if not isinstance(profiles,dict) or len(profiles)>32:raise ValueError('invalid profiles')
        allowed=set(self.sources)|{'character:'+k for k in profiles}
        for key,p in profiles.items():
            if not key or len(key)>64 or p.get('id')!=key:raise ValueError('invalid profile id')
            if not isinstance(p.get('label'),str) or not 0<len(p['label'])<=100:raise ValueError('invalid label')
            if p.get('base_source') not in allowed:raise ValueError('unknown base source')
            if type(p.get('enabled')) is not bool or type(p.get('revision')) is not int:raise ValueError('invalid profile')
            if not 0<=p.get('transition_ms',-1)<=2000 or not 100<=p.get('timeout_ms',0)<=10000:raise ValueError('invalid timing')
            for kind in ('routes','bones'):
                if not isinstance(p.get(kind),dict) or len(p[kind])>256:raise ValueError('invalid routes')
                for target,r in p[kind].items():
                    part=target if kind=='routes' else part_of(target)
                    if part not in PARTS:raise ValueError('unknown target part/bone: '+target)
                    if r.get('mode') not in ('source','hold','rest'):raise ValueError('invalid route mode')
                    if r['mode']!='source':continue
                    if r.get('source') not in allowed:raise ValueError('unknown source')
                    source_part=r.get('source_part',part)
                    compatible={part}
                    if part.startswith('left_'):compatible.add(part.replace('left_','right_',1))
                    if part.startswith('right_'):compatible.add(part.replace('right_','left_',1))
                    if source_part not in compatible:raise ValueError('incompatible source part')
                    if r.get('fallback','agi') not in ('agi','hold','rest'):raise ValueError('invalid fallback')
                    if type(r.get('mirror',False)) is not bool:raise ValueError('invalid mirror')
                    source_bone=r.get('source_bone')
                    if source_bone and (kind!='bones' or part_of(source_bone)!=source_part):raise ValueError('source bone must belong to source part')
        self.order(profiles)
    def order(self,profiles=None):
        profiles=profiles or self.profiles;done=set();active=set();result=[]
        def visit(key):
            if key in active:raise ValueError('角色订阅形成了循环，请移除其中一条连接')
            if key in done:return
            active.add(key);p=profiles[key]
            refs=[p['base_source']]+[r.get('source','') for kind in ('routes','bones') for r in p[kind].values() if r['mode']=='source']
            for source in refs:
                if source.startswith('character:'):visit(source.split(':',1)[1])
            active.remove(key);done.add(key);result.append(key)
        for key in profiles:visit(key)
        return result
    def save(self,request):
        if self.load_error:raise ValueError('配置读取失败，先检查 routing.json: '+self.load_error)
        key=request['id'];old=self.profiles.get(key)
        if old and request.get('expected_revision')!=old['revision']:raise ValueError('配置已被其他窗口修改，请重新载入后再保存')
        candidate=copy.deepcopy(request['profile']);candidate['id']=key;candidate['revision']=(old['revision']+1) if old else 0
        profiles={**self.profiles,key:candidate};self.validate_all(profiles)
        if self.path:
            self.path.parent.mkdir(parents=True,exist_ok=True);temp=self.path.with_suffix('.tmp')
            temp.write_text(json.dumps({'version':2,'profiles':profiles},ensure_ascii=False,indent=2),encoding='utf8');temp.replace(self.path)
        self.profiles=profiles
        state=self.states.get(key)
        if state:state['passthrough_after']=time.monotonic()+candidate['transition_ms']/1000+.1
        return copy.deepcopy(candidate)
    def profile_for(self,name,requested=None):
        if requested:
            if requested not in self.profiles:raise ValueError('unknown character profile')
            return requested
        for key,p in self.profiles.items():
            if name==p['label']:return key
        return None
    def pure_agi(self,key,now):
        if key is None:return True
        p=self.profiles[key]
        return p['enabled'] and key not in self.actions and p['base_source']=='agi' and not p['routes'] and not p['bones'] and now>=self.states.get(key,{}).get('passthrough_after',0)
    def route(self,p,name,part,blend):
        action=self.actions.get(p['id'])
        if action and not blend and part in ACTION_MASKS[action['mask']][1]:
            return {'mode':'source','source':'action:'+p['id'],'source_part':part,'fallback':'agi','action_id':action['id']}
        return (None if blend else p['bones'].get(name)) or p['routes'].get(part) or {'mode':'source','source':p['base_source'],'source_part':part,'fallback':'agi'}
    def sample(self,source,name,blend,now,timeout):
        if source.startswith('action:'):
            action=self.actions.get(source.split(':',1)[1])
            return action['source'].local(name,now,timeout) if action and not blend else None
        if source.startswith('character:'):
            state=self.states.get(source.split(':',1)[1],{})
            return state.get('blends' if blend else 'locals',{}).get(name)
        s=self.sources[source]
        if blend:
            value=s.blends.get(name)
            return value[0] if value and now-value[1]<=timeout else None
        return s.local(name,now,timeout)
    def play_action(self,actor,action_id,label,clip,mask='full',loop=False,now=None):
        if actor not in self.profiles:raise ValueError('unknown character profile')
        if not self.profiles[actor]['enabled']:raise ValueError('角色已暂停接收动作，请先恢复')
        if mask not in ACTION_MASKS or type(loop) is not bool:raise ValueError('invalid action options')
        fps=clip.get('fps');frames=clip.get('frames')
        if clip.get('contract')!='agi-vmc-unity-world-v1' or not isinstance(fps,(int,float)) or not 1<=fps<=120 or not isinstance(frames,list) or not 1<=len(frames)<=3600:
            raise ValueError('unsupported action clip')
        if not all(isinstance(frame,dict) and frame and len(frame)<=256 for frame in frames):raise ValueError('invalid action frames')
        for frame in frames:
            for name,raw in frame.items():
                if not isinstance(name,str) or len(name)>100 or not isinstance(raw,list) or len(raw)!=7:
                    raise ValueError('invalid action bone')
                if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in raw):
                    raise ValueError('non-finite action bone')
                normalize((raw[6],*raw[3:6]))
        now=time.monotonic() if now is None else now
        self.actions[actor]={'id':action_id,'label':label,'mask':mask,'loop':loop,'fps':float(fps),
                             'frames':frames,'started':now,'source':Source('action:'+actor,{})}
        return self.action_status(actor,now)
    def stop_action(self,actor,now=None):
        if actor not in self.profiles:raise ValueError('unknown character profile')
        now=time.monotonic() if now is None else now
        old=self.actions.pop(actor,None)
        if old:
            state=self.states.get(actor)
            if state:
                # An ended one-shot must not become an accidental permanent
                # pose when the base source is quiet.  Remove every channel
                # owned by the action mask; the character receiver can then
                # blend those bones back to its own calibrated rest pose.
                controlled=set(ACTION_MASKS[old['mask']][1])
                for name in list(state['locals']):
                    if part_of(name) in controlled:
                        state['locals'].pop(name,None)
                        state['positions'].pop(name,None)
                        state['transitions'].pop('bone:'+name,None)
                state['passthrough_after']=now+self.profiles[actor]['transition_ms']/1000+.1
        return {'actor':actor,'stopped':bool(old)}
    def action_status(self,actor,now):
        action=self.actions.get(actor)
        if not action:return None
        duration=len(action['frames'])/action['fps'];elapsed=max(0.,now-action['started'])
        return {'id':action['id'],'label':action['label'],'mask':action['mask'],'loop':action['loop'],
                'elapsed_seconds':round(elapsed if not action['loop'] else elapsed%duration,2),
                'duration_seconds':round(duration,2)}
    def update_actions(self,now):
        for actor,action in list(self.actions.items()):
            elapsed=now-action['started'];frames=action['frames'];fps=action['fps']
            if not action['loop'] and elapsed>=len(frames)/fps:
                self.stop_action(actor,now);continue
            frame=frames[int(elapsed*fps)%len(frames)]
            action['source'].ingest([('/VMC/Ext/Bone/Pos',[name,*raw]) for name,raw in frame.items()],now)
    def evaluate(self,now=None):
        now=time.monotonic() if now is None else now;packets={}
        self.update_actions(now)
        for key in self.order():
            p=self.profiles[key]
            st=self.states.setdefault(key,{'locals':{},'blends':{},'transitions':{},'effective':{},'positions':{},'passthrough_after':0})
            if not p['enabled']:continue
            names=set(PARENTS)
            for s in self.sources.values():names.update(n for n in s.bones if part_of(n))
            action=self.actions.get(key)
            if action:names.update(n for n in action['source'].bones if part_of(n))
            names.update(p['bones'])
            blend_names={n for s in self.sources.values() for n in s.blends}|set(st['blends'])
            for other in self.states.values():blend_names.update(other['blends'])
            effective={};timeout=p['timeout_ms']/1000
            for blend,channels in ((False,sorted(names)),(True,sorted(blend_names))):
                values=st['blends' if blend else 'locals']
                for name in channels:
                    part=part_of(name,blend);r=self.route(p,name,part,blend);mode=r['mode'];neutral=0. if blend else IDENTITY
                    origin=mode;value=None;why=''
                    if mode=='source':
                        source=r['source'];source_part=r.get('source_part',part);mapped=r.get('source_bone') or remap(name,part,source_part)
                        if blend and r.get('mirror',False):mapped=swap_sides(mapped)
                        value=self.sample(source,mapped,blend,now,timeout);origin=source+':'+mapped
                        if value is None:
                            why='缺少通道或来源超时';fallback=r.get('fallback','agi');origin='fallback:'+fallback
                            if fallback=='agi':value=self.sample('agi',name,blend,now,timeout)
                            if fallback=='rest':value=neutral
                        elif r.get('mirror',False) and not blend:value=mirror(value)
                    elif mode=='rest':value=neutral
                    if value is None:
                        if name not in values:continue
                        value=values[name];origin='hold' if not why else origin+'→hold'
                    previous=values.get(name,value);channel=('blend:' if blend else 'bone:')+name
                    signature=json.dumps(r,sort_keys=True)+origin
                    transition=st['transitions'].get(channel)
                    if not transition or transition[0]!=signature:
                        transition=(signature,now,previous);st['transitions'][channel]=transition
                    # Voice visemes are already smoothed against the playback
                    # clock.  Keep route acquisition/release short enough to
                    # avoid a visible quarter-second lip-sync delay.
                    transition_ms=40 if blend and r.get('source')=='voice' else p['transition_ms']
                    duration=transition_ms/1000;t=min(1.,(now-transition[1])/duration) if duration else 1.
                    values[name]=(transition[2]+(value-transition[2])*t) if blend else slerp(transition[2],value,t)
                    bucket=effective.setdefault(part,{'requested':r.get('source',mode),'actual':set(),'fallback':False,'channels':0})
                    bucket['actual'].add(origin.split(':'+name)[0]);bucket['fallback']|=bool(why);bucket['channels']+=1
            st['effective']={part:{**v,'actual':sorted(v['actual'])} for part,v in effective.items()}
            world={};messages=[]
            # PARENTS insertion order is topological; independent finger/eye channels follow.
            for name in list(PARENTS)+sorted(set(st['locals'])-set(PARENTS)):
                if name not in st['locals']:continue
                parent=PARENTS.get(name);q=mul(world.get(parent,IDENTITY),st['locals'][name]);world[name]=q
                reference=self.sources['agi'].bones.get(name)
                if reference:st['positions'][name]=reference[0][:3]
                if name not in st['positions']:
                    reference=next((s.bones[name] for s in self.sources.values() if name in s.bones),None)
                    if not reference and action:reference=action['source'].bones.get(name)
                    if reference:st['positions'][name]=reference[0][:3]
                if name not in st['positions']:continue
                messages.append(message('/VMC/Ext/Bone/Pos',name,*st['positions'][name],*q[1:],q[0]))
            for name,value in st['blends'].items():messages.append(message('/VMC/Ext/Blend/Val',name,value))
            messages.extend([message('/VMC/Ext/Blend/Apply'),message('/VMC/Ext/OK',1),message('/VMC/Ext/T',now%86400)])
            # Keep bundle below UDP's limit; current <=256 bones+256 face channels.
            packets[key]=bundle(messages)
        return packets
    def status(self,now):
        return {'routing_version':2,'routing_load_error':self.load_error,'profiles':copy.deepcopy(self.profiles),
                'action_masks':{key:label for key,(label,_) in ACTION_MASKS.items()},
                'active_actions':{key:self.action_status(key,now) for key in self.actions},
                'parts':{k:{'label':v[0],'bones':v[1]} for k,v in PARTS.items()},
                'known_bones':sorted(set(PARENTS)|{n for s in self.sources.values() for n in s.bones if part_of(n)}),
                'effective':{key:state['effective'] for key,state in self.states.items()},
                'sources':[{ 'id':key,**s.meta,'active':bool(s.last and now-s.last<1),
                    'age_seconds':round(now-s.last,2) if s.last else None,'mode':s.mode,'clip':s.clip,'packets':s.packets,
                    'bones':len(s.bones),'blends':len(s.blends)} for key,s in self.sources.items()]}
