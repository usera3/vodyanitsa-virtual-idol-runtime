"""Vodyanitsa native PMX skirt simulation; body mocap remains authoritative."""
import bpy,time
import sys,atexit
from collections import deque
from mathutils import Quaternion
from mmd_tools.core.model import Model,FnModel

def build_skirt(arm):
    model=Model(FnModel.find_root_object(arm))
    rigids=list(model.rigidBodies())
    keep={obj for obj in rigids if obj.mmd_rigid.collision_group_number in {0,3}}
    if not any(obj.mmd_rigid.collision_group_number==3 for obj in keep):
        raise RuntimeError('未找到沃雅妮莎的裙摆刚体组，请检查是否导入 PHYSICS。')
    for obj in list(model.joints()):
        constraint=obj.rigid_body_constraint
        if not constraint or constraint.object1 not in keep or constraint.object2 not in keep:
            bpy.data.objects.remove(obj,do_unlink=True)
    for obj in rigids:
        if obj not in keep:bpy.data.objects.remove(obj,do_unlink=True)
    for obj in keep:
        group=obj.mmd_rigid.collision_group_number
        mask=[True]*16
        mask[3 if group==0 else 0]=False
        obj.mmd_rigid.collision_group_mask=mask
        obj.hide_render=True;obj.display_type='WIRE'
    bpy.context.view_layer.objects.active=arm
    bpy.ops.mmd_tools.build_rig(non_collision_distance_scale=1.5,collision_margin=.001)
    scene=bpy.context.scene;world=scene.rigidbody_world
    world.enabled=True;world.substeps_per_frame=4;world.solver_iterations=20
    scene.render.fps=30;scene.frame_start=1;scene.frame_end=1000000
    world.point_cache.frame_start=1;world.point_cache.frame_end=1000000
    scene.frame_set(1)
    return {'skirt_rigids':sum(o.mmd_rigid.collision_group_number==3 for o in keep),
            'body_colliders':sum(o.mmd_rigid.collision_group_number==0 for o in keep),
            'joints':len(list(model.joints())),'substeps':4,'solver_iterations':20,
            'collision_margin':.001,'target_fps':30}

class LiveStepper:
    def __init__(self,scene):
        self.scene=scene;self.frame=max(1,scene.frame_current)
        self.started=time.perf_counter();self.frames=0;self.last_ms=0;self.total_ms=0;self.samples=deque(maxlen=90)
        self.apply_samples=deque(maxlen=90);self.callback_samples=deque(maxlen=90)

    def step(self):
        start=time.perf_counter()
        if self.scene.frame_current!=self.frame:
            # A manually scrubbed timeline invalidates Bullet's sequential state.
            self.scene.frame_set(self.frame)
        self.frame+=1
        if self.frame>=self.scene.rigidbody_world.point_cache.frame_end-2:
            self.scene.frame_end+=1000000
            self.scene.rigidbody_world.point_cache.frame_end=self.scene.frame_end
        self.scene.frame_set(self.frame)
        self.frames+=1;self.last_ms=(time.perf_counter()-start)*1000;self.total_ms+=self.last_ms
        self.samples.append(time.perf_counter())

    def status(self):
        return {'simulation_frame':self.frame,'physics_steps':self.frames,
                'actual_fps':round((len(self.samples)-1)/max(.001,self.samples[-1]-self.samples[0]),2) if len(self.samples)>1 else 0,
                'mean_step_ms':round(self.total_ms/max(1,self.frames),2),'last_step_ms':round(self.last_ms,2),
                'vmc_apply_ms':round(sum(self.apply_samples)/max(1,len(self.apply_samples)),2),
                'total_callback_ms':round(sum(self.callback_samples)/max(1,len(self.callback_samples)),2)}

def attach_live_timer(vmc,scene,transfer=None,profile_path=None):
    if sys.platform=='win32':
        import ctypes
        class PowerState(ctypes.Structure):
            _fields_=[('Version',ctypes.c_ulong),('ControlMask',ctypes.c_ulong),('StateMask',ctypes.c_ulong)]
        # Keep this realtime process's timer request honored when its window is
        # covered by Mocap or the control panel. No persistent system change.
        power=PowerState(1,4,0)
        ctypes.windll.kernel32.SetProcessInformation.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_ulong]
        ctypes.windll.kernel32.SetProcessInformation(ctypes.c_void_p(-1),4,ctypes.byref(power),ctypes.sizeof(power))
        if ctypes.windll.winmm.timeBeginPeriod(1)==0:
            atexit.register(lambda:ctypes.windll.winmm.timeEndPeriod(1))
    original=vmc._apply_timer
    if bpy.app.timers.is_registered(original):bpy.app.timers.unregister(original)
    stepper=LiveStepper(scene)
    initial_motion_frames=0
    profiler=None;profile_frames=0
    previous_end=None;previous_delay=0.0;scheduler_overhead=0.0;deadline=None
    if profile_path:
        import cProfile
        profiler=cProfile.Profile()
    def apply_and_step():
        nonlocal initial_motion_frames,profile_frames,profiler,previous_end,previous_delay,scheduler_overhead,deadline
        start=time.perf_counter()
        if previous_end is not None:
            observed=max(0,min(.03,start-previous_end-previous_delay))
            scheduler_overhead=scheduler_overhead*.8+observed*.2
        if deadline is None or start-deadline>2/30:deadline=start
        profiling=profiler is not None and stepper.frames>=30
        if profiling:profiler.enable()
        result=original()
        stepper.apply_samples.append((time.perf_counter()-start)*1000)
        if result is None:return None
        # Ease only the initial switch from bind pose to a live moving person.
        # Subsequent motion remains exactly the receiver's pose.
        if vmc._bone_buf and initial_motion_frames<24:
            initial_motion_frames+=1;factor=initial_motion_frames/24
            arm=scene.vmc_link_armature
            # Ease only bones present in the incoming snapshot.  Iterating the
            # whole mapping also touched silent arms and multiplied their
            # calibrated idle rotations toward identity/A-pose every frame.
            received={vmc._cached_bone_map.get(vmc_name) for vmc_name in vmc._bone_buf}
            for name in {name for name in received if name}:
                bone=arm.pose.bones.get(name)
                if bone:
                    bone.rotation_quaternion=Quaternion().slerp(bone.rotation_quaternion,factor)
                    bone.location*=factor
        if transfer:transfer()
        stepper.step()
        if profiling:
            profiler.disable();profile_frames+=1
            if profile_frames==60:
                import pstats,json
                with profile_path.open('w',encoding='utf8') as handle:pstats.Stats(profiler,stream=handle).sort_stats('cumulative').print_stats(45)
                profile_path.with_suffix('.json').write_text(json.dumps(dict(vmc._bone_buf),ensure_ascii=False),encoding='utf8')
                profiler=None
        stepper.callback_samples.append((time.perf_counter()-start)*1000)
        deadline+=1/30
        previous_end=time.perf_counter()
        previous_delay=max(.001,deadline-previous_end-scheduler_overhead)
        return previous_delay
    vmc._apply_timer=apply_and_step
    if vmc.is_running():bpy.app.timers.register(apply_and_step,persistent=True)
    return stepper
