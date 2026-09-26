import copy,json,math,socket,tempfile,time,unittest
from pathlib import Path
from pose_router import Router,PARENTS,IDENTITY,mul,inv,mirror,parse,message,bundle
from vmc_hub import Hub
from 动作订阅 import request

def axis(degrees,vector=(0,1,0)):
    a=math.radians(degrees)/2;return (math.cos(a),*(math.sin(a)*x for x in vector))
def frame(local=None,offset=0):
    local=local or {};world={};out=[]
    for i,name in enumerate(PARENTS):
        q=mul(world.get(PARENTS[name],IDENTITY),local.get(name,IDENTITY));world[name]=q
        out.append(('/VMC/Ext/Bone/Pos',[name,offset+i/100,.1,0,*q[1:],q[0]]))
    return out
def raw_local(data,name):
    bones={a[0]:a[1:] for address,a in parse(data) if address=='/VMC/Ext/Bone/Pos'}
    def q(name):r=bones[name];return (r[6],*r[3:6])
    return mul(inv(q(PARENTS[name])),q(name)) if PARENTS[name] else q(name)

class RoutingTests(unittest.TestCase):
    def setUp(self):self.router=Router();self.now=time.monotonic()
    def put(self,source,local=None,now=None,offset=0):self.router.sources[source].ingest(frame(local,offset),self.now if now is None else now)
    def configure(self,changes,key='vesna'):
        p=copy.deepcopy(self.router.profiles[key]);p.update(transition_ms=0,**changes)
        return self.router.save({'id':key,'expected_revision':p['revision'],'profile':p})
    def assertQuat(self,a,b):self.assertAlmostEqual(abs(sum(x*y for x,y in zip(a,b))),1,places=5)
    def test_parent_local_composition_and_reference_lengths(self):
        self.put('agi',{'Chest':axis(55),'Head':axis(15)},offset=0)
        self.put('ardy',{'Chest':axis(-80),'Head':axis(40,(1,0,0))},offset=10)
        self.configure({'routes':{'head':{'mode':'source','source':'ardy','fallback':'hold'}}})
        data=self.router.evaluate(self.now)['vesna']
        self.assertQuat(raw_local(data,'Head'),axis(40,(1,0,0)))
        self.assertQuat(raw_local(data,'Chest'),axis(55))
        hips=next(a for address,a in parse(data) if address=='/VMC/Ext/Bone/Pos' and a[0]=='Hips')
        self.assertEqual(hips[1],0.) # ARDY lengths did not replace AGI reference.
    def test_cross_side_and_single_bone_priority(self):
        self.put('agi');self.put('ardy',{'RightUpperArm':axis(35),'RightLowerArm':axis(65)})
        self.put('kimodo',{'LeftLowerArm':axis(-10)})
        self.configure({'routes':{'left_arm':{'mode':'source','source':'ardy','source_part':'right_arm','mirror':True}},
                        'bones':{'LeftLowerArm':{'mode':'source','source':'kimodo','source_bone':'LeftLowerArm'}}})
        data=self.router.evaluate(self.now)['vesna']
        self.assertQuat(raw_local(data,'LeftUpperArm'),mirror(axis(35)))
        self.assertQuat(raw_local(data,'LeftLowerArm'),axis(-10))
        self.assertQuat(raw_local(data,'RightUpperArm'),IDENTITY)
    def test_stale_individual_channel_fallback_hold_and_rest(self):
        self.put('agi',{'Head':axis(10)});self.put('ardy',{'Head':axis(70)})
        self.configure({'routes':{'head':{'mode':'source','source':'ardy','fallback':'agi'}}})
        self.router.evaluate(self.now)
        later=self.now+2;self.put('agi',{'Head':axis(25)},now=later)
        # A fresh heartbeat does not make stale bones fresh.
        self.router.sources['ardy'].ingest([('/VMC/Ext/OK',[1])],later)
        data=self.router.evaluate(later)['vesna'];self.assertQuat(raw_local(data,'Head'),axis(25))
        self.assertTrue(self.router.states['vesna']['effective']['head']['fallback'])
        data=self.router.evaluate(later+3)['vesna'];self.assertQuat(raw_local(data,'Head'),axis(25))
        self.configure({'routes':{'head':{'mode':'rest'}}});data=self.router.evaluate(later+4)['vesna']
        self.assertQuat(raw_local(data,'Head'),IDENTITY)
    def test_character_routes_topology_and_cycles(self):
        self.put('agi');self.put('ardy',{'LeftHand':axis(55)})
        self.configure({'routes':{'left_wrist':{'mode':'source','source':'ardy'}}},'vodyanitsa')
        self.configure({'routes':{'left_wrist':{'mode':'source','source':'character:vodyanitsa'}}})
        data=self.router.evaluate(self.now)['vesna'];self.assertQuat(raw_local(data,'LeftHand'),axis(55))
        with self.assertRaisesRegex(ValueError,'循环'):
            self.configure({'routes':{'head':{'mode':'source','source':'character:vesna'}}},'vodyanitsa')
        self.assertEqual(self.router.profiles['vodyanitsa']['routes']['left_wrist']['source'],'ardy')
    def test_transition_pause_persistence_and_conflict(self):
        self.put('agi');self.put('ardy',{'Head':axis(90)})
        self.router.evaluate(self.now)
        p=copy.deepcopy(self.router.profiles['vesna']);p['routes']={'head':{'mode':'source','source':'ardy'}}
        self.router.save({'id':'vesna','expected_revision':0,'profile':p})
        a=self.router.evaluate(self.now+.01)['vesna'];b=self.router.evaluate(self.now+.135)['vesna'];c=self.router.evaluate(self.now+.3)['vesna']
        self.assertQuat(raw_local(a,'Head'),IDENTITY);self.assertQuat(raw_local(b,'Head'),axis(45));self.assertQuat(raw_local(c,'Head'),axis(90))
        self.configure({'enabled':False});self.assertNotIn('vesna',self.router.evaluate(self.now+.4))
        with tempfile.TemporaryDirectory() as d:
            r=Router(d);p=copy.deepcopy(r.profiles['vesna']);p['routes']={'left_leg':{'mode':'hold'}}
            r.save({'id':'vesna','expected_revision':0,'profile':p})
            self.assertEqual(Router(d).profiles['vesna']['routes'],p['routes'])
            with self.assertRaises(ValueError):r.save({'id':'vesna','expected_revision':0,'profile':p})
    def test_face_channel_partition_and_independent_routes(self):
        self.put('agi')
        self.router.sources['agi'].ingest([('/VMC/Ext/Blend/Val',['A',.7]),('/VMC/Ext/Blend/Val',['Blink_L',.8]),('/VMC/Ext/Blend/Val',['Joy',.9])],self.now)
        self.configure({'routes':{'mouth':{'mode':'rest'}}});data=self.router.evaluate(self.now)['vesna']
        blends={a[0]:a[1] for addr,a in parse(data) if addr=='/VMC/Ext/Blend/Val'}
        self.assertEqual(blends['A'],0);self.assertAlmostEqual(blends['Blink_L'],.8);self.assertAlmostEqual(blends['Joy'],.9)
    def test_voice_source_owns_only_mouth_and_falls_back_closed(self):
        self.put('agi')
        self.router.sources['agi'].ingest([('/VMC/Ext/Blend/Val',['A',.2]),('/VMC/Ext/Blend/Val',['Blink_L',.8]),('/VMC/Ext/Blend/Val',['Joy',.9])],self.now)
        self.router.sources['voice'].ingest([('/VMC/Ext/Blend/Val',['A',.85]),('/VMC/Ext/Blend/Val',['I',.1])],self.now)
        self.configure({'routes':{'mouth':{'mode':'source','source':'voice','fallback':'rest'}}})
        data=self.router.evaluate(self.now+.05)['vesna']
        blends={a[0]:a[1] for addr,a in parse(data) if addr=='/VMC/Ext/Blend/Val'}
        self.assertAlmostEqual(blends['A'],.85)
        self.assertAlmostEqual(blends['I'],.1)
        self.assertAlmostEqual(blends['Blink_L'],.8)
        self.assertAlmostEqual(blends['Joy'],.9)
        self.router.evaluate(self.now+2)
        closed=self.router.evaluate(self.now+2.05)['vesna']
        closed_blends={a[0]:a[1] for addr,a in parse(closed) if addr=='/VMC/Ext/Blend/Val'}
        self.assertEqual(closed_blends['A'],0)
        self.assertEqual(closed_blends['I'],0)
    def test_behavior_source_drives_micro_pose_blink_and_expression_only(self):
        self.put('agi')
        self.put('behavior',{'Spine':axis(1,(1,0,0)),'Chest':axis(2,(1,0,0)),
                             'Neck':axis(3),'Head':axis(4),'LeftEye':axis(2),'RightEye':axis(2)})
        self.router.sources['behavior'].ingest([
            ('/VMC/Ext/Blend/Val',['Blink_L',.8]),('/VMC/Ext/Blend/Val',['Blink_R',.7]),
            ('/VMC/Ext/Blend/Val',['Joy',.25]),('/VMC/Ext/Blend/Val',['A',.9])],self.now)
        self.configure({'routes':{
            'torso':{'mode':'source','source':'behavior','fallback':'agi'},
            'head':{'mode':'source','source':'behavior','fallback':'agi'},
            'eyes':{'mode':'source','source':'behavior','fallback':'agi'},
            'expression':{'mode':'source','source':'behavior','fallback':'agi'},
            'mouth':{'mode':'rest'}}})
        output=self.router.evaluate(self.now)['vesna']
        self.assertQuat(raw_local(output,'Head'),axis(4))
        blends={a[0]:a[1] for addr,a in parse(output) if addr=='/VMC/Ext/Blend/Val'}
        self.assertAlmostEqual(blends['Blink_L'],.8);self.assertAlmostEqual(blends['Blink_R'],.7)
        self.assertAlmostEqual(blends['Joy'],.25);self.assertEqual(blends['A'],0)
    def test_actual_agi_integer_blend_identifiers(self):
        packet=bundle([message('/VMC/Ext/Blend/Val',2,.7),message('/VMC/Ext/Blend/Val',16,.8),message('/VMC/Ext/Blend/Val',17,.2)])
        self.router.sources['agi'].ingest(parse(packet),self.now)
        self.configure({'routes':{'mouth':{'mode':'rest'},'eyes':{'mode':'source','source':'agi','mirror':True}}})
        output=self.router.evaluate(self.now)['vesna']
        blends={a[0]:a[1] for addr,a in parse(output) if addr=='/VMC/Ext/Blend/Val'}
        self.assertEqual(blends['A'],0);self.assertAlmostEqual(blends['Blink_L'],.2);self.assertAlmostEqual(blends['Blink_R'],.8)
    def test_malformed_packet_atomicity_and_validation(self):
        source=self.router.sources['agi']
        with self.assertRaises(ValueError):source.ingest(frame()+[('/VMC/Ext/Bone/Pos',['Head',0,0,0,0,0,0,0])],self.now)
        self.assertEqual(source.bones,{})
        for invalid in (b'#bundle\0',b'#bundle\0'+bytes(range(32)),message('/VMC/Ext/T',float('nan'))):
            with self.assertRaises(ValueError):parse(invalid)
        with self.assertRaises(ValueError):self.configure({'routes':{'head':{'mode':'source','source':'ardy','source_part':'left_leg'}}})

