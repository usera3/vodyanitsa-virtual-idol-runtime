"""Convert selected credited CMU BVHs with Blender's installed BVH importer.

Run: blender --background --factory-startup --python export_cmu_actions.py
The original CMU captures are 120 Hz; export at ~30 Hz for the local bus.
"""
import sys,json,hashlib,addon_utils
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import bpy
from blender_publisher import sample_armature

ROOT=Path(__file__).resolve().parent.parent
CLIPS=ROOT/'clips';ORIGINALS=CLIPS/'source_cmu'
manifest_path=CLIPS/'manifest.json';catalog_path=CLIPS/'action_catalog.json'
manifest=json.loads(manifest_path.read_text(encoding='utf8'))
catalog=json.loads(catalog_path.read_text(encoding='utf8'))
source_manifest=json.loads((ORIGINALS/'download_manifest.json').read_text(encoding='utf8'))
SOURCE_PAGE='https://mocap.cs.cmu.edu/'
LICENSE_NOTE='CMU motion capture: free for use; no direct resale of raw data. Bruce Hahne BVH conversion adds no restrictions.'

# The label is deliberately no narrower than the original CMU trial description.
SPECS=[
 ('13_10','daily','踮脚伸手',['日常','踮脚','伸手'],'jump up to grab, reach for, tiptoe'),
 ('13_27','daily','挥手指向',['日常','挥手','指向'],'direct traffic, wave, point'),
 ('13_29','daily','热身组合',['日常','热身','转体','下蹲'],'jumping jacks, side twists, bend over, squats'),
 ('15_06','daily','前倾伸手',['日常','前倾','伸手'],'lean forward, reach for'),
 ('15_08','daily','前臂转动',['日常','手势','前臂'],'hand signals - horizontally revolve forearms'),
 ('49_18','daily','单脚平衡',['日常','平衡','单脚'],'balance on one leg, outstretched arms'),
 ('05_02','dance','抬臂转圈',['舞蹈','旋转','手臂'],'dance - expressive arms, pirouette'),
 ('05_03','dance','侧展转步',['舞蹈','侧展','转步'],'dance - sideways arabesque, turn step, folding arms'),
 ('05_07','dance','芭蕾转身',['舞蹈','芭蕾','旋转'],'dance - small jetes, attitude/arabesque, shifted-axis pirouette, turn'),
 ('05_11','dance','侧步旋转',['舞蹈','侧步','旋转'],'dance - sideways steps, pirouette'),
 ('05_12','dance','高举手臂旋身',['舞蹈','高举手臂','旋身'],'dance - arms held high, pointe tendue a terre, upper body rotation'),
 ('49_09','dance','高举双臂侧展',['舞蹈','双臂','侧展'],'dance - arms held high, side arabesque')]

if not hasattr(bpy.types,'IMPORT_ANIM_OT_bvh'):
    for candidate in ['io_anim_bvh']+[m.__name__ for m in addon_utils.modules() if m.__name__.endswith('io_anim_bvh')]:
        try:addon_utils.enable(candidate,default_set=False)
        except Exception:continue
        if hasattr(bpy.types,'IMPORT_ANIM_OT_bvh'):break
if not hasattr(bpy.types,'IMPORT_ANIM_OT_bvh'):raise RuntimeError('Blender BVH importer is unavailable')

for code,category,label,tags,description in SPECS:
    path=ORIGINALS/(code+'.bvh')
    if not path.exists():raise FileNotFoundError(path)
    raw=path.read_bytes()
    if not raw.startswith(b'HIERARCHY') or b'Frame Time:' not in raw:raise ValueError('not BVH: '+code)
    text=raw.decode('utf8');frame_count=int(text.split('Frames:')[1].splitlines()[0].strip())
    frame_time=float(text.split('Frame Time:')[1].splitlines()[0].strip())
    stride=max(1,round(1/frame_time/30));fps=round(1/frame_time/stride,3)
    bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
    bpy.ops.import_anim.bvh(filepath=str(path),axis_forward='-Z',axis_up='Y',target='ARMATURE',
        global_scale=.01,frame_start=1,use_fps_scale=False,update_scene_fps=False,update_scene_duration=True)
    arm=bpy.context.object
    if arm is None or arm.type!='ARMATURE':raise RuntimeError('missing imported armature: '+code)
    frames=[]
    for number in range(2,frame_count+1,stride): # frame 1 is Bruce Hahne's added T pose
        bpy.context.scene.frame_set(number);bpy.context.view_layer.update()
        frames.append(sample_armature(arm,'cmu'))
    if not 20<=len(frames)<=3600:raise ValueError('clip duration not supported: '+code)
    clip_id='cmu-'+code.replace('_','-');out_file=clip_id+'.json'
    (CLIPS/out_file).write_text(json.dumps({'contract':'agi-vmc-unity-world-v1','fps':fps,'frames':frames},separators=(',',':')),encoding='utf8')
    manifest[clip_id]={'source':'cmu','file':out_file,'label':label+' · CMU 动捕','fps':fps,'frames':len(frames),
        'source_file':str(path),'source_url':source_manifest[code]['url'],'source_page':SOURCE_PAGE,
        'source_description':description,'sha256':hashlib.sha256(raw).hexdigest(),'mode':'replay','license_note':LICENSE_NOTE}
    action_id=category+'-cmu-'+code.replace('_','-')
    entry={'id':action_id,'label':label,'category':category,'tags':tags,'clip':clip_id}
    catalog=[x for x in catalog if x['id']!=action_id]+[entry]
    print('EXPORTED',code,len(frames),fps,flush=True)
manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
catalog_path.write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf8')
print('DONE',len(catalog),flush=True)
