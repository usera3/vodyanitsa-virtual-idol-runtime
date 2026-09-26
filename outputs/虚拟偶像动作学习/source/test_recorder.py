import unittest
from record_vmc import FrameAssembler

NAMES=['Hips','Spine','Chest','UpperChest','Neck','Head',
       'LeftShoulder','LeftUpperArm','LeftLowerArm','LeftHand',
       'RightShoulder','RightUpperArm','RightLowerArm','RightHand',
       'LeftUpperLeg','LeftLowerLeg','LeftFoot','RightUpperLeg','RightLowerLeg','RightFoot']

def frame(builder,t=0):
    for name in NAMES:
        builder.ingest('/VMC/Ext/Bone/Pos',[name,0.,0.,0.,0.,0.,0.,1.],t)

class RecorderTests(unittest.TestCase):
    def test_complete_frame_and_face_opt_in(self):
        basic=FrameAssembler();frame(basic)
        basic.ingest('/VMC/Ext/Blend/Val',[7,.5],.1)
        row=basic.ingest('/VMC/Ext/OK',[1],.2)
        self.assertEqual(len(row['bones']),20)
        self.assertNotIn('blends',row)
        face=FrameAssembler(include_face=True);frame(face)
        face.ingest('/VMC/Ext/Blend/Val',[7,.5],.1)
        self.assertEqual(face.ingest('/VMC/Ext/OK',[1],.2)['blends']['Blink'],.5)
    def test_hips_repetition_and_incomplete_drop(self):
        builder=FrameAssembler();frame(builder)
        completed=builder.ingest('/VMC/Ext/Bone/Pos',['Hips',0.,0.,0.,0.,0.,0.,1.],1.)
        self.assertEqual(len(completed['bones']),20)
        self.assertIsNone(builder.finish(2.))
        self.assertEqual(builder.dropped,1)
    def test_invalid_quaternion_not_recorded(self):
        builder=FrameAssembler()
        builder.ingest('/VMC/Ext/Bone/Pos',['Hips',0.,0.,0.,0.,0.,0.,0.],0.)
        self.assertEqual(builder.bad_bones,1)
        self.assertFalse(builder.bones)

if __name__=='__main__':unittest.main()
