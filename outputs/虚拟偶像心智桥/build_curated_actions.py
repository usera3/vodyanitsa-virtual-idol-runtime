"""Build deterministic ARDY-derived short skills without playing them live."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from mind_action_bridge import ArdyGenerator, ArdyLibrary, StatusWriter, load_config


ACTIONS = [
    ("idle_weight_shift", "待机·左右换重心", "idle", 4.0, "full", True,
     "A person stands naturally in place, slowly shifts body weight to the left foot, returns to center, shifts weight to the right foot, returns to center, relaxes, and finishes in a neutral standing posture."),
    ("idle_head_tilt", "待机·轻微偏头", "idle", 3.0, "upper", True,
     "A person stands still, gently tilts the head a little to the left with relaxed shoulders, pauses briefly, brings the head back upright, and finishes in a neutral standing posture."),
    ("idle_look_around", "待机·左右看看", "idle", 3.5, "upper", True,
     "A person stands calmly, turns the head slightly to look left, returns to center, looks slightly right, returns to center, and finishes facing forward in a neutral standing posture."),
    ("idle_small_nod", "待机·轻点头", "idle", 2.5, "upper", True,
     "A person stands calmly, lowers the head into one small gentle nod, raises the head back to center, relaxes the neck and shoulders, and finishes in a neutral standing posture."),
    ("idle_lower_head", "待机·低头再抬起", "idle", 3.0, "upper", True,
     "A person stands quietly, slowly lowers the head as if thinking, pauses briefly, gently raises the head to face forward again, and finishes in a neutral standing posture."),
    ("idle_relax_shoulders", "待机·放松肩膀", "idle", 3.0, "upper", True,
     "A person stands naturally, lifts both shoulders slightly, rolls them gently backward, lowers and relaxes both shoulders, lets both arms settle, and finishes in a neutral standing posture."),
    ("idle_deep_breath", "待机·深呼吸", "idle", 4.0, "upper", True,
     "A person stands calmly, slowly expands the chest while taking a deep breath, raises the shoulders only slightly, exhales, lets the chest and shoulders relax, and finishes in a neutral standing posture."),
    ("idle_shy_posture", "待机·羞涩侧头", "idle", 3.5, "upper", True,
     "A person stands in place, gently lowers and tilts the head to one side with a shy posture, draws both elbows slightly inward, pauses, then relaxes and returns to a neutral standing posture."),
    ("idle_gentle_sway", "待机·轻柔摇曳", "idle", 4.5, "full", True,
     "A person stands with both feet planted, makes one slow gentle sway of the torso to the left and right with very small knee bends, settles the weight evenly, and finishes in a neutral standing posture."),
    ("idle_hand_relax", "待机·手腕放松", "idle", 3.0, "arms", True,
     "A person stands still with arms near the sides, gently loosens both wrists and hands with one small outward and inward motion, lowers both hands, and finishes with relaxed arms in a neutral posture."),
    ("wave_right", "对话·右手挥手", "conversation", 3.5, "arms", False,
     "A person stands in place, raises the right hand beside the shoulder, waves the forearm clearly from side to side two times, lowers the right arm smoothly, and finishes in a neutral standing posture."),
    ("wave_left", "对话·左手挥手", "conversation", 3.5, "arms", False,
     "A person stands in place, raises the left hand beside the shoulder, waves the forearm clearly from side to side two times, lowers the left arm smoothly, and finishes in a neutral standing posture."),
    ("agree_nod", "对话·肯定点头", "conversation", 2.8, "upper", True,
     "A person stands facing forward, performs two clear but gentle nods, relaxes the head and shoulders, and finishes in a neutral standing posture."),
    ("disagree_shake_head", "对话·轻轻摇头", "conversation", 3.0, "upper", True,
     "A person stands facing forward, gently turns the head left and right in one clear disagreement gesture, returns the head to center, and finishes in a neutral standing posture."),
    ("explain_right_palm", "对话·右手解释", "conversation", 4.0, "arms", False,
     "A person stands in place, raises the right forearm with the palm open, makes one calm outward explanatory gesture, draws the hand back, lowers the arm, and finishes in a neutral standing posture."),
    ("explain_both_palms", "对话·双手解释", "conversation", 4.5, "arms", False,
     "A person stands in place, raises both forearms with open palms, moves both hands outward in one clear explanatory gesture, brings them back, lowers both arms, and finishes in a neutral standing posture."),
    ("point_left", "对话·指向左侧", "conversation", 3.5, "arms", False,
     "A person stands facing forward, raises the right arm and points clearly toward the left side, holds briefly, retracts and lowers the arm, and finishes in a neutral standing posture."),
    ("point_right", "对话·指向右侧", "conversation", 3.5, "arms", False,
     "A person stands facing forward, raises the left arm and points clearly toward the right side, holds briefly, retracts and lowers the arm, and finishes in a neutral standing posture."),
    ("shrug", "对话·轻耸肩", "conversation", 3.0, "upper", True,
     "A person stands facing forward, lifts both shoulders and slightly turns both open palms outward in one small shrug, lowers the shoulders and arms, and finishes in a neutral standing posture."),
    ("clap_twice", "情绪·轻拍手两次", "emotion", 3.5, "arms", False,
     "A person stands in place, brings both hands together for two gentle claps in front of the chest, separates and lowers both hands, and finishes in a neutral standing posture."),
    ("polite_bow", "礼仪·鞠躬", "social", 4.0, "full", False,
     "A person stands with both feet planted, bends the upper body forward into one clear polite bow, holds briefly, rises upright, settles balance, and finishes in a neutral standing posture."),
    ("think_chin", "对话·托下巴思考", "conversation", 4.0, "arms", False,
     "A person stands facing forward, raises the right hand near the chin in a brief thinking gesture, slightly tilts the head, lowers the hand smoothly, and finishes in a neutral standing posture."),
    ("hands_clasp", "情绪·双手轻合", "emotion", 4.0, "arms", False,
     "A person stands in place, gently brings both hands together near the chest in a shy reserved gesture, pauses briefly, separates and lowers both hands, and finishes in a neutral standing posture."),
    ("small_celebration", "情绪·小小庆祝", "emotion", 4.0, "upper", False,
     "A person stands in place, raises both forearms in a small happy celebration, makes one light upward bounce of the arms without jumping, lowers both arms, and finishes in a neutral standing posture."),
    ("walk_in_place", "运动·原地走路", "locomotion", 5.0, "full", False,
     "A person walks visibly in place for four alternating steps, coordinates opposite arm swings, keeps the body near the starting point, slows down, plants both feet, and finishes in a neutral standing posture."),
    ("jog_in_place", "运动·原地跑步", "locomotion", 6.0, "full", False,
     "A person jogs visibly in place with alternating knee lifts and coordinated opposite arm swings, keeps both feet near the starting point, gradually slows down, plants both feet, lowers both arms, and finishes in a neutral standing posture."),
    ("side_step", "运动·左右侧步", "locomotion", 5.0, "full", False,
     "A person steps once to the left and brings the feet together, steps once to the right and brings the feet together, settles weight at the center, and finishes in a neutral standing posture."),
    ("squat_return", "运动·下蹲再站起", "exercise", 5.0, "full", False,
     "A person stands with feet planted, bends both knees and hips into one controlled shallow squat, pauses briefly, rises smoothly to standing, regains balance, and finishes in a neutral standing posture."),
    ("overhead_stretch", "运动·举臂伸展", "exercise", 5.0, "full", False,
     "A person stands with feet planted, raises both arms overhead into a gentle full-body stretch, lengthens the torso, lowers both arms smoothly to the sides, relaxes, and finishes in a neutral standing posture."),
    ("turn_left_return", "姿态·左转再回正", "posture", 4.0, "full", False,
     "A person stands in place, rotates the torso and head moderately to the left while keeping the feet planted, looks briefly, rotates back to face forward, and finishes in a neutral standing posture."),
    ("turn_right_return", "姿态·右转再回正", "posture", 4.0, "full", False,
     "A person stands in place, rotates the torso and head moderately to the right while keeping the feet planted, looks briefly, rotates back to face forward, and finishes in a neutral standing posture."),
    ("dance_step_touch", "舞蹈·左右踏步", "dance", 7.0, "full", False,
     "A person performs a clear step-touch dance in place, steps left and touches the right foot in, steps right and touches the left foot in, repeats the pattern with broad gentle arm arcs, slows down, lowers both arms, and finishes in a neutral standing posture."),
]


GROUPS = {
    "idle_calm": ["idle_weight_shift", "idle_look_around", "idle_deep_breath"],
    "idle_shy": ["idle_lower_head", "idle_shy_posture", "idle_gentle_sway"],
    "greeting_warm": ["idle_small_nod", "wave_right", "idle_relax_shoulders"],
    "conversation_explain": ["explain_right_palm", "agree_nod"],
    "conversation_think": ["think_chin", "idle_lower_head"],
    "mini_warmup": ["idle_relax_shoulders", "overhead_stretch", "squat_return"],
    "movement_in_place": ["walk_in_place", "side_step", "jog_in_place"],
    "dance_simple": ["idle_gentle_sway", "dance_step_touch"],
}


def quat_angle(a: list[float], b: list[float]) -> float:
    qa = [a[6], a[3], a[4], a[5]]
    qb = [b[6], b[3], b[4], b[5]]
    na = math.sqrt(sum(value * value for value in qa))
    nb = math.sqrt(sum(value * value for value in qb))
    dot = abs(sum(x * y for x, y in zip(qa, qb)) / max(1e-8, na * nb))
    return math.degrees(2.0 * math.acos(min(1.0, dot)))


def inspect_clip(path: Path, idle_candidate: bool) -> dict:
    clip = json.loads(path.read_text(encoding="utf-8"))
    first, last = clip["frames"][0], clip["frames"][-1]
    spans = {}
    endings = {}
    for bone, initial in first.items():
        values = [frame[bone] for frame in clip["frames"] if bone in frame]
        spans[bone] = max(quat_angle(initial, value) for value in values)
        endings[bone] = quat_angle(initial, last.get(bone, initial))
    arm = max((spans.get(name, 0.0) for name in (
        "LeftUpperArm", "RightUpperArm", "LeftLowerArm", "RightLowerArm",
        "LeftHand", "RightHand")), default=0.0)
    leg = max((spans.get(name, 0.0) for name in (
        "LeftUpperLeg", "RightUpperLeg", "LeftLowerLeg", "RightLowerLeg")), default=0.0)
    max_span = max(spans.values(), default=0.0)
    end_max = max(endings.values(), default=0.0)
    numeric_pass = bool(idle_candidate and arm <= 95.0 and leg <= 50.0 and max_span <= 135.0 and end_max <= 40.0)
    return {
        "frames": len(clip["frames"]), "fps": clip["fps"],
        "max_rotation_deg": round(max_span, 2),
        "max_arm_rotation_deg": round(arm, 2),
        "max_leg_rotation_deg": round(leg, 2),
        "end_to_start_max_deg": round(end_max, 2),
        "numeric_idle_candidate_passed": numeric_pass,
        "safe_for_idle": False,
        "review_required": True,
    }


def main() -> None:
    base = Path(__file__).resolve().parent
    config = load_config(base / "config.json")
    status = StatusWriter(base / "动作库构建状态.json")
    generator = ArdyGenerator(config["ardy"], base, status)
    library = ArdyLibrary((base / config["ardy"]["library_file"]).resolve())
    built = []
    try:
        generator.preload()
        for index, (key, label, category, duration, mask, idle_candidate, prompt) in enumerate(ACTIONS):
            started = time.perf_counter()
            generated = generator.generate(prompt, duration, "curated:" + key, seed=2026092600 + index)
            plan = {"ardy_prompt": prompt, "duration_seconds": duration, "mask": mask,
                    "assistant_action_text": ""}
            item = library.record(generated, plan, label)
            quality = inspect_clip(Path(generated["clip"]), idle_candidate)
            built.append({"key": key, "label": label, "category": category,
                          "idle_candidate": idle_candidate, "id": item["id"],
                          "file": item["file"], "duration_seconds": duration,
                          "mask": mask, "prompt": prompt, "seed": generated["seed"],
                          "build_seconds": round(time.perf_counter() - started, 3),
                          "quality": quality})
            print(json.dumps({"built": index + 1, "total": len(ACTIONS),
                              "key": key, "seconds": built[-1]["build_seconds"],
                              "idle_safe": quality["safe_for_idle"]}, ensure_ascii=False), flush=True)
    finally:
        generator.close()

    by_id = {row["id"]: row for row in built}
    raw = json.loads(library.path.read_text(encoding="utf-8"))
    for item in raw["actions"]:
        meta = by_id.get(item.get("id"))
        if not meta:
            continue
        item.update({
            "curated_key": meta["key"], "category": meta["category"],
            "tags": ["ARDY", "预生成", meta["category"], meta["label"]],
            "entry_pose": "neutral_standing", "exit_pose": "neutral_standing",
            "transition": {"blend_in_ms": 250, "release_to_neutral_ms": 600},
            "max_repeats": 1, "cooldown_seconds": 20,
            "idle_candidate": meta["idle_candidate"],
            "human_reviewed": False,
            "safe_for_idle": False,
            "review_required": True,
            "quality": meta["quality"],
        })
    temporary = library.path.with_suffix(".tmp")
    temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(library.path)

    key_to_id = {row["key"]: row["id"] for row in built}
    groups = {
        "version": 1, "transition": "neutral_release", "gap_ms": 650,
        "groups": [
            {"id": group, "actions": [key_to_id[key] for key in keys],
             "keys": keys, "max_repeats": 1}
            for group, keys in GROUPS.items()
        ],
    }
    output = base.parent / "Mocap动作总线" / "clips" / "generated"
    (output / "curated_catalog.json").write_text(
        json.dumps({"version": 1, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "actions": built}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "action_groups.json").write_text(
        json.dumps(groups, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "total": len(built),
        "idle_candidates": sum(row["idle_candidate"] for row in built),
        "idle_safe": sum(row["quality"]["safe_for_idle"] for row in built),
        "review_required": sum(row["quality"]["review_required"] for row in built),
        "categories": sorted({row["category"] for row in built}),
        "groups": list(GROUPS),
        "actions": built,
    }
    (base / "预生成动作库报告.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "total", "idle_candidates", "idle_safe", "review_required", "groups")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
