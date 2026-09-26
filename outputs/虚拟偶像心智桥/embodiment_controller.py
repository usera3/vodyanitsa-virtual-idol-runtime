"""Deterministic 30 Hz humanoid micro-motion and blink publisher."""

from __future__ import annotations

import json
import math
import random
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any


IDENTITY = (1.0, 0.0, 0.0, 0.0)
PARENTS = {
    "Hips": None,
    "Spine": "Hips",
    "Chest": "Spine",
    "UpperChest": "Chest",
    "Neck": "Chest",
    "Head": "Neck",
}
for side in ("Left", "Right"):
    PARENTS.update({
        side + "Shoulder": "UpperChest",
        side + "UpperArm": side + "Shoulder",
        side + "LowerArm": side + "UpperArm",
        side + "Hand": side + "LowerArm",
        side + "UpperLeg": "Hips",
        side + "LowerLeg": side + "UpperLeg",
        side + "Foot": side + "LowerLeg",
    })

# Unity humanoid local offsets used by the installed VMC receiver to rebuild
# visible arm directions.  Supplying zero offsets leaves the arm chain silent;
# supplying identity rotations with these offsets produces the imported A-pose.
BONE_OFFSETS = {
    "Hips": (0.0, 0.951, 0.0),
    "Spine": (0.0, 0.0709891, -0.0473261),
    "Chest": (0.0, 0.28193, -0.0200),
    "UpperChest": (0.0, 0.0, 0.0),
    "Neck": (0.0, 0.248462, 0.0354944),
    "Head": (0.0, 0.1281699, 0.0225997),
    "LeftShoulder": (-0.0319949, 0.172772, 0.0533246),
    "LeftUpperArm": (-0.158908, 0.0, 0.0),
    "LeftLowerArm": (-0.295436, 0.0, 0.0),
    "LeftHand": (-0.232652, 0.0, 0.0),
    "RightShoulder": (0.0319949, 0.172772, 0.0533246),
    "RightUpperArm": (0.158908, 0.0, 0.0),
    "RightLowerArm": (0.295436, 0.0, 0.0),
    "RightHand": (0.232652, 0.0, 0.0),
    "LeftUpperLeg": (-0.0949182, -0.0277289, 0.0),
    "LeftLowerLeg": (0.0, -0.412118, 0.0),
    "LeftFoot": (0.0, -0.456091, 0.0),
    "RightUpperLeg": (0.0949182, -0.0277289, 0.0),
    "RightLowerLeg": (0.0, -0.412118, 0.0),
    "RightFoot": (0.0, -0.456091, 0.0),
    "LeftEye": (0.0, 0.0, 0.0),
    "RightEye": (0.0, 0.0, 0.0),
}


def mul(a, b):
    w, x, y, z = a
    v, i, j, k = b
    return (
        w * v - x * i - y * j - z * k,
        w * i + x * v + y * k - z * j,
        w * j - x * k + y * v + z * i,
        w * k + x * j - y * i + z * v,
    )


def axis_angle(axis: tuple[float, float, float], degrees: float):
    angle = math.radians(degrees) / 2.0
    s = math.sin(angle)
    return (math.cos(angle), axis[0] * s, axis[1] * s, axis[2] * s)


def euler(pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0):
    return mul(mul(axis_angle((1, 0, 0), pitch), axis_angle((0, 1, 0), yaw)), axis_angle((0, 0, 1), roll))


def rotate_vec(quat, vec):
    w, x, y, z = quat
    vx, vy, vz = vec
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def normalize(vec):
    length = math.sqrt(sum(component * component for component in vec)) or 1.0
    return tuple(component / length for component in vec)


def warped_sin(elapsed: float, hz: float, phase: float, inhale_frac: float = 0.58) -> float:
    """Slower inhale, faster exhale. `phase` is radians."""
    cycle = (elapsed * hz + phase / math.tau) % 1.0
    if cycle < inhale_frac:
        shaped = 0.5 * (cycle / max(inhale_frac, 1e-6))
    else:
        shaped = 0.5 + 0.5 * ((cycle - inhale_frac) / max(1.0 - inhale_frac, 1e-6))
    return math.sin(shaped * math.tau)


