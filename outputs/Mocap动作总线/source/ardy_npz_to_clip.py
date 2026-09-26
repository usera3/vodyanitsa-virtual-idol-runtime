"""Convert an ARDY Core npz into an AGI/VMC action-bus clip. No Blender."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from pose_router import PARENTS

F = np.diag([-1.0, 1.0, 1.0])
ARDY_MAP = {
    "Hips": "Hips",
    "Spine": "Spine",
    "Chest": "Spine3",
    "UpperChest": "Spine3",
    "Neck": "Neck",
    "Head": "Head",
}
for _side in ("Left", "Right"):
    ARDY_MAP.update(
        {
            _side + "Shoulder": _side + "Shoulder",
            _side + "UpperArm": _side + "Arm",
            _side + "LowerArm": _side + "ForeArm",
            _side + "Hand": _side + "Hand",
            _side + "UpperLeg": _side + "UpLeg",
            _side + "LowerLeg": _side + "Leg",
            _side + "Foot": _side + "Foot",
        }
    )


def _pack_pose(rotations: dict, positions: dict) -> dict:
    frame = {}
    for name in PARENTS:
        if name not in rotations:
            continue
        parent = PARENTS[name]
        position = positions[name]
        if parent in rotations:
            position = rotations[parent].T @ (position - positions[parent])
        quat = Rotation.from_matrix(rotations[name]).as_quat()
        frame[name] = [round(float(x), 7) for x in (*position, *quat)]
    return frame


def npz_to_frames(path: Path) -> tuple[list[dict], float, list[str]]:
    with np.load(path, allow_pickle=False) as motion:
        names = [str(name) for name in motion["joint_names"]]
        index = {target: names.index(native) for target, native in ARDY_MAP.items()}
        frames = []
        for global_rot, posed in zip(motion["global_rot_mats"], motion["posed_joints"]):
            rotations = {name: F @ np.asarray(global_rot[i], dtype=np.float64) @ F.T for name, i in index.items()}
            positions = {name: F @ np.asarray(posed[i], dtype=np.float64) for name, i in index.items()}
            frames.append(_pack_pose(rotations, positions))
        fps = float(motion["fps"])
        text = str(motion["text"]) if "text" in motion.files else ""
    return frames, fps, text


def write_clip(npz_path: Path, clip_path: Path) -> dict:
    frames, fps, text = npz_to_frames(npz_path)
    clip_path.write_text(
        json.dumps({"contract": "agi-vmc-unity-world-v1", "fps": fps, "frames": frames}, separators=(",", ":")),
        encoding="utf8",
    )
    return {"file": str(clip_path), "fps": fps, "frames": len(frames), "text": text, "bones": len(frames[0])}
