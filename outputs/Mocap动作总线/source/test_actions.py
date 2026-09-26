import json,math,socket,tempfile,time,unittest,copy
from pathlib import Path
from pose_router import Router,parse,bundle,message,IDENTITY,PARENTS,mul,inv
from vmc_hub import Hub
from 动作订阅 import request

ROOT=Path(__file__).resolve().parent.parent
def clip(name=None):
    path=(ROOT/'clips'/'generated'/name) if name else next((ROOT/'clips'/'generated').glob('ardy-*.json'))
    return json.loads(path.read_text(encoding='utf8'))
def agi_frame(angle=0):
    q=(math.cos(angle/2),0.,0.,math.sin(angle/2));out=[];world={}
    for name in PARENTS:
        local=q if name=='LeftUpperArm' else IDENTITY
        world[name]=mul(world.get(PARENTS[name],IDENTITY),local)
        w,x,y,z=world[name];out.append(('/VMC/Ext/Bone/Pos',[name,0.,.2,0.,x,y,z,w]))
    return out
def local(packet,name):
    b={a[0]:a[1:] for address,a in parse(packet) if address=='/VMC/Ext/Bone/Pos'}
    def q(n):r=b[n];return (r[6],*r[3:6])
    p=PARENTS.get(name)
    return mul(inv(q(p)),q(name)) if p else q(name)
def same(a,b):return abs(sum(x*y for x,y in zip(a,b)))>.999

class ActionTests(unittest.TestCase):
    def test_generated_action_uses_bounded_generated_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);generated=root/'clips'/'generated';generated.mkdir(parents=True)
            (generated/'one.json').write_text(json.dumps(clip()),encoding='utf8')
            hub=Hub(0,0,None);hub.app_root=root
            result=hub.generated_action({'actor':'vodyanitsa','file':'one.json','label':'语义生成动作','mask':'full'})
            self.assertTrue(result['id'].startswith('generated-'))
            self.assertEqual(result['label'],'语义生成动作')
            with self.assertRaisesRegex(ValueError,'invalid generated clip name'):
                hub.generated_action({'actor':'vodyanitsa','file':'../one.json'})

    def test_mask_isolation_loop_and_auto_restore(self):
        r=Router();start=time.monotonic();r.sources['agi'].ingest(agi_frame(.6),start)
        c=clip();state=r.play_action('vesna','ardy-saved-a','ARDY 保存动作 A',c,mask='arms',loop=False,now=start)
        self.assertEqual(state['mask'],'arms');self.assertFalse(r.pure_agi('vesna',start))
        packets=r.evaluate(start+.3)
        self.assertIn('vesna',packets);self.assertIn('vodyanitsa',packets)
        self.assertEqual(r.states['vesna']['effective']['left_arm']['requested'],'action:vesna')
        self.assertEqual(r.states['vesna']['effective']['right_leg']['requested'],'agi')
        self.assertTrue(same(local(packets['vesna'],'RightFoot'),IDENTITY))
        self.assertTrue(same(local(packets['vodyanitsa'],'LeftUpperArm'),(math.cos(.3),0,0,math.sin(.3))))
        # A second actor can call a different tag without replacing this actor's clip.
        other=clip();r.play_action('vodyanitsa','ardy-saved-b','ARDY 保存动作 B',other,mask='upper',loop=True,now=start+.3)
        self.assertEqual(r.actions['vesna']['id'],'ardy-saved-a')
        self.assertEqual(r.actions['vodyanitsa']['id'],'ardy-saved-b')
        finished=start+len(c['frames'])/c['fps']+.1
        r.evaluate(finished)
        self.assertNotIn('vesna',r.actions);self.assertIn('vodyanitsa',r.actions)
        self.assertFalse(r.pure_agi('vesna',finished)) # transition back to baseline
        restored=finished+.5
        r.sources['agi'].ingest(agi_frame(.6),restored)
        r.evaluate(restored)
        self.assertTrue(r.pure_agi('vesna',restored))
        self.assertTrue(r.stop_action('vodyanitsa',restored)['stopped'])
        self.assertFalse(r.stop_action('vodyanitsa',restored)['stopped'])
    def test_empty_legacy_catalog_and_generated_api_send_only_selected_character(self):
        with tempfile.TemporaryDirectory() as d:
            hub=Hub(0,0,d).start();sockets=[];sender=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            try:
                base=f'http://127.0.0.1:{hub.control_port}'
                for actor in ('vesna','vodyanitsa'):
                    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.bind(('127.0.0.1',0));sock.settimeout(1);sockets.append(sock)
                    request('/api/subscribe',{'client_id':actor,'name':hub.router.profiles[actor]['label'],'port':sock.getsockname()[1]},base=base)
                status=request('/api/status',base=base)
                self.assertEqual(status['action_catalog'],[])
                self.assertEqual(set(status['action_masks']),{'full','upper','arms'})
                filename=next((ROOT/'clips'/'generated').glob('ardy-*.json')).name
                response=request('/api/generated-action',{'actor':'vesna','file':filename,'label':'ARDY 保存动作','mask':'upper','loop':True},base=base)
                self.assertEqual(response['label'],'ARDY 保存动作')
                data=bundle([message(address,*a) for address,a in agi_frame(.5)])
                sender.sendto(data,('127.0.0.1',hub.input_port))
                self.assertEqual(sockets[1].recv(65535),data) # other actor keeps exact default stream
                received=sockets[0].recv(65535)
                self.assertTrue(received.startswith(b'#bundle\0'))
                self.assertEqual(request('/api/status',base=base)['active_actions']['vesna']['mask'],'upper')
                self.assertTrue(request('/api/action',{'actor':'vesna','command':'stop'},base=base)['stopped'])
                self.assertEqual(request('/api/status',base=base)['active_actions'],{})
                self.assertEqual(request('/api/status',base=base)['profiles']['vesna']['revision'],0)
            finally:
                hub.close();sender.close()
                for sock in sockets:sock.close()

    def test_saved_ardy_action_has_pose_without_live_source(self):
        r=Router();c=clip();start=time.monotonic()
        self.assertGreater(len(c['frames']),0)
        self.assertEqual(len(c['frames'][0]),20)
        r.play_action('vesna','ardy-saved','ARDY 保存动作',c,mask='full',now=start)
        packet=r.evaluate(start+.5)['vesna']
        bones={a[0] for address,a in parse(packet) if address=='/VMC/Ext/Bone/Pos'}
        self.assertEqual(len(bones),20)
        self.assertEqual(r.states['vesna']['effective']['head']['actual'],['action:vesna'])
        self.assertNotIn('cmu',r.sources) # saved ARDY playback does not seize a shared live input

    def test_finished_action_does_not_leave_masked_pose_held(self):
        r=Router();c=clip();start=time.monotonic()
        r.play_action('vesna','ardy-once','ARDY 单次动作',c,mask='arms',now=start)
        r.evaluate(start+.3)
        self.assertIn('LeftUpperArm',r.states['vesna']['locals'])
        r.stop_action('vesna',start+.4)
        self.assertNotIn('LeftUpperArm',r.states['vesna']['locals'])
        self.assertNotIn('RightHand',r.states['vesna']['locals'])

if __name__=='__main__':unittest.main(verbosity=2)