def envelope(phase: float) -> float:
    if not 0.0 <= phase <= 1.0:
        return 0.0
    return math.sin(math.pi * phase)


# Verified Vodyanitsa rest: ~12° out from vertical so both hands stay visible.
ARM_BASE_DEGREES = 78.0
HIP_BIAS_ROLL = 0.38
SHOULDER_BIAS = 0.28
WEIGHT_PERIOD = 6.4

IDLE_CUE_WEIGHTS = (
    ("look", 0.20),
    ("look_around", 0.16),
    ("weight_settle", 0.14),
    ("head_tilt", 0.10),
    ("nod", 0.08),
    ("shoulder_roll", 0.08),
    ("lower_head", 0.06),
    ("smile", 0.05),
    ("breath", 0.05),
    ("wrist_ease", 0.04),
    ("shy", 0.02),
    ("relax", 0.02),
)
LISTENING_CUES = ("look", "nod", "head_tilt", "breath", "smile")


def osc_string(value: str) -> bytes:
    data = value.encode("utf-8") + b"\0"
    return data + b"\0" * (-len(data) % 4)


def osc_message(address: str, *args: object) -> bytes:
    tags = ","
    body = b""
    for arg in args:
        if isinstance(arg, str):
            tags += "s"
            body += osc_string(arg)
        elif isinstance(arg, int):
            tags += "i"
            body += struct.pack(">i", arg)
        else:
            tags += "f"
            body += struct.pack(">f", float(arg))
    return osc_string(address) + osc_string(tags) + body


def osc_bundle(messages: list[bytes]) -> bytes:
    return b"#bundle\0" + struct.pack(">Q", 1) + b"".join(
        struct.pack(">I", len(item)) + item for item in messages
    )


