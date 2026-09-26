"""Vesna PMX live VMC target. Run in a fresh Blender window only."""
import bpy,json,sys,time,socket,math
from pathlib import Path
from mathutils import Vector,Euler
ROOT=Path(__file__).resolve().parent
HAIR='--hair-physics' in sys.argv
PHYSICS='--skirt-physics' in sys.argv or HAIR
RUNTIME=ROOT/'头发裙摆物理版' if HAIR else (ROOT/'裙摆物理版' if PHYSICS else ROOT)
RUNTIME.mkdir(exist_ok=True)
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT.parent/'Mocap动作总线'/'source'))
from 动作订阅 import Subscriber
subscription=Subscriber('薇斯纳')
ADDONS=Path(r'C:\Users\mozi\Documents\Codex\2026-08-16\wo\work\mmd_auto\vendor\blender_user_scripts\addons')
sys.path.insert(0,str(ADDONS))
PMX=ROOT/'模型'/'Vesna'/'Vesna.pmx'
REPORT=RUNTIME/'实时连接状态.json'
LOG=RUNTIME/'实时动捕.log'
LOG.write_text('',encoding='utf8')
def log(message):
    with LOG.open('a',encoding='utf8') as f:f.write(time.strftime('%H:%M:%S ')+message+'\n')
    print(message,flush=True)
for addon in ['mmd_tools','bl_ext.user_default.vmc_link']:bpy.ops.preferences.addon_enable(module=addon)
from bl_ext.user_default.vmc_link import main as vmc
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
bpy.ops.mmd_tools.import_model(filepath=str(PMX),types={'MESH','ARMATURE','MORPHS'}|({'PHYSICS'} if PHYSICS else set()),scale=.08,fix_ik_links=True,rename_bones=False)
scene=bpy.context.scene
arm=next(o for o in scene.objects if o.type=='ARMATURE')
meshes=[o for o in scene.objects if o.type=='MESH' and len(o.data.vertices)>1000 and not getattr(o,'mmd_type','') in {'RIGID_BODY','JOINT','NON_COLLISION_CONSTRAINT'}]
face=next(o for o in meshes if o.data.shape_keys)
for bone in arm.pose.bones:
    for constraint in bone.constraints:
        if constraint.type=='IK':constraint.mute=True;constraint.influence=0
    if hasattr(bone,'mmd_ik_toggle') and ('ＩＫ' in bone.name or 'IK' in bone.name):bone.mmd_ik_toggle=False
scene.vmc_link_port=39539;scene.vmc_link_bind_mode='THIS_PC'
scene.vmc_link_bind_address_custom='127.0.0.1';scene.vmc_link_rate_hz=60
scene.vmc_link_auto_bone_mapping=True;scene.vmc_link_auto_blend_mapping=True
scene.vmc_link_apply_bone_translation=False;scene.vmc_link_armature=arm;scene.vmc_link_face_object=face
for name,shape in {'A':'あ','I':'い','U':'う','E':'え','O':'お','Blink':'まばたき','Blink_L':'ウィンク','Blink_R':'ウィンク右','Joy':'笑い','Sorrow':'困る','Angry':'怒り','Surprise':'びっくり'}.items():
    prop=vmc._blend_override_prop_name(name)
    if hasattr(scene,prop) and shape in face.data.shape_keys.key_blocks:setattr(scene,prop,shape)
vmc._rebuild_maps(scene)
log(f'Target={arm.name}, mappings={len(vmc._cached_bone_map)}, face mappings={len(vmc._cached_blend_map)}')
physics_settings={};stepper=None
if PHYSICS:
    from 裙摆物理 import build_skirt,attach_live_timer
    if HAIR:
        from 发裙物理 import build_hair_skirt,use_light_driver
        physics_settings=build_hair_skirt(arm)
        driver,transfer=use_light_driver(vmc,scene,arm)
        from 动捕加速 import install
        fast_osc=install(vmc)
        physics_settings['driver_bones']=len(driver.pose.bones)
    else:physics_settings=build_skirt(arm)
    stepper=attach_live_timer(vmc,scene,transfer=transfer if HAIR else None,profile_path=(RUNTIME/'性能剖析.txt') if '--profile' in sys.argv else None)
    log('Skirt physics: '+json.dumps(physics_settings,ensure_ascii=False))
# Same texture-first viewport as the accepted Citlali setup.
# Use a single-pass viewport antialiaser for this winged model while AGI uses
# the same GPU. This changes neither model textures nor physics settings.
aa=bpy.context.preferences.system.bl_rna.properties.get('viewport_aa')
if aa and 'FXAA' in {item.identifier for item in aa.enum_items}:
    bpy.context.preferences.system.viewport_aa='FXAA'
