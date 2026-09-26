"""Fast paths for the existing receiver; no change to pose conversion or physics."""
import struct

def arm_solver(vmc):
    """Same directions as the receiver, with analytic parent propagation on its helper rig."""
    import bpy
    from mathutils import Vector
    def solve(pose,bone_map,positions):
        bpy.context.view_layer.update()
        matrices={bone.name:bone.matrix.copy() for bone in pose}
        relative={bone.name:(matrices[bone.parent.name].inverted_safe()@matrices[bone.name] if bone.parent else matrices[bone.name]) for bone in pose}
        changed={}
        def world(bone):
            if bone.name in changed:return changed[bone.name]
            if bone.parent:return world(bone.parent)@relative[bone.name]
            return matrices[bone.name]
        for index in range(len(vmc._ARM_DIRECTION_CHAINS[0])-1):
            for chain in vmc._ARM_DIRECTION_CHAINS:
                actual=bone_map.get(chain[index]);start=positions.get(chain[index]);end=positions.get(chain[index+1])
                if actual not in pose or start is None or end is None:continue
                direction=vmc._vmc_position_to_blender(end-start)
                if direction.length<1e-6:continue
                direction.normalize();bone=pose[actual]
                rest_q=bone.bone.matrix_local.to_3x3().to_quaternion().normalized()
                rest_direction=(rest_q@Vector((0,1,0))).normalized()
                desired=(rest_direction.rotation_difference(direction)@rest_q).to_matrix().to_4x4()
                desired.translation=world(bone).translation
                args={}
                if bone.parent:args={'parent_matrix':world(bone.parent),'parent_matrix_local':bone.parent.bone.matrix_local}
                bone.rotation_mode='QUATERNION'
                bone.matrix_basis=bone.bone.convert_local_to_pose(desired,bone.bone.matrix_local,invert=True,**args)
                changed[bone.name]=desired
    return solve

class FastOSC:
    def __init__(self,vmc):
        self.vmc=vmc
        self.routes={b'/VMC/Ext/Bone/Pos':(b',sfffffff',vmc._on_vmc_bone_pos),
                     b'/VMC/Ext/Root/Pos':(b',sfffffff',vmc._on_vmc_root_pos),
                     b'/VMC/Ext/Blend/Val':(b',sf',vmc._on_vmc_blend_val),
                     b'/VMC/Ext/Blend/Apply':(b',',vmc._on_vmc_blend_apply),
                     b'/VMC/Ext/T':(b',f',vmc._on_vmc_time)}
        self.names={};self.hits=0;self.fallback=0

    def dispatch(self,data):
        try:
            end=data.index(0);address=data[:end]
            route=self.routes.get(address)
            if not route:return False
            tags_at=(end+4)&~3;end_tags=data.index(0,tags_at)
            tags=data[tags_at:end_tags]
            if tags!=route[0]:return False
            offset=(end_tags+4)&~3;args=[]
            if tags.startswith(b',s'):
                end_name=data.index(0,offset);raw=data[offset:end_name]
                name=self.names.get(raw)
                if name is None:name=raw.decode('utf8');self.names[raw]=name
                args.append(name);offset=(end_name+4)&~3
            count=tags.count(b'f')
            if offset+count*4!=len(data):return False
            if count:args.extend(struct.unpack_from('>'+str(count)+'f',data,offset))
            route[1](address.decode('ascii'),*args);self.hits+=1
            return True
        except (ValueError,UnicodeError,struct.error):return False

    def poll(self):
        vmc=self.vmc
        if vmc._receiver_socket is None or vmc._dispatcher is None:return
        while True:
            try:data,address=vmc._receiver_socket.recvfrom(65535)
            except BlockingIOError:break
            except OSError:break
            if not self.dispatch(data):
                self.fallback+=1
                vmc._dispatcher.call_handlers_for_packet(data,address)

def install(vmc):
    fast=FastOSC(vmc);vmc._poll_osc_packets=fast.poll
    original_bone=vmc._find_bone_name;original_blend=vmc._find_shapekey_name
    original_rebuild=vmc._rebuild_maps
    bone_cache={};blend_cache={}
    def bone(arm,wanted,scene=None):
        if arm is None:return original_bone(arm,wanted,scene)
        key=(arm.as_pointer(),scene.as_pointer() if scene else 0,len(arm.pose.bones),wanted,
             getattr(scene,vmc._bone_override_prop_name(wanted),'') if scene else '',
             getattr(scene,'vmc_link_auto_bone_mapping',True))
        if key not in bone_cache:bone_cache[key]=original_bone(arm,wanted,scene)
        return bone_cache[key]
    def blend(face,wanted,scene=None):
        if not vmc._has_shape_keys(face):return original_blend(face,wanted,scene)
        key=(face.as_pointer(),scene.as_pointer() if scene else 0,len(face.data.shape_keys.key_blocks),wanted,
             getattr(scene,vmc._blend_override_prop_name(wanted),'') if scene else '',
             getattr(scene,'vmc_link_auto_blend_mapping',True))
        if key not in blend_cache:blend_cache[key]=original_blend(face,wanted,scene)
        return blend_cache[key]
    def rebuild(scene):
        bone_cache.clear();blend_cache.clear();return original_rebuild(scene)
    vmc._find_bone_name=bone;vmc._find_shapekey_name=blend;vmc._rebuild_maps=rebuild
    vmc._apply_arm_direction_pose=arm_solver(vmc)
    return fast