class EmbodimentController:
    def __init__(self, raw: dict[str, Any] | None, base_dir: Path, *, autostart: bool = True):
        raw = raw or {}
        self.enabled = bool(raw.get("enabled", False))
        self.port = int(raw.get("port", 39544))
        self.fps = max(10.0, min(60.0, float(raw.get("fps", 30.0))))
        self.rng = random.Random(int(raw.get("seed", 20260925)))
        status_value = str(raw.get("status_file") or "拟人状态.json")
        status_path = Path(status_value)
        self.status_path = status_path if status_path.is_absolute() else (base_dir / status_path).resolve()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.speaking = False
        self.listening_until = 0.0
        self.thinking_until = 0.0
        self.settle_until = 0.0
        self.action_until = 0.0
        self.action_mask = ""
        self.expression_name = ""
        self.expression_intensity = 0.0
        self.expression_until = 0.0
        self.performance_start = 0.0
        self.performance_duration = 0.0
        self.performance_cues: list[dict[str, Any]] = []
        self.performance_active: list[str] = []
        self.idle_cue_kind = ""
        self.idle_cue_start = 0.0
        self.idle_cue_duration = 0.0
        self.idle_cue_sign = 1.0
        self.last_idle_kind = ""
        self.frames = 0
        self.blinks = 0
        self.started = time.monotonic()
        self.next_blink = self.started + self.rng.uniform(2.2, 4.8)
        self.blink_start = -1.0
        self.pending_double_blink = False
        self.next_gaze = self.started + self.rng.uniform(0.8, 1.8)
        self.next_idle_cue = self.started + self.rng.uniform(2.4, 5.0)
        self.gaze_yaw = self.gaze_pitch = 0.0
        self.target_yaw = self.target_pitch = 0.0
        self.gaze_returning = False
        self.phase = [self.rng.uniform(0, math.tau) for _ in range(12)]
        self.layer_debug: dict[str, float | str] = {}
        if self.enabled and autostart:
            self.thread = threading.Thread(target=self._run, name="embodiment", daemon=True)
            self.thread.start()

    def set_speaking(self, active: bool) -> None:
        with self.lock:
            self.speaking = bool(active)
            if active:
                self.listening_until = 0.0
                self.thinking_until = 0.0
                self.settle_until = 0.0
            else:
                self.settle_until = time.monotonic() + 0.65

    def set_listening(self, seconds: float = 8.0) -> None:
        with self.lock:
            self.listening_until = max(self.listening_until, time.monotonic() + max(0.0, float(seconds)))
            self.thinking_until = 0.0

    def set_thinking(self, seconds: float = 8.0) -> None:
        with self.lock:
            self.thinking_until = time.monotonic() + max(0.0, float(seconds))
            self.listening_until = 0.0

    def set_action(self, duration: float, mask: str) -> None:
        with self.lock:
            self.action_until = max(self.action_until, time.monotonic() + max(0.0, duration))
            self.action_mask = mask

    def set_expression(self, name: str, duration: float, intensity: float = 0.8) -> None:
        with self.lock:
            self.expression_name = str(name)
            self.expression_intensity = max(0.0, min(1.0, float(intensity)))
            self.expression_until = time.monotonic() + max(0.0, float(duration))

    def start_performance(self, cues: list[dict[str, Any]], duration: float) -> None:
        with self.lock:
            self.performance_start = time.monotonic()
            self.performance_duration = max(0.0, float(duration))
            self.performance_cues = [dict(cue) for cue in cues[:16]]
            self.performance_active = []

    def state(self, now: float) -> str:
        with self.lock:
            if self.speaking:
                return "speaking"
            if now < self.action_until:
                return "acting"
            if now < self.listening_until:
                return "listening"
            if now < self.thinking_until:
                return "thinking"
            if now < self.settle_until:
                return "settling"
            return "idle"

    def _presence_scales(self, mode: str) -> dict[str, float]:
        if mode == "acting" and self.action_mask in {"full", "upper"}:
            return {"body": 0.20, "head": 0.20, "weight": 0.18, "lean": 0.0,
                    "breath_hz": 0.26, "breath_amp": 0.35, "nod": 0.0}
        if mode == "listening":
            return {"body": 0.55, "head": 0.80, "weight": 0.35, "lean": 8.0,
                    "breath_hz": 0.24, "breath_amp": 0.78, "nod": 1.2}
        if mode == "thinking":
            return {"body": 0.48, "head": 0.85, "weight": 0.28, "lean": 0.35,
                    "breath_hz": 0.22, "breath_amp": 0.70, "nod": 0.0}
        if mode == "speaking":
            return {"body": 0.78, "head": 1.18, "weight": 0.55, "lean": 0.55,
                    "breath_hz": 0.32, "breath_amp": 1.12, "nod": 2.05}
        if mode == "settling":
            return {"body": 0.85, "head": 0.90, "weight": 0.70, "lean": 0.15,
                    "breath_hz": 0.26, "breath_amp": 0.90, "nod": 0.0}
        return {"body": 1.0, "head": 1.0, "weight": 1.0, "lean": 0.0,
                "breath_hz": 0.28, "breath_amp": 1.0, "nod": 0.0}

    def _schedule_idle_cue(self, now: float, mode: str) -> None:
        if mode == "listening":
            choices = [kind for kind, _weight in IDLE_CUE_WEIGHTS if kind in LISTENING_CUES]
        else:
            choices = list(IDLE_CUE_WEIGHTS)
        if self.last_idle_kind:
            choices = [item for item in choices if (item if isinstance(item, str) else item[0]) != self.last_idle_kind] or choices
        if mode == "listening":
            kind = self.rng.choice(tuple(choices))
        else:
            pool = [(name, weight) for name, weight in choices if name != self.last_idle_kind] or list(IDLE_CUE_WEIGHTS)
            total = sum(weight for _name, weight in pool)
            pick = self.rng.random() * total
            kind = pool[-1][0]
            running = 0.0
            for name, weight in pool:
                running += weight
                if pick <= running:
                    kind = name
                    break
        if kind == "look_around":
            duration = self.rng.uniform(1.8, 2.8)
            gap = self.rng.uniform(7.0, 13.0)
        elif kind == "weight_settle":
            duration = self.rng.uniform(1.6, 2.5)
            gap = self.rng.uniform(6.5, 12.0)
        elif kind in {"shoulder_roll", "wrist_ease"}:
            duration = self.rng.uniform(1.1, 2.0)
            gap = self.rng.uniform(5.5, 10.0)
        else:
            duration = self.rng.uniform(0.9, 1.8)
            gap = self.rng.uniform(4.2, 8.5)
        if mode == "listening":
            gap += 3.0
        self.idle_cue_kind = kind
        self.idle_cue_start = now
        self.idle_cue_duration = duration
        self.idle_cue_sign = -1.0 if self.rng.random() < 0.5 else 1.0
        self.last_idle_kind = kind
        self.next_idle_cue = now + duration + gap

    def _update_gaze(self, now: float, mode: str) -> None:
        if now >= self.next_gaze:
            if mode == "listening":
                self.target_yaw = self.rng.uniform(-1.6, 1.6)
                self.target_pitch = self.rng.uniform(-0.9, 0.7)
                self.next_gaze = now + self.rng.uniform(1.4, 2.8)
                self.gaze_returning = False
            elif mode == "thinking":
                self.target_yaw = self.rng.choice((-1.0, 1.0)) * self.rng.uniform(8.0, 16.0)
                self.target_pitch = self.rng.uniform(-1.5, 7.0)
                self.next_gaze = now + self.rng.uniform(0.55, 1.15)
                self.gaze_returning = True
            elif mode == "speaking":
                self.target_yaw = self.rng.uniform(-2.4, 2.4)
                self.target_pitch = self.rng.uniform(-1.1, 0.9)
                self.next_gaze = now + self.rng.uniform(1.1, 2.2)
                self.gaze_returning = False
            elif self.gaze_returning or self.rng.random() < 0.38:
                self.target_yaw = self.rng.uniform(-1.8, 1.8)
                self.target_pitch = self.rng.uniform(-1.1, 0.9)
                self.next_gaze = now + self.rng.uniform(1.4, 3.0)
                self.gaze_returning = False
            elif self.rng.random() < 0.32:
                self.target_yaw = self.rng.choice((-1.0, 1.0)) * self.rng.uniform(7.0, 12.5)
                self.target_pitch = self.rng.uniform(-3.6, 2.2)
                self.next_gaze = now + self.rng.uniform(0.65, 1.25)
                self.gaze_returning = True
            else:
                self.target_yaw = self.rng.uniform(-6.5, 6.5)
                self.target_pitch = self.rng.uniform(-2.6, 1.8)
                self.next_gaze = now + self.rng.uniform(0.9, 2.2)
        error = abs(self.target_yaw - self.gaze_yaw) + abs(self.target_pitch - self.gaze_pitch)
        smoothing = 0.30 if error > 5.0 else (0.16 if mode == "idle" else 0.22)
        self.gaze_yaw += (self.target_yaw - self.gaze_yaw) * smoothing
        self.gaze_pitch += (self.target_pitch - self.gaze_pitch) * smoothing

    def _update_blink(self, now: float) -> float:
        if self.blink_start < 0 and now >= self.next_blink:
            self.blink_start = now
            self.blinks += 1
        blink = 0.0
        if self.blink_start >= 0:
            phase = now - self.blink_start
            if phase < 0.065:
                blink = phase / 0.065
            elif phase < 0.095:
                blink = 1.0
            elif phase < 0.19:
                blink = 1.0 - (phase - 0.095) / 0.095
            else:
                self.blink_start = -1.0
                if self.pending_double_blink:
                    self.pending_double_blink = False
                    self.next_blink = now + self.rng.uniform(2.6, 6.4)
                elif self.rng.random() < 0.12:
                    self.pending_double_blink = True
                    self.next_blink = now + self.rng.uniform(0.14, 0.24)
                else:
                    self.next_blink = now + self.rng.uniform(2.4, 6.2)
        return blink

    def arm_directions(self, bones: dict[str, tuple[float, float, float, float]]) -> dict[str, tuple[float, float, float]]:
        return {
            "left_upper": normalize(rotate_vec(bones["LeftUpperArm"], BONE_OFFSETS["LeftLowerArm"])),
            "left_lower": normalize(rotate_vec(bones["LeftLowerArm"], BONE_OFFSETS["LeftHand"])),
            "right_upper": normalize(rotate_vec(bones["RightUpperArm"], BONE_OFFSETS["RightLowerArm"])),
            "right_lower": normalize(rotate_vec(bones["RightLowerArm"], BONE_OFFSETS["RightHand"])),
        }

    def sample(self, now: float) -> tuple[dict[str, tuple[float, float, float, float]], dict[str, float], str]:
        mode = self.state(now)
        elapsed = now - self.started
        with self.lock:
            performance_start = self.performance_start
            performance_duration = self.performance_duration
            performance_cues = [dict(cue) for cue in self.performance_cues]
        perf_head_pitch = perf_head_yaw = perf_head_roll = 0.0
        perf_torso_yaw = perf_torso_roll = perf_nod = 0.0
        perf_smile = perf_surprise = perf_breath = 0.0
        active_cues = []
        if performance_duration > 0.0:
            for cue in performance_cues:
                cue_start = performance_start + float(cue.get("start", 0.0)) * performance_duration
                cue_span = max(0.45, float(cue.get("duration", 0.3)) * performance_duration)
                phase = (now - cue_start) / cue_span
                if not 0.0 <= phase <= 1.0:
                    continue
                kind = str(cue.get("kind") or "")
                strength = math.sin(math.pi * phase) * float(cue.get("intensity", 1.0))
                active_cues.append(kind)
                if kind == "turn":
                    perf_torso_yaw += 5.0 * strength
                    perf_head_yaw += 8.0 * strength
                elif kind == "look":
                    perf_head_yaw += 4.5 * strength
                    perf_head_roll += 1.2 * strength
                elif kind == "lower_head":
                    perf_head_pitch += 7.0 * strength
                elif kind == "raise_head":
                    perf_head_pitch -= 5.0 * strength
                elif kind == "nod":
                    perf_nod += math.sin(math.tau * phase) * 4.0 * float(cue.get("intensity", 1.0))
                elif kind == "smile":
                    perf_smile = max(perf_smile, 0.72 * strength)
                elif kind == "surprise":
                    perf_surprise = max(perf_surprise, 0.45 * strength)
                elif kind == "breath":
                    perf_breath = max(perf_breath, strength)
                elif kind == "relax":
                    perf_torso_roll -= 0.8 * strength
                    perf_head_pitch -= 1.2 * strength
                elif kind == "shy":
                    perf_head_pitch += 3.5 * strength
                    perf_head_roll += 2.2 * strength
                    perf_torso_roll += 1.0 * strength
        performance_running = bool(
            performance_duration > 0.0
            and performance_start <= now <= performance_start + performance_duration + 0.6
        )
        cue_torso_roll = cue_shoulder = cue_wrist = cue_forearm = cue_weight = 0.0
        with self.lock:
            if mode in {"idle", "listening"} and not performance_running and now >= self.next_idle_cue:
                self._schedule_idle_cue(now, mode)
            idle_kind = self.idle_cue_kind
            idle_start = self.idle_cue_start
            idle_duration = self.idle_cue_duration
            idle_sign = self.idle_cue_sign
        idle_phase = (now - idle_start) / idle_duration if idle_duration > 0.0 else -1.0
        cue_live = mode in {"idle", "listening"} and not performance_running and 0.0 <= idle_phase <= 1.0
        if cue_live:
            strength = envelope(idle_phase)
            active_cues.append("idle:" + idle_kind)
            if idle_kind == "look":
                perf_head_yaw += 9.0 * idle_sign * strength
                perf_head_roll += 1.6 * idle_sign * strength
            elif idle_kind == "look_around":
                if idle_phase < 0.5:
                    bump = envelope(idle_phase / 0.5)
                    perf_head_yaw += 11.0 * idle_sign * bump
                    perf_head_roll += 1.8 * idle_sign * bump
                else:
                    bump = envelope((idle_phase - 0.5) / 0.5)
                    perf_head_yaw -= 8.5 * idle_sign * bump
                    perf_head_roll -= 1.4 * idle_sign * bump
            elif idle_kind == "nod":
                perf_nod += math.sin(math.tau * idle_phase) * 3.8
            elif idle_kind == "lower_head":
                perf_head_pitch += 6.0 * strength
            elif idle_kind == "smile":
                perf_smile = max(perf_smile, 0.34 * strength)
            elif idle_kind == "breath":
                perf_breath = max(perf_breath, 0.85 * strength)
            elif idle_kind == "shy":
                perf_head_pitch += 3.4 * strength
                perf_head_roll += 2.2 * idle_sign * strength
                cue_torso_roll += 1.1 * idle_sign * strength
            elif idle_kind == "relax":
                perf_head_pitch -= 1.4 * strength
                cue_torso_roll -= 0.8 * idle_sign * strength
            elif idle_kind == "head_tilt":
                perf_head_roll += 5.5 * idle_sign * strength
                perf_head_yaw += 2.4 * idle_sign * strength
            elif idle_kind == "weight_settle":
                cue_weight += idle_sign * strength
                cue_torso_roll += 1.2 * idle_sign * strength
            elif idle_kind == "shoulder_roll":
                cue_shoulder += 3.6 * idle_sign * strength
            elif idle_kind == "wrist_ease":
                cue_wrist += 5.5 * idle_sign * strength
                cue_forearm += 1.8 * idle_sign * strength
        with self.lock:
            self.performance_active = active_cues
        self._update_gaze(now, mode)
        blink = self._update_blink(now)

        scales = self._presence_scales(mode)
        body = scales["body"]
        head_scale = scales["head"]
        lean = scales["lean"]
        breath_hz = scales["breath_hz"]
        breath_amp = scales["breath_amp"] * (1.0 + 0.7 * perf_breath)

        b_hips = warped_sin(elapsed, breath_hz, self.phase[0])
        b_spine = warped_sin(elapsed, breath_hz, self.phase[0] + 0.50)
        b_chest = warped_sin(elapsed, breath_hz, self.phase[0] + 1.05)
        b_shoulder = warped_sin(elapsed, breath_hz, self.phase[0] + 1.55)
        b_head = warped_sin(elapsed, breath_hz, self.phase[0] + 2.05)
        b_arm = warped_sin(elapsed, breath_hz, self.phase[0] + 1.80)

        weight = math.sin(elapsed * math.tau / WEIGHT_PERIOD + self.phase[5])
        weight += 0.22 * math.sin(elapsed * math.tau / (WEIGHT_PERIOD * 1.73) + self.phase[6])
        weight = (weight + cue_weight * 1.15) * scales["weight"]
        slow_yaw = math.sin(elapsed * math.tau * 0.047 + self.phase[1])
        slow_pitch = math.sin(elapsed * math.tau * 0.039 + self.phase[2])
        slow_roll = math.sin(elapsed * math.tau * 0.031 + self.phase[3])
        nod = math.sin(elapsed * math.tau * 0.34 + self.phase[4]) * scales["nod"]

        hip_roll = (HIP_BIAS_ROLL + 1.65 * weight + 0.35 * slow_roll) * body
        hip_yaw = 0.45 * slow_yaw * body
        hip_pitch = -0.55 * b_hips * breath_amp * body

        spine_local = euler(
            pitch=(1.15 * b_spine * breath_amp - 0.20 * hip_roll) * body + 0.25 * lean,
            yaw=(perf_torso_yaw * 0.35 + 0.18 * weight) * body,
            roll=(0.55 * weight + cue_torso_roll * 0.45 + perf_torso_roll * 0.35) * body,
        )
        chest_local = euler(
            pitch=(2.15 * b_chest * breath_amp + 0.28 * slow_pitch) * body + 0.55 * lean,
            yaw=(perf_torso_yaw * 0.65 + 0.12 * weight) * body,
            roll=(0.42 * weight + 0.22 * slow_roll + cue_torso_roll * 0.55 + perf_torso_roll * 0.65) * body,
        )
        upper_chest_local = euler(
            pitch=0.85 * b_shoulder * breath_amp * body,
            roll=0.18 * weight * body,
        )
        neck_local = euler(
            pitch=(0.70 * b_head * breath_amp + 0.85 * slow_pitch + nod + perf_nod * 0.4
                   + perf_head_pitch * 0.4 + self.gaze_pitch * 0.32) * body,
            yaw=(1.15 * slow_yaw + perf_head_yaw * 0.45 + self.gaze_yaw * 0.34) * body,
            roll=(0.55 * slow_roll + perf_head_roll * 0.4 + 0.20 * weight) * body,
        )
        head_local = euler(
            pitch=(0.55 * b_head * breath_amp + 1.05 * slow_pitch + nod + perf_nod * 0.6
                   + perf_head_pitch * 0.6 + self.gaze_pitch * 0.42) * body * head_scale,
            yaw=(1.45 * slow_yaw + perf_head_yaw * 0.6 + self.gaze_yaw * 0.40) * body * head_scale,
            roll=(0.70 * slow_roll + perf_head_roll * 0.6) * body,
        )
        arm_breath = 0.85 * b_arm * breath_amp * body
        arm_asymmetry = (0.35 * slow_roll + 0.12 * weight) * body
        spine_roll = (0.55 * weight + cue_torso_roll * 0.45 + perf_torso_roll * 0.35) * body
        chest_roll = (0.42 * weight + 0.22 * slow_roll + cue_torso_roll * 0.55 + perf_torso_roll * 0.65) * body
        arm_keep = hip_roll + 0.55 * chest_roll + 0.25 * spine_roll
        left_shoulder = euler(
            pitch=(1.15 * b_shoulder * breath_amp + cue_shoulder) * body,
            roll=-SHOULDER_BIAS + 0.25 * weight * body,
        )
        right_shoulder = euler(
            pitch=(1.05 * b_shoulder * breath_amp - 0.35 * cue_shoulder) * body,
            roll=SHOULDER_BIAS + 0.25 * weight * body,
        )
        loaded_left = max(0.0, weight)
        loaded_right = max(0.0, -weight)
        local = {
            "Hips": euler(pitch=hip_pitch, yaw=hip_yaw, roll=hip_roll),
            "Spine": spine_local,
            "Chest": chest_local,
            "UpperChest": upper_chest_local,
            "Neck": neck_local,
            "Head": head_local,
            # Twelve degrees away from vertical keeps both hands visible
            # outside the dress panels while remaining a relaxed stance.
            "LeftShoulder": left_shoulder,
            "LeftUpperArm": axis_angle((0, 0, 1), ARM_BASE_DEGREES + 0.90 + arm_breath + arm_asymmetry - arm_keep),
            "LeftLowerArm": axis_angle((0, 0, 1), -3.2 + 0.55 * slow_pitch * body + cue_forearm),
            "LeftHand": euler(pitch=0.8 * slow_pitch * body, roll=1.8 * slow_yaw * body + cue_wrist),
            "RightShoulder": right_shoulder,
            "RightUpperArm": axis_angle((0, 0, 1), -ARM_BASE_DEGREES + 0.70 - arm_breath + arm_asymmetry - arm_keep),
            "RightLowerArm": axis_angle((0, 0, 1), 3.2 - 0.55 * slow_pitch * body - 0.4 * cue_forearm),
            "RightHand": euler(pitch=-0.8 * slow_pitch * body, roll=-1.8 * slow_yaw * body - 0.4 * cue_wrist),
            "LeftUpperLeg": euler(pitch=0.55 * loaded_left * body, roll=-0.85 * hip_roll),
            "LeftLowerLeg": euler(pitch=-0.35 * loaded_left * body),
            "LeftFoot": IDENTITY,
            "RightUpperLeg": euler(pitch=0.55 * loaded_right * body, roll=-0.85 * hip_roll),
            "RightLowerLeg": euler(pitch=-0.35 * loaded_right * body),
            "RightFoot": IDENTITY,
        }
        self.layer_debug = {
            "breath": round(b_chest * breath_amp, 3),
            "weight": round(weight, 3),
            "lean": round(lean, 3),
            "gaze_yaw": round(self.gaze_yaw, 2),
            "cue": ("idle:" + idle_kind) if cue_live else "",
        }
        world = {}
        for name in PARENTS:
            parent = PARENTS[name]
            world[name] = mul(world.get(parent, IDENTITY), local[name])
        world["LeftEye"] = euler(pitch=self.gaze_pitch, yaw=self.gaze_yaw)
        world["RightEye"] = euler(pitch=self.gaze_pitch, yaw=self.gaze_yaw)
        with self.lock:
            expression_name = self.expression_name if now < self.expression_until else ""
            expression_intensity = self.expression_intensity if expression_name else 0.0
        blends = {
            # Vodyanitsa's verified both-eyes morph is mapped from Blink.
            # The two wink channels were correct on the wire but did not
            # reliably read as a normal blink on this PMX.
            "Blink": max(0.0, min(1.0, blink)),
            "Joy": max(0.025 if mode == "speaking" else 0.0,
                       expression_intensity if expression_name == "Joy" else 0.0,
                       perf_smile),
            "Angry": expression_intensity if expression_name == "Angry" else 0.0,
            "Sorrow": expression_intensity if expression_name == "Sorrow" else 0.0,
            "Surprise": max(expression_intensity if expression_name == "Surprise" else 0.0,
                            perf_surprise),
        }
        return world, blends, mode

    def _packet(self, now: float) -> tuple[bytes, str]:
        bones, blends, mode = self.sample(now)
        messages = []
        for name, quat in bones.items():
            w, x, y, z = quat
            px, py, pz = BONE_OFFSETS.get(name, (0.0, 0.0, 0.0))
            messages.append(osc_message("/VMC/Ext/Bone/Pos", name, px, py, pz, x, y, z, w))
        messages.extend(osc_message("/VMC/Ext/Blend/Val", name, value) for name, value in blends.items())
        messages.extend((osc_message("/VMC/Ext/Blend/Apply"), osc_message("/VMC/Ext/OK", 1)))
        return osc_bundle(messages), mode

    def _write_status(self, mode: str) -> None:
        payload = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "state": mode,
            "frames": self.frames,
            "blinks": self.blinks,
            "gaze": {"yaw": round(self.gaze_yaw, 3), "pitch": round(self.gaze_pitch, 3)},
            "port": self.port,
            "fps": self.fps,
            "full_body_idle": True,
            "performance_active": list(self.performance_active),
            "idle_layers": dict(self.layer_debug),
        }
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_suffix(self.status_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            temporary.replace(self.status_path)
        except OSError:
            try:
                self.status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _run(self) -> None:
        deadline = time.perf_counter()
        last_status = 0.0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            while not self.stop_event.is_set():
                now = time.monotonic()
                packet, mode = self._packet(now)
                sock.sendto(packet, ("127.0.0.1", self.port))
                self.frames += 1
                status_interval = 0.2 if self.performance_active else 1.0
                if now - last_status >= status_interval:
                    last_status = now
                    try:
                        self._write_status(mode)
                    except OSError:
                        pass
                deadline += 1.0 / self.fps
                self.stop_event.wait(max(0.0, deadline - time.perf_counter()))
            for _ in range(3):
                packet, _ = self._packet(time.monotonic())
                sock.sendto(packet, ("127.0.0.1", self.port))

    def close(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
