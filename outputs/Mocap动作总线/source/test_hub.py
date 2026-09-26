import json,socket,struct,threading,time,unittest,uuid
from urllib.error import HTTPError
from vmc_hub import Hub
from pose_router import bundle
from 动作订阅 import Subscriber,request

def receiver():
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.bind(('127.0.0.1',0));sock.settimeout(1)
    return sock

def message(index):
    def string(text):
        value=text.encode()+b'\0';return value+b'\0'*((-len(value))%4)
    return string('/VMC/Ext/Bone/Pos')+string(',sfffffff')+string('Hips')+struct.pack('>7f',index*.01,1,0,0,0,0,1)

class HubTests(unittest.TestCase):
    def setUp(self):
        self.hub=Hub(0,0).start();self.base=f'http://127.0.0.1:{self.hub.control_port}'
        self.sender=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.receivers=[]
    def tearDown(self):
        self.hub.close();self.sender.close()
        for sock in self.receivers:sock.close()
    def subscribe(self,name,lease=15):
        sock=receiver();self.receivers.append(sock);client=uuid.uuid4().hex
        body={'client_id':client,'name':name,'port':sock.getsockname()[1],'topics':['agi/main'],'lease_seconds':lease}
        request('/api/subscribe',body,base=self.base)
        return sock,body
    def publish(self,data):self.sender.sendto(data,('127.0.0.1',self.hub.input_port))
    def test_fanout_order_and_idempotence(self):
        a,body=self.subscribe('薇斯纳');b,_=self.subscribe('沃雅妮莎')
        request('/api/subscribe',body,base=self.base)
        frames=[message(i) for i in range(20)]+[bundle([message(21),message(22)])]
        for frame in frames:
            self.publish(frame);self.assertEqual(a.recv(65535),frame);self.assertEqual(b.recv(65535),frame)
        self.assertEqual(len(self.hub.status()['subscribers']),2)
        a.settimeout(.08)
        with self.assertRaises(socket.timeout):a.recv(65535)
    def test_unsubscribe_and_expiry(self):
        a,body=self.subscribe('A');b,body_b=self.subscribe('B',lease=2)
        request('/api/unsubscribe',{'client_id':body['client_id']},base=self.base)
        self.publish(message(1));self.assertEqual(b.recv(65535),message(1))
        a.settimeout(.08)
        with self.assertRaises(socket.timeout):a.recv(65535)
        time.sleep(2.1);self.assertEqual(self.hub.status()['subscribers'],[])
    def test_limits_and_topic_isolation(self):
        _,body=self.subscribe('A')
        for overrides in [{'host':'8.8.8.8'},{'port':self.hub.input_port},{'topics':['wrong/topic']}]:
            with self.assertRaises(HTTPError):request('/api/subscribe',{**body,**overrides},base=self.base)
        self.publish(b'invalid packet');time.sleep(.08)
        self.assertEqual(self.hub.status()['invalid_packets'],1)
    def test_client_private_port_and_restart(self):
        class Scene(dict):
            def __getattr__(self,name):return self.get(name)
        class VMC:
            _receiver_socket=None
            def is_running(self):return self._receiver_socket is not None
            def _start_server(self,scene):
                self._receiver_socket=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
                self._receiver_socket.bind(('127.0.0.1',scene.vmc_link_port));self._receiver_socket.settimeout(1)
        vmc=VMC();scene=Scene();client=Subscriber('test-model',base=self.base,auto_start=False)
        def until(predicate,timeout=7):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                if predicate():return
                time.sleep(.05)
            self.fail('condition timed out')
        try:
            client.ensure_receiver(vmc,scene);self.receivers.append(vmc._receiver_socket)
            until(lambda:len(self.hub.status()['subscribers'])==1)
            self.assertNotEqual(scene.vmc_link_port,self.hub.input_port)
            self.publish(message(2));self.assertEqual(vmc._receiver_socket.recv(65535),message(2))
            old_id=self.hub.hub_id;ports=(self.hub.input_port,self.hub.control_port)
            self.hub.close();self.hub=Hub(*ports).start()
            until(lambda:len(self.hub.status()['subscribers'])==1)
            self.assertNotEqual(old_id,client.status()['hub_id'])
            self.publish(message(3));self.assertEqual(vmc._receiver_socket.recv(65535),message(3))
        finally:client.close()
        self.assertEqual(self.hub.status()['subscribers'],[])

if __name__=='__main__':unittest.main(verbosity=2)