class IntegrationTests(unittest.TestCase):
    def test_mixed_two_clients_replay_and_restart_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            hub=Hub(0,0,d).start();sockets=[]
            try:
                base=f'http://127.0.0.1:{hub.control_port}'
                for name in ['薇斯纳','沃雅妮莎']:
                    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('127.0.0.1',0));s.settimeout(2);sockets.append(s)
                    request('/api/subscribe',{'client_id':name,'name':name,'port':s.getsockname()[1]},base=base)
                p=copy.deepcopy(hub.router.profiles['vesna']);p.update(transition_ms=0,routes={'left_arm':{'mode':'source','source':'ardy'}})
                request('/api/profile',{'id':'vesna','expected_revision':0,'profile':p},base=base)
                filename=next((Path(__file__).resolve().parent.parent/'clips'/'generated').glob('ardy-*.json')).name
                request('/api/generated-action',{'actor':'vesna','file':filename,'label':'ARDY 保存动作','mask':'arms','loop':True},base=base)
                sender=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
                try:
                    data=bundle([message(address,*a) for address,a in frame({'Head':axis(30)})]);sender.sendto(data,('127.0.0.1',hub.input_port))
                    self.assertEqual(sockets[1].recv(65535),data) # untouched character stays byte-identical.
                    for _ in range(10):
                        routed=sockets[0].recv(65535)
                        if any(a[0]=='Head' for addr,a in parse(routed) if addr=='/VMC/Ext/Bone/Pos'):break
                    self.assertQuat=lambda a,b:self.assertAlmostEqual(abs(sum(x*y for x,y in zip(a,b))),1,places=5)
                    self.assertQuat(raw_local(routed,'Head'),axis(30))
                    self.assertIn('vesna',hub.router.actions)
                    self.assertEqual(hub.router.actions['vesna']['mask'],'arms')
                    self.assertEqual(Router(d).profiles['vesna']['routes'],p['routes'])
                    self.assertTrue(request('/api/action',{'actor':'vesna','command':'stop'},base=base)['stopped'])
                    self.assertNotIn('vesna',hub.router.actions)
                finally:sender.close()
            finally:
                hub.close()
                for s in sockets:s.close()

if __name__=='__main__':unittest.main(verbosity=2)
