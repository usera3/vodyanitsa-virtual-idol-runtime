"""Turn an ARDY idle take into a bounded, closed-loop review candidate.

This does not approve the motion for automatic use.  It keeps ARDY's body and
arm gesture, limits risky root travel, and blends every channel back to the
first frame so a one-shot preview cannot leave the character frozen in the
last generated pose.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


ARM_SCALE = {
    "LeftShoulder": 0.80,
    "RightShoulder": 0.80,
    "LeftUpperArm": 1.00,
    "RightUpperArm": 1.00,
    "LeftLowerArm": 1.00,
    "RightLowerArm": 1.00,
    "LeftHand": 1.00,
    "RightHand": 1.00,
}
BODY_SCALE = {
    "Hips": 0.20,
    "Spine": 0.35,
    "Chest": 0.35,
    "UpperChest": 0.35,
    "Neck": 0.25,
    "Head": 0.25,
    "LeftUpperLeg": 0.18,
    "RightUpperLeg": 0.18,
    "LeftLowerLeg": 0.12,
    "RightLowerLeg": 0.12,
    "LeftFoot": 0.10,
    "RightFoot": 0.10,
}


def normalized(q: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in q))
    if length <= 1e-12:
        return [0.0, 0.0, 0.0, 1.0]
    return [value / length for value in q]


def slerp(a: list[float], b: list[float], amount: float) -> list[float]:
    a = normalized(a)
    b = normalized(b)
    dot = sum(x * y for x, y in zip(a, b))
    if dot < 0.0:
        b = [-value for value in b]
        dot = -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        return normalized([x + (y - x) * amount for x, y in zip(a, b)])
    angle = math.acos(dot)
    sine = math.sin(angle)
    left = math.sin((1.0 - amount) * angle) / sine
    right = math.sin(amount * angle) / sine
    return [left * x + right * y for x, y in zip(a, b)]


def smoothstep(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return value * value * (3.0 - 2.0 * value)


def quat_angle(a: list[float], b: list[float]) -> float:
    a = normalized(a)
    b = normalized(b)
    dot = abs(sum(x * y for x, y in zip(a, b)))
    return math.degrees(2.0 * math.acos(min(1.0, max(-1.0, dot))))


def build(source: Path, output_dir: Path) -> tuple[Path, dict]:
    clip = json.loads(source.read_text(encoding="utf-8"))
    frames = clip["frames"]
    first = frames[0]
    processed: list[dict[str, list[float]]] = []
    return_start = 0.72
    for index, frame in enumerate(frames):
        progress = index / max(1, len(frames) - 1)
        return_amount = smoothstep((progress - return_start) / (1.0 - return_start))
        cooked: dict[str, list[float]] = {}
        for bone, raw in frame.items():
            origin = first[bone]
            scale = ARM_SCALE.get(bone, BODY_SCALE.get(bone, 0.25))
            position = [origin[i] + (raw[i] - origin[i]) * scale for i in range(3)]
            rotation = slerp(origin[3:7], raw[3:7], scale)
            if return_amount:
                position = [value + (origin[i] - value) * return_amount for i, value in enumerate(position)]
                rotation = slerp(rotation, origin[3:7], return_amount)
            cooked[bone] = [round(value, 7) for value in (*position, *rotation)]
        processed.append(cooked)
    # Guarantee exact closure after float rounding.
    processed[-1] = {bone: list(raw) for bone, raw in first.items()}
    payload = {"contract": clip["contract"], "fps": clip["fps"], "frames": processed}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    output = output_dir / f"ardy-review-idle-{digest}.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    arm_bones = list(ARM_SCALE)
    arm_spans = {
        bone: max(quat_angle(first[bone][3:7], frame[bone][3:7]) for frame in processed)
        for bone in arm_bones
    }
    hips = first["Hips"]
    hip_excursion = max(
        math.sqrt(sum((frame["Hips"][i] - hips[i]) ** 2 for i in range(3))) for frame in processed
    )
    report = {
        "source": source.name,
        "file": output.name,
        "frames": len(processed),
        "fps": payload["fps"],
        "duration_seconds": round(len(processed) / float(payload["fps"]), 3),
        "max_arm_excursion_deg": round(max(arm_spans.values()), 3),
        "arm_excursion_deg": {key: round(value, 3) for key, value in arm_spans.items()},
        "hips_translation_excursion_m": round(hip_excursion, 5),
        "end_to_start_max_deg": round(
            max(quat_angle(first[bone][3:7], processed[-1][bone][3:7]) for bone in first), 6
        ),
        "human_reviewed": False,
        "safe_for_idle": False,
        "review_required": True,
        "notes": "ARDY-derived preview; arm gesture retained, root travel bounded, and every channel returns exactly to frame 0.",
    }
    return output, report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    output, report = build(args.source.resolve(), args.output_dir.resolve())
    args.report.resolve().write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
