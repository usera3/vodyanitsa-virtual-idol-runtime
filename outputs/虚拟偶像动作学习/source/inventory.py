"""Inventory local motion material without copying private capture data."""
from __future__ import annotations
import hashlib,json,math
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[3]
PRODUCT=Path(__file__).resolve().parents[1]
ARDY=Path(r'C:\Users\mozi\Documents\Codex\2026-09-14\ardy-github-blender\outputs\motions')
CLIPS=ROOT/'outputs'/'Mocap动作总线'/'clips'
QWEN=Path(r'D:\AI\Models\Qwen3.8-27B-UD-IQ3_XXS\Qwen3.8-27B-UD-IQ3_XXS.gguf')
EXPORTS=[Path(r'C:\Users\mozi\Downloads\AGI_Mocap_终身测试\export.bvh'),
         Path(r'C:\Users\mozi\Downloads\export.vmd')]

def sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def inspect_ardy(path):
    with np.load(path,allow_pickle=False) as data:
        required=('posed_joints','local_rot_mats','root_positions','fps','text','joint_names')
        if any(key not in data for key in required):raise ValueError(f'incomplete ARDY clip: {path}')
        joints=data['posed_joints'];rot=data['local_rot_mats'];root=data['root_positions']
        if joints.ndim!=3 or joints.shape[1:]!=(27,3) or rot.shape!=(len(joints),27,3,3) or root.shape!=(len(joints),3):
            raise ValueError(f'unexpected ARDY shape: {path}')
        if not all(np.isfinite(x).all() for x in (joints,rot,root)):raise ValueError(f'non-finite ARDY clip: {path}')
        fps=float(data['fps']);label=str(data['text'].item()).strip()
        if not 1<=fps<=120 or not label:raise ValueError(f'invalid ARDY metadata: {path}')
        return {'path':str(path),'sha256':sha256(path),'frames':int(len(joints)),'fps':fps,
                'seconds':round(len(joints)/fps,2),'joint_count':27,'text':label,
                'has_foot_contacts':'foot_contacts' in data}

def build():
    ardy=[inspect_ardy(path) for path in sorted(ARDY.glob('*.npz'))]
    unique={clip['sha256']:clip for clip in ardy}
    catalog=json.loads((CLIPS/'action_catalog.json').read_text(encoding='utf8'))
    manifest=json.loads((CLIPS/'manifest.json').read_text(encoding='utf8'))
    actions=[]
    for entry in catalog:
        source=manifest[entry['clip']]
        actions.append({'id':entry['id'],'label':entry['label'],'tags':entry['tags'],
                        'category':entry['category'],'source':source['source'],
                        'clip_file':str(CLIPS/source['file']),
                        'frames':int(source['frames']),'fps':float(source['fps']),
                        'seconds':round(source['frames']/source['fps'],2),
                        'source_sha256':source.get('sha256')})
    exports=[{'path':str(p),'bytes':p.stat().st_size,'format':p.suffix.lower(),
              'label_status':'unlabeled','training_status':'needs_review_and_retarget'}
             for p in EXPORTS if p.exists()]
    result={'schema_version':1,'qwen':{'file':str(QWEN),'exists':QWEN.exists(),
                'bytes':QWEN.stat().st_size if QWEN.exists() else None,
                'format':'GGUF inference quantization','direct_peft_ready':False},
            'ardy':{'files':ardy,'unique_files':len(unique),
                    'unique_seconds':round(sum(c['seconds'] for c in unique.values()),2),
                    'duplicates':{digest:[c['path'] for c in ardy if c['sha256']==digest]
                                  for digest in unique if sum(c['sha256']==digest for c in ardy)>1}},
            'tagged_actions':actions,'mocap_export_candidates':exports,
            'readiness':{'actor_policy_sft':'needs_labeled_state_action_examples',
                         'direct_motion_distillation':'insufficient_paired_motion_data',
                         'mocap_to_ardy':'needs_core27_retarget_and_quality_review'}}
    return result

if __name__=='__main__':
    PRODUCT.mkdir(parents=True,exist_ok=True)
    result=build();out=PRODUCT/'素材清单.json'
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'path':str(out),'ardy_unique':result['ardy']['unique_files'],
         'ardy_unique_seconds':result['ardy']['unique_seconds'],
         'tagged_actions':len(result['tagged_actions']),
         'mocap_exports':len(result['mocap_export_candidates'])},ensure_ascii=False))
