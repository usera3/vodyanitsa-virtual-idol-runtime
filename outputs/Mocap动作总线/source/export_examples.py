"""Run with Blender --background --python ... ; uses the installed BVH importer."""
import sys,json,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import bpy,addon_utils,numpy as np
from mathutils import Matrix,Vector
from blender_publisher import F,mapping,pack_pose,sample_armature

ROOT=Path(__file__).resolve().parent.parent
ARDY_ROOT=Path(r'C:\Users\mozi\Documents\Codex\2026-09-14\ardy-github-blender\outputs\motions')
KIMODO=Path(r'C:\Users\mozi\Documents\Codex\2026-09-05\earth-online\out\walk_wave.bvh')
clips=ROOT/'clips';clips.mkdir(exist_ok=True)
manifest_path=clips/'manifest.json'
manifest=json.loads(manifest_path.read_text(encoding='utf8')) if manifest_path.exists() else {}
def save(key,kind,frames,fps,path,label):
    file=key+'.json';(clips/file).write_text(json.dumps({'contract':'agi-vmc-unity-world-v1','fps':fps,'frames':frames},separators=(',',':')),encoding='utf8')
    manifest[key]={'source':kind,'file':file,'label':label,'fps':fps,'frames':len(frames),'source_file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'mode':'replay'}

for key,filename,label in [
    ('ardy-walk-wave','walk_wave.npz','走路挥手 · ARDY 已生成'),
    ('ardy-dance-a','dance.npz','双臂舞步 A · ARDY 已生成'),
    ('ardy-dance-b','ardy_20260914_204147_1fd90a.npz','双臂舞步 B · ARDY 已生成')]:
    path=ARDY_ROOT/filename
    with np.load(path,allow_pickle=False) as motion:
        names=[str(n) for n in motion['joint_names']];m={target:names.index(native) for target,native in mapping('ardy').items()};frames=[]
        for g,p in zip(motion['global_rot_mats'],motion['posed_joints']):
            rotations={name:F@Matrix(g[i])@F.transposed() for name,i in m.items()}
            positions={name:F@Vector(p[i]) for name,i in m.items()}
            frames.append(pack_pose(rotations,positions))
        save(key,'ardy',frames,float(motion['fps']),path,label)

if not hasattr(bpy.types,'IMPORT_ANIM_OT_bvh'):
    for candidate in ['io_anim_bvh']+[m.__name__ for m in addon_utils.modules() if m.__name__.endswith('io_anim_bvh')]:
        try:addon_utils.enable(candidate,default_set=False)
        except Exception:continue
        if hasattr(bpy.types,'IMPORT_ANIM_OT_bvh'):break
if not hasattr(bpy.types,'IMPORT_ANIM_OT_bvh'):raise RuntimeError('Installed BVH importer is unavailable')
bpy.ops.import_anim.bvh(filepath=str(KIMODO),axis_forward='-Z',axis_up='Y',target='ARMATURE',global_scale=.01,frame_start=1,use_fps_scale=False,update_scene_fps=False,update_scene_duration=True)
obj=bpy.context.object;frames=[]
text=KIMODO.read_text(encoding='utf8');fps=1/float(text.split('Frame Time:')[1].splitlines()[0].strip());count=int(text.split('Frames:')[1].splitlines()[0].strip())
for i in range(1,count+1):
    bpy.context.scene.frame_set(i);bpy.context.view_layer.update();frames.append(sample_armature(obj,'kimodo'))
save('kimodo-walk-wave','kimodo',frames,fps,KIMODO,'走路挥手 · Kimodo 已生成')
manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
print('EXPORTED',json.dumps(manifest,ensure_ascii=False))