bpy.ops.object.select_all(action='DESELECT')
for obj in meshes:obj.select_set(True)
bpy.context.view_layer.objects.active=face
try:bpy.ops.mmd_tools.convert_materials(use_principled=True,clean_nodes=True)
except Exception as exc:log('Material conversion: '+str(exc))
bpy.context.view_layer.update()
points=[o.matrix_world@Vector(p) for o in meshes for p in o.bound_box]
low=Vector(tuple(min(v[i] for v in points) for i in range(3)))
high=Vector(tuple(max(v[i] for v in points) for i in range(3)))
center=(low+high)*.5;height=high.z-low.z
if bpy.context.screen:
    for area in bpy.context.screen.areas:
        if area.type=='VIEW_3D':
            space=area.spaces.active
            space.shading.type='SOLID';space.shading.light='FLAT';space.shading.color_type='TEXTURE'
            space.overlay.show_overlays=False
            space.region_3d.view_rotation=Euler((math.pi/2,0,0)).to_quaternion()
            space.region_3d.view_location=center;space.region_3d.view_distance=height*1.5
            space.region_3d.view_perspective='ORTHO'
bpy.ops.object.select_all(action='DESELECT')
scene['动捕角色']='薇斯纳';scene['动捕输入']='角色动作总线 → 薇斯纳部位订阅'
scene['物理状态']='原生头发、裙摆物理 + 身体碰撞' if HAIR else ('原生裙摆物理 + 身体碰撞；头发和其余饰物未模拟' if PHYSICS else '未启用衣裙物理')
bpy.ops.file.pack_all()
bpy.ops.wm.save_as_mainfile(filepath=str(RUNTIME/('薇斯纳_AGI头发裙摆物理.blend' if HAIR else ('薇斯纳_AGI裙摆物理.blend' if PHYSICS else '薇斯纳_AGI实时动捕.blend'))),compress=True)
state={'character':'薇斯纳','armature':arm.name,'port':39539,'listener':False,'connected':False,
       'incoming_changes':0,'pose_changes':0,'mesh_changes':0,'status':'正在连接'}
last_in=None;last_pose=None;last_mesh=None;last_logged='';last_hair=None;hair_changes=0
def tick():
    global last_in,last_pose,last_mesh,last_logged,last_hair,hair_changes
    try:
        # Narrow local maintenance entry: future authorized restarts can preserve
        # this exact live scene without desktop automation or losing edits.
        command=RUNTIME/'维护请求.json'
        if command.exists():
            request=json.loads(command.read_text(encoding='utf8'))
            command.unlink()
            if request.get('action') in {'snapshot','snapshot_and_exit'}:
                recovery=RUNTIME/('场景备份_'+time.strftime('%Y%m%d_%H%M%S')+'.blend')
                bpy.ops.wm.save_as_mainfile(filepath=str(recovery),copy=True,compress=True)
                log('Scene backup: '+str(recovery))
                if request['action']=='snapshot_and_exit':
                    subscription.close()
                    vmc._stop_server(scene)
                    bpy.ops.wm.quit_blender()
                    return None
        subscription.ensure_receiver(vmc,scene)
        state['port']=scene.vmc_link_port
        state['publisher_port']=39539
        state['subscription']=subscription.status()
        state['listener']=bool(vmc.is_running())
        packet_time=getattr(vmc,'_last_packet_ts',0)
        state['connected']=bool(packet_time and 0<=time.time()-packet_time<3)
        if state['listener']:
            state['status']='正在接收角色动作总线' if state['connected'] else ('已订阅，等待路由数据' if subscription.status()['subscribed'] else '正在连接动作总线')
        incoming=tuple((n,*[round(float(x),5) for x in v]) for n,v in sorted(vmc._bone_buf.items()))
        mapped=set(vmc._cached_bone_map.values())
        pose=tuple((b.name,*[round(float(x),5) for row in b.matrix for x in row]) for b in arm.pose.bones if b.name in mapped)
        evaluated=face.evaluated_get(bpy.context.evaluated_depsgraph_get())
        vertices=evaluated.data.vertices
        sampled=tuple(tuple(round(float(x),5) for x in vertices[i].co) for i in range(0,len(vertices),max(1,len(vertices)//250)))
        if state['connected']:
            if last_in is not None and incoming!=last_in:state['incoming_changes']+=1
            if last_pose is not None and pose!=last_pose:state['pose_changes']+=1
            if last_mesh is not None and sampled!=last_mesh:state['mesh_changes']+=1
        last_in,last_pose,last_mesh=incoming,pose,sampled
        state['received_bones']=len(vmc._bone_buf);state['mapped_bones']=len(vmc._cached_bone_map)
        state['received_blends']=len(vmc._blend_buf);state['mapped_blends']=len(vmc._cached_blend_map)
        state['updated_at']=time.strftime('%Y-%m-%d %H:%M:%S')
        if stepper:state['physics']={**physics_settings,**stepper.status()}
        state['viewport_aa']=bpy.context.preferences.system.viewport_aa
        if HAIR:
            head_inv=arm.pose.bones['頭'].matrix.inverted()
            hair=tuple(tuple(round(float(x),5) for row in (head_inv@b.matrix) for x in row) for b in arm.pose.bones if b.name.startswith('髪') and 'mmd_tools_rigid_track' in b.constraints)
            if last_hair is not None and hair!=last_hair:hair_changes+=1
            last_hair=hair;state['hair_motion_changes']=hair_changes
        REPORT.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf8')
        scene['实时连接状态']=state['status']
        if state['status']!=last_logged:log(state['status']);last_logged=state['status']
    except Exception as exc:log('Status error: '+repr(exc))
    return 1.0
bpy.app.timers.register(tick,first_interval=.5)
log('Vesna live target initialized')
