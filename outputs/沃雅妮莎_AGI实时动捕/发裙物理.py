"""Native hair/skirt dynamics with compact, verified collision filtering."""
import bpy,time
from collections import defaultdict
from mmd_tools.core.model import Model,FnModel
from 裙摆物理 import LiveStepper

def compact_collision_filters(rigids):
    constraints=[o for o in bpy.context.scene.objects if getattr(o,'mmd_type','')=='NON_COLLISION_CONSTRAINT']
    neighbors=defaultdict(set)
    dynamic={o for o in rigids if o.mmd_rigid.type!='0'}
    for obj in constraints:
        c=obj.rigid_body_constraint;a,b=c.object1,c.object2
        if a in dynamic and b in dynamic and a.mmd_rigid.collision_group_number==b.mmd_rigid.collision_group_number:
            neighbors[a].add(b);neighbors[b].add(a)
    colors={}
    for group,palette in [(2,range(0,10)),(3,range(10,20))]:
        pending={o for o in dynamic if o.mmd_rigid.collision_group_number==group}
        while pending:
            obj=max(pending,key=lambda o:(len({colors[n] for n in neighbors[o] if n in colors}),len(neighbors[o]),o.name))
            used=[colors[n] for n in neighbors[obj] if n in colors]
            colors[obj]=min(palette,key=lambda color:(used.count(color),color))
            pending.remove(obj)
    for obj in rigids:
        bits=[False]*20
        if obj in colors:bits[colors[obj]]=True
        elif obj.mmd_rigid.collision_group_number==0:bits=[True]*20
        obj.rigid_body.collision_collections=bits
    removed=0
    for obj in constraints:
        c=obj.rigid_body_constraint;a,b=c.object1,c.object2
        overlap=any(x and y for x,y in zip(a.rigid_body.collision_collections,b.rigid_body.collision_collections))
        if not overlap or (a.rigid_body.kinematic and b.rigid_body.kinematic):
            bpy.data.objects.remove(obj,do_unlink=True);removed+=1
    bodies=[o for o in rigids if o.mmd_rigid.collision_group_number==0]
    assert all(any(x and y for x,y in zip(a.rigid_body.collision_collections,b.rigid_body.collision_collections)) for a in bodies for b in dynamic)
    return {'filter_constraints_before':len(constraints),'filter_constraints_removed':removed,'filter_constraints_remaining':len(constraints)-removed}

def build_hair_skirt(arm):
    model=Model(FnModel.find_root_object(arm));rigids=list(model.rigidBodies())
    keep={o for o in rigids if o.mmd_rigid.collision_group_number in {0,2,3}}
    hair_bones={o.mmd_rigid.bone for o in keep if o.mmd_rigid.collision_group_number==2}
    anchors=[]
    for obj in keep:
        if obj.mmd_rigid.collision_group_number!=2 or obj.mmd_rigid.type=='0':continue
        bone=arm.data.bones.get(obj.mmd_rigid.bone)
        if bone and (not bone.parent or bone.parent.name not in hair_bones):
            obj.mmd_rigid.type='0';anchors.append(bone.name)
    for obj in list(model.joints()):
        c=obj.rigid_body_constraint
        if not c or c.object1 not in keep or c.object2 not in keep:bpy.data.objects.remove(obj,do_unlink=True)
    for obj in rigids:
        if obj not in keep:bpy.data.objects.remove(obj,do_unlink=True)
    for obj in keep:
        group=obj.mmd_rigid.collision_group_number;mask=[True]*16
        if group==0:mask[2]=mask[3]=False
        elif obj.mmd_rigid.type!='0':mask[0]=False
        obj.mmd_rigid.collision_group_mask=mask;obj.hide_render=True;obj.display_type='WIRE'
    bpy.context.view_layer.objects.active=arm
    bpy.ops.mmd_tools.build_rig(non_collision_distance_scale=1.5,collision_margin=.001)
    filters=compact_collision_filters(keep)
    scene=bpy.context.scene;world=scene.rigidbody_world
    world.enabled=True;world.substeps_per_frame=2;world.solver_iterations=20
    scene.render.fps=30;scene.frame_start=1;scene.frame_end=1000000
    world.point_cache.frame_start=1;world.point_cache.frame_end=1000000;scene.frame_set(1)
    return {'hair_rigids':sum(o.mmd_rigid.collision_group_number==2 for o in keep),
            'skirt_rigids':sum(o.mmd_rigid.collision_group_number==3 for o in keep),'body_colliders':sum(o.mmd_rigid.collision_group_number==0 for o in keep),
            'joints':len(list(model.joints())),'substeps':2,'solver_iterations':20,'collision_margin':.001,'target_fps':30,'anchored_hair_roots':anchors,
            'scene_objects':len(scene.objects),**filters}

def make_light_driver(arm,needed=None):
    """Evaluate the existing exact MMD arm solver without re-skinning each stage."""
    helper=arm.copy();helper.data=arm.data.copy();helper.name='Vodyanitsa_动捕驱动骨架'
    bpy.context.scene.collection.objects.link(helper)
    helper.animation_data_clear();helper.parent=None;helper.matrix_world=arm.matrix_world.copy()
    helper.hide_render=True;helper.hide_viewport=False;helper.hide_set(False)
    for collection in helper.data.collections:collection.is_visible=False
    for bone in helper.pose.bones:
        for constraint in list(bone.constraints):
            if constraint.name=='mmd_tools_rigid_track':bone.constraints.remove(constraint)
            elif hasattr(constraint,'target') and constraint.target==arm:constraint.target=helper
    if needed:
        keep=set(needed)
        pending=list(keep)
        while pending:
            name=pending.pop();bone=helper.pose.bones.get(name)
            if not bone:continue
            linked=([bone.parent.name] if bone.parent else [])+[c.subtarget for c in bone.constraints if hasattr(c,'subtarget') and getattr(c,'target',None)==helper and c.subtarget]
            for dependency in linked:
                if dependency not in keep:keep.add(dependency);pending.append(dependency)
        previous=bpy.context.view_layer.objects.active
        bpy.context.view_layer.objects.active=helper;helper.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        for bone in list(helper.data.edit_bones):
            if bone.name not in keep:helper.data.edit_bones.remove(bone)
        bpy.ops.object.mode_set(mode='OBJECT');helper.select_set(False)
        bpy.context.view_layer.objects.active=previous
    return helper

def use_light_driver(vmc,scene,arm):
    helper=make_light_driver(arm,set(vmc._cached_bone_map.values()))
    scene.vmc_link_armature=helper
    vmc._rebuild_maps(scene)
    def transfer():
        for name in set(vmc._cached_bone_map.values()):
            source=helper.pose.bones.get(name);target=arm.pose.bones.get(name)
            if source and target:
                target.rotation_mode=source.rotation_mode
                target.matrix_basis=source.matrix_basis.copy()
        arm.location=helper.location.copy();arm.rotation_mode=helper.rotation_mode
        arm.rotation_quaternion=helper.rotation_quaternion.copy()
    return helper,transfer
