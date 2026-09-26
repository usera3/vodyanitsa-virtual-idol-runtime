"""Blender-only adapters: source skeleton -> common AGI/VMC pose.

In the generator's Blender console:
    import sys; sys.path.insert(0, <this directory>)
    import blender_publisher; blender_publisher.start('ardy')
    blender_publisher.stop()
Explicit start only; does not generate motion or change the character physics.
"""
import json,socket,time
import bpy
from mathutils import Matrix,Vector
from pose_router import PARENTS,message,bundle

U=Matrix(((-1,0,0),(0,0,1),(0,-1,0))) # Blender Z-up -> Unity Y-up, left is -X.
F=Matrix(((-1,0,0),(0,1,0),(0,0,1))) # Native ARDY/SOMA Y-up -> Unity.

def mapping(kind):
    if kind=='cmu':
        out={'Hips':'Hips','Spine':'Spine','Chest':'Spine1','UpperChest':'Spine1','Neck':'Neck1','Head':'Head'}
        for side in ('Left','Right'):
            out.update({side+'Shoulder':side+'Shoulder',side+'UpperArm':side+'Arm',
                        side+'LowerArm':side+'ForeArm',side+'Hand':side+'Hand',
                        side+'UpperLeg':side+'UpLeg',side+'LowerLeg':side+'Leg',side+'Foot':side+'Foot'})
        return out
    out={'Hips':'Hips','Spine':'Spine' if kind=='ardy' else 'Spine1',
         'Chest':'Spine3' if kind=='ardy' else 'Chest','UpperChest':'Spine3' if kind=='ardy' else 'Chest',
         'Neck':'Neck' if kind=='ardy' else 'Neck1','Head':'Head'}
    for side in ('Left','Right'):
        for target,source in [('Shoulder','Shoulder'),('UpperArm','Arm'),('LowerArm','ForeArm'),('Hand','Hand'),
                              ('UpperLeg','UpLeg' if kind=='ardy' else 'Leg'),('LowerLeg','Leg' if kind=='ardy' else 'Shin'),('Foot','Foot')]:out[side+target]=side+source
    return out

def pack_pose(rotations,positions):
    frame={}
    for name in PARENTS:
        if name not in rotations:continue
        parent=PARENTS[name];position=positions[name]
        if parent in rotations:position=rotations[parent].transposed()@(position-positions[parent])
        q=rotations[name].to_quaternion().normalized()
        frame[name]=[round(float(x),7) for x in (*position,q.x,q.y,q.z,q.w)]
    return frame

def sample_armature(obj,kind):
    m=mapping(kind)
    if kind=='ardy':
        names=['Hips','Spine','Spine1','Spine2','Spine3','Neck','Head','RightShoulder','RightArm','RightForeArm','RightHand','RightHandEnd','RightHandThumb1','LeftShoulder','LeftArm','LeftForeArm','LeftHand','LeftHandEnd','LeftHandThumb1','RightUpLeg','RightLeg','RightFoot','RightToeBase','LeftUpLeg','LeftLeg','LeftFoot','LeftToeBase']
        m={target:'A27_'+str(names.index(native)).zfill(2) for target,native in m.items()}
    rotations={};positions={}
    for name,native in m.items():
        pb=obj.pose.bones.get(native)
        if pb is None:raise ValueError('Source skeleton missing '+native)
        rest=obj.matrix_world@pb.bone.matrix_local;pose=obj.matrix_world@pb.matrix
        delta=pose.to_3x3().normalized()@rest.to_3x3().normalized().transposed()
        rotations[name]=U@delta@U.transposed();positions[name]=U@pose.translation
    return pack_pose(rotations,positions)

_state=None
def stop():
    global _state
    if _state:
        if bpy.app.timers.is_registered(_tick):bpy.app.timers.unregister(_tick)
        _state['socket'].close()
        for obj,hidden in _state['visibility'].values():
            try:obj.hide_viewport=hidden
            except ReferenceError:pass
    _state=None

def _tick():
    if not _state:return None
    state=_state;obj=bpy.data.objects.get(state['object']);scene=bpy.context.scene
    if obj is None:return .2
    # ARDY live source has no action; finished/hidden ARDY must not send stale poses.
    if state['kind']=='ardy' and obj.hide_viewport:return .2
    # Kimodo's launcher disables source evaluation after retarget. Temporarily
    # enable dependency evaluation (still hidden with hide_set) while publishing.
    if obj.name not in state['visibility']:
        state['visibility'][obj.name]=(obj,obj.hide_viewport)
    if state['kind']=='kimodo' and obj.hide_viewport:obj.hide_viewport=False
    bpy.context.view_layer.update()
    try:
        frame=sample_armature(obj,state['kind'])
        data=bundle([message('/VMC/Ext/Bone/Pos',n,*v) for n,v in frame.items()]+[message('/VMC/Ext/OK',1)])
        state['socket'].sendto(data,('127.0.0.1',state['port']))
        scene['motion_bus_publisher']=state['kind']+' · 外部 VMC 输入（动作可能来自时间轴）'
    except Exception as exc:scene['motion_bus_publisher']='Error: '+str(exc)
    return 1/30

def start(kind,object_name=None,port=None):
    global _state
    if kind not in ('ardy','kimodo'):raise ValueError('ardy or kimodo required')
    stop();sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.setblocking(False)
    _state={'kind':kind,'object':object_name or ('ARDY_Source' if kind=='ardy' else 'Kimodo_Source'),
            'port':port or (39542 if kind=='ardy' else 39541),'socket':sock,'visibility':{}}
    bpy.app.timers.register(_tick,first_interval=.1)
    return 'Publishing '+kind+' to '+str(_state['port'])+'; this does not start a generator.'
