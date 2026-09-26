"""Drive voice and motion from YuriOS with semantic planning and ARDY fallback.

Spark-X2.5-4B polishes Yuri's *stage directions* into a bounded English ARDY
prompt.  Laya reuses saved clips for explicit commands.  MiniLM ARDY generates
new motion.  The action bus plays the clip on the Blender body.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import hashlib
import json
import logging
import os
import queue
import random
import re
import signal
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

from embodiment_controller import EmbodimentController


LOG = logging.getLogger("virtual_idol.mind_action_bridge")
VALID_MASKS = {"full", "upper", "arms"}
VISEMES = ("A", "I", "U", "E", "O")
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _osc_string(value: str) -> bytes:
    data = value.encode("utf-8") + b"\0"
    return data + b"\0" * (-len(data) % 4)


def _osc_message(address: str, *args: object) -> bytes:
    tags = ","
    body = b""
    for arg in args:
        if isinstance(arg, str):
            tags += "s"
            body += _osc_string(arg)
        elif isinstance(arg, int):
            tags += "i"
            body += struct.pack(">i", arg)
        else:
            tags += "f"
            body += struct.pack(">f", float(arg))
    return _osc_string(address) + _osc_string(tags) + body


def publish_closed_mouth(port: int) -> None:
    messages = [_osc_message("/VMC/Ext/Blend/Val", name, 0.0) for name in VISEMES]
    messages.extend((_osc_message("/VMC/Ext/Blend/Apply"), _osc_message("/VMC/Ext/OK", 1)))
    packet = b"#bundle\0" + struct.pack(">Q", 1) + b"".join(
        struct.pack(">I", len(item)) + item for item in messages
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _ in range(3):
            sock.sendto(packet, ("127.0.0.1", int(port)))


def spoken_text(text: str) -> str:
    """Remove stage directions and emotion tags before TTS."""
    value = re.sub(r"\*[^*]*\*", " ", text, flags=re.DOTALL)
    value = re.sub(r"\[[^\]\r\n]{1,40}\]", " ", value)
    return " ".join(value.split())


def is_presence_filler(text: str) -> bool:
    """YuriOS greeting / 'I'm here' presence lines that should not be spoken."""
    spoken = spoken_text(text)
    if not spoken:
        return True
    if re.search(r"[\u4e00-\u9fff]", spoken):
        return False
    compact = " ".join(re.findall(r"[a-z']+", spoken.casefold()))
    if len(compact) > 90:
        return False
    return bool(re.search(
        r"\bi'?m here\b|\bi have been waiting\b|\bi've been waiting\b|"
        r"\bhi\b.{0,24}\bhere\b|\bhey\b.{0,24}\bhere\b",
        compact,
    ))


def stage_directions(text: str) -> str:
    """Extract bounded assistant-authored physical acting from *stage directions*."""
    parts = [" ".join(item.split()) for item in re.findall(r"\*([^*]+)\*", text, flags=re.DOTALL)]
    return " ".join(part for part in parts if part)[:1200]


def performance_plan(text: str, current_pose: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Turn interleaved *acting prose* into safe, timed embodiment cues."""
    matches = list(re.finditer(r"\*([^*]+)\*", text, flags=re.DOTALL))
    if not matches:
        return []
    total_spoken = max(1, len(spoken_text(text)))
    cues: list[dict[str, Any]] = []
    for match in matches:
        acting = " ".join(match.group(1).split())
        start = min(0.70, len(spoken_text(text[: match.start()])) / total_spoken)

        def add(kind: str, intensity: float = 1.0, duration: float = 0.28) -> None:
            cues.append({
                "kind": kind, "start": round(start, 4),
                "duration": duration, "intensity": intensity,
                "source": acting[:160],
            })

        turn_toward_user = any(word in acting for word in ("望向", "看着你", "望着你", "看向你"))
        already_facing_user = bool((current_pose or {}).get("facing_user", False))
        if any(word in acting for word in ("转身", "转过身", "侧身")) and not (turn_toward_user and already_facing_user):
            add("turn", 0.8, 0.34)
        if any(word in acting for word in ("望向", "看着你", "望着你", "看向你", "侧过头", "偏过头", "歪了歪头")):
            add("look", 0.75, 0.32)
        if any(word in acting for word in ("低头", "垂下眼", "垂下头", "垂眸")):
            add("lower_head", 0.8, 0.32)
        if any(word in acting for word in ("抬头", "抬眼")):
            add("raise_head", 0.65, 0.28)
        if "点头" in acting:
            add("nod", 0.9, 0.28)
        if any(word in acting for word in ("微笑", "笑意", "笑容", "嘴角", "眼睛弯")):
            add("smile", 0.7, 0.46)
        if any(word in acting for word in ("惊讶", "一怔", "愣住")):
            add("surprise", 0.5, 0.22)
        if any(word in acting for word in ("深吸", "呼吸", "喘了一口气")):
            add("breath", 0.8, 0.34)
        if any(word in acting for word in ("放松", "安心")):
            add("relax", 0.65, 0.42)
        if any(word in acting for word in ("指尖", "手指", "衣角", "袖口", "羞涩", "害羞", "不好意思")):
            add("shy", 0.65, 0.42)
    return cues[:16]


def normalized_command(text: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text.casefold())


def needs_assistant_context(text: str) -> bool:
    """Broad commands let Yuri's current acting draft choose the actual motion."""
    return normalized_command(text) in {"跳舞", "舞蹈", "表演", "表演一下", "动一下"}


def is_dance_command(command: str) -> bool:
    return bool(
        "舞蹈" in command
        or "dance" in command
        or re.search(r"(?:跳|来)(?:一|个|支|段|小段|一段|一支|一个)?舞", command)
    )


def curated_idle_candidates(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in catalog.get("actions", [])
        if item.get("category") == "idle"
        and bool(item.get("human_reviewed", False))
        and bool((item.get("quality") or {}).get("safe_for_idle"))
        and not bool((item.get("quality") or {}).get("review_required"))
    ]


def local_motion_plan(user_text: str, assistant_action_text: str) -> dict[str, Any] | None:
    """Fast scene-safe plans for common body commands; no second model call."""
    command = normalized_command(user_text)
    if any(word in command for word in ("跳跃", "跳一下", "跳起来", "跃起", "jump")):
        return {
            "mode": "generate", "action_id": None,
            "ardy_prompt": (
                "A person bends both knees, rises onto both toes, swings both arms slightly backward, "
                "jumps straight upward once with both feet clearly leaving the floor, lands softly on both "
                "feet with bent knees, regains balance, lowers both arms, and returns to a neutral standing posture."
            ),
            "duration_seconds": 4.0, "mask": "full", "loop": False,
            "confidence": 1.0, "reason": "本地跳跃模板；保留明确离地和落地顺序",
        }
    if is_dance_command(command):
        return {
            "mode": "generate", "action_id": None,
            "ardy_prompt": (
                "A person performs a clearly visible dance in place, bends both knees rhythmically, "
                "takes alternating side steps, shifts body weight widely from left to right, sways the hips "
                "and torso, raises both arms outward into broad flowing arcs, briefly rises onto both toes, "
                "then lowers both arms and returns to a neutral standing posture."
            ),
            "duration_seconds": 7.0, "mask": "full", "loop": False,
            "confidence": 1.0, "reason": "本地可见舞蹈模板；不引用场景物体",
        }
    if any(word in command for word in ("跑步", "跑一下", "慢跑", "run", "jog")):
        return {
            "mode": "generate", "action_id": None,
            "ardy_prompt": (
                "A person jogs visibly in place, leans the torso slightly forward, alternately lifts both "
                "knees to a clear height, swings the left and right arms in opposite rhythm with broad arcs, "
                "keeps both feet near the starting point, gradually slows down, straightens the torso, lowers "
                "both arms, and returns to a neutral standing posture."
            ),
            "duration_seconds": 6.0, "mask": "full", "loop": False,
            "confidence": 1.0, "reason": "本地原地跑步模板；不依赖根位移",
        }
    if any(word in command for word in ("笑一个", "微笑", "笑笑", "smile")):
        return {
            "mode": "expression", "expression": "Joy", "intensity": 0.9,
            "duration_seconds": 3.5, "mask": "expression", "loop": False,
            "confidence": 1.0, "reason": "表情反射无需 ARDY",
        }
    if any(word in command for word in ("挥手", "打招呼", "wave")):
        return {
            "mode": "generate", "action_id": None,
            "ardy_prompt": (
                "A person stands in place, raises the right arm beside the shoulder, opens the hand, waves "
                "the forearm clearly from side to side three times, lowers the arm, and returns to neutral."
            ),
            "duration_seconds": 4.0, "mask": "arms", "loop": False,
            "confidence": 1.0, "reason": "本地挥手模板",
        }
    if any(word in command for word in ("鞠躬", "bow")):
        return {
            "mode": "generate", "action_id": None,
            "ardy_prompt": (
                "A person stands with both feet planted, bends the upper body forward into a clear polite bow, "
                "holds briefly, rises upright, and returns to a neutral standing posture."
            ),
            "duration_seconds": 4.0, "mask": "full", "loop": False,
            "confidence": 1.0, "reason": "本地鞠躬模板",
        }
    return None


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def parse_agnes_plan(
    content: str, allowed_actions: set[str], forbidden_prompt_terms: Iterable[str] = (),
) -> dict[str, Any]:
    """Parse and validate the planner's JSON without trusting model output."""
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("planner returned no JSON object")
    raw = json.loads(value[start : end + 1])
    if not isinstance(raw, dict):
        raise ValueError("planner result must be an object")
    mode = str(raw.get("mode") or "none").strip().casefold()
    if mode not in {"none", "catalog", "generate"}:
        raise ValueError("planner returned an unsupported mode")
    action_id = raw.get("action_id")
    if mode == "catalog":
        if not isinstance(action_id, str) or action_id not in allowed_actions:
            raise ValueError("planner selected an action outside the live catalog")
    else:
        action_id = None
    ardy_prompt = str(raw.get("ardy_prompt") or "").strip()
    if mode == "generate":
        if not 8 <= len(ardy_prompt) <= 500 or not ardy_prompt.casefold().startswith("a person"):
            raise ValueError("planner returned an invalid ARDY prompt")
        lowered = ardy_prompt.casefold()
        forbidden = [
            str(term).strip().casefold() for term in forbidden_prompt_terms
            if str(term).strip() and re.search(rf"\b{re.escape(str(term).strip().casefold())}\b", lowered)
        ]
        if forbidden:
            raise ValueError("ARDY prompt references unavailable scene objects: " + ", ".join(forbidden))
    else:
        ardy_prompt = ""
    mask = str(raw.get("mask") or "full")
    if mask not in VALID_MASKS:
        mask = "full"
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        duration = max(2.0, min(10.0, float(raw.get("duration_seconds", 5.0))))
    except (TypeError, ValueError):
        duration = 5.0
    return {
        "mode": mode,
        "action_id": action_id,
        "ardy_prompt": ardy_prompt,
        "duration_seconds": duration,
        "mask": mask,
        "loop": False,
        "confidence": confidence,
        "reason": str(raw.get("reason") or "")[:160],
    }


def _load_spark_credentials(raw: dict[str, Any], base_dir: Path) -> tuple[str, str, str]:
    """Read Spark MaaS credentials. Key stays in YuriOS .env, never in the project."""
    api_key = str(raw.get("api_key") or os.environ.get("YURIOS_MODEL_API_KEY_SPARK") or "").strip()
    base_url = str(raw.get("base_url") or "https://maas-api.cn-huabei-1.xf-yun.com/v2").rstrip("/")
    model = str(raw.get("model") or "spark-x2.5-4b")
    if api_key:
        return api_key, base_url, model
    env_file = str(raw.get("env_file") or "")
    if env_file:
        env_value = env_file.replace("%USERPROFILE%", os.environ.get("USERPROFILE", ""))
        env_path = Path(os.path.expandvars(env_value))
        if not env_path.is_absolute():
            env_path = (base_dir / env_path).resolve()
        if env_path.is_file():
            values = _read_env(env_path)
            api_key = (
                values.get("YURIOS_MODEL_API_KEY_SPARK")
                or values.get("SPARK_API_KEY")
                or values.get("AGNES_API_KEY")
                or ""
            ).strip()
            base_url = (values.get("SPARK_BASE_URL") or values.get("AGNES_BASE_URL") or base_url).rstrip("/")
            model = values.get("SPARK_MODEL") or values.get("AGNES_MODEL") or model
            if api_key:
                return api_key, base_url, model
    env_name = str(raw.get("credential_env") or "YURIOS_MODEL_API_KEY_SPARK")
    wsl_file = str(raw.get("credential_wsl_file") or "/home/mozi/yurios-eval/runtime/.env")
    try:
        output = subprocess.check_output(
            [
                "wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc",
                f"grep '^{env_name}=' {wsl_file} | cut -d= -f2-",
            ],
            text=True, timeout=8, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        raise ValueError(f"Spark planner credentials are not configured: {exc}") from exc
    api_key = output.strip().strip('"').strip("'")
    if not api_key:
        raise ValueError("Spark planner credentials are not configured")
    return api_key, base_url, model


class AgnesMotionPlanner:
    """Spark-X2.5-4B action planner. Agnes credentials are not used."""

    def __init__(self, raw: dict[str, Any] | None, base_dir: Path):
        raw = raw or {}
        self.enabled = bool(raw.get("enabled", False))
        self.timeout = float(raw.get("timeout_seconds", 20.0))
        self.confidence_threshold = float(raw.get("confidence_threshold", 0.62))
        self.api_key = ""
        self.base_url = ""
        self.model = ""
        self.provider = str(raw.get("provider") or "spark")
        scene = raw.get("scene") or {}
        self.scene = {
            "description": str(scene.get("description") or "An empty standing stage with a flat floor."),
            "available_interaction_objects": list(scene.get("available_interaction_objects") or []),
            "allow_object_interaction": bool(scene.get("allow_object_interaction", False)),
        }
        self.forbidden_prompt_terms = tuple(
            str(item).strip() for item in scene.get("forbidden_prompt_terms", []) if str(item).strip()
        )
        self.assistant_context_timeout = float(raw.get("assistant_context_timeout_seconds", 12.0))
        # China MaaS should not follow the local VPN proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if not self.enabled:
            return
        self.api_key, self.base_url, self.model = _load_spark_credentials(raw, base_dir)

    def plan(
        self, user_text: str, catalog: list[dict[str, Any]],
        assistant_action_text: str = "",
        current_pose: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        compact = [
            {
                "id": item.get("id"),
                "label": item.get("label"),
                "tags": item.get("tags", []),
                "seconds": item.get("duration_seconds"),
                "prompt": item.get("prompt", ""),
                "examples": item.get("examples", []),
                "source": item.get("source", "catalog"),
            }
            for item in catalog
            if isinstance(item, dict) and item.get("id")
        ]
        allowed = {str(item["id"]) for item in compact}
        has_acting = bool(str(assistant_action_text or "").strip())
        system = (
            "你是虚拟角色的身体动作规划器。只输出一个 JSON 对象。"
            "assistant_action_text 是角色本回合已经写出的舞台动作描写。"
            "只要 assistant_action_text 包含可见身体动作，就必须 mode=generate 或 mode=catalog，禁止 mode=none。"
            "普通聊天同样适用：有动作描写就要做动作。"
            "目录同时含预置项和过去成功生成并保存的 ARDY 动作；语义合适时 mode=catalog 并选择目录内 action_id。"
            "没有合适目录项时 mode=generate，把描写改写成清晰的英文 ARDY 提示词，"
            "必须以 'A person' 开头，写清动作顺序、左右方向、双脚是否离地和最后恢复中立站姿。"
            "scene_context 是当前 Blender 场景事实。删除或改写对不存在物体、家具、窗户、座位和墙面的交互。"
            "current_pose 是角色此刻真实输出姿态的摘要。新动作必须从它自然衔接；如果角色已经面向用户，"
            "不要执行‘转身看向用户’而把身体转走，只做视线或轻微姿态调整。"
            "只保留人体能够在原地或平地独立完成的动作；不要在 ARDY 提示词中提及不存在的物体。"
            "把情绪和目光转成可见的头部、躯干、手臂和脚步动作，不写内心、雨声或镜头语言。"
            "不要把包含多个可行动作细节的草稿压缩成单一步骤；依次保留所有不依赖物体的动作。"
            "对话手势优先 mask=upper 或 arms；只有明确涉及脚步、重心、舞蹈、鞠躬、跳跃时才用 full。"
            "duration_seconds 取 2 到 8 秒；loop 永远 false。"
            "若 assistant_action_text 为空，且用户没有要求或明显暗示身体动作，才允许 mode=none。"
            "格式：{\"mode\":\"none|catalog|generate\",\"action_id\":null,"
            "\"ardy_prompt\":\"\",\"duration_seconds\":5,\"mask\":\"upper\","
            "\"confidence\":0.0,\"reason\":\"\"}"
        )
        if has_acting:
            system += "当前回合已有动作描写，禁止输出 mode=none。"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(
                    {
                        "user_text": user_text,
                        "assistant_action_text": assistant_action_text,
                        "scene_context": self.scene,
                        "current_pose": current_pose or {},
                        "action_catalog": compact,
                    }, ensure_ascii=False
                )},
            ],
            "temperature": 0,
            "max_tokens": 420,
            "stream": False,
            "reasoning_effort": "none",
        }
        if body["messages"] and body["messages"][0].get("role") == "system":
            system_text = str(body["messages"][0].get("content") or "")
            if "/no_think" not in system_text:
                body["messages"][0]["content"] = system_text + "\n/no_think"
        def complete() -> str:
            request = urllib.request.Request(
                self.base_url + "/chat/completions",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            message = (((payload.get("choices") or [{}])[0].get("message") or {}))
            return str(message.get("content") or message.get("reasoning_content") or "")

        def validate(candidate: str) -> dict[str, Any]:
            plan = parse_agnes_plan(candidate, allowed, self.forbidden_prompt_terms)
            if has_acting and plan["mode"] == "none":
                raise ValueError("stage directions present; mode=none is not allowed")
            if plan["mode"] == "generate" and assistant_action_text:
                if len(assistant_action_text) >= 30 and len(plan["ardy_prompt"].split()) < 30:
                    raise ValueError("ARDY prompt lost feasible action details from assistant_action_text")
                if re.search(r"跳舞|舞蹈|dance", user_text, flags=re.IGNORECASE):
                    plan["duration_seconds"] = max(7.0, float(plan["duration_seconds"]))
            return plan

        content = complete()
        try:
            return validate(content)
        except (ValueError, json.JSONDecodeError) as first_error:
            body["messages"].extend([
                {"role": "assistant", "content": content[:1200]},
                {"role": "user", "content": (
                    "上一个结果不符合动作 JSON 契约。重新只输出一个 JSON 对象。"
                    "generate 模式的 ardy_prompt 必须是 8–500 字符的英文句子并以 A person 开头，"
                    "不得出现当前场景不存在的物体；把相关动作改写成原地人体动作。"
                    "只要 assistant_action_text 有可见身体动作，禁止 mode=none。"
                    "必须重新读取原始 assistant_action_text，依次保留所有可由人体独立完成的动作；"
                    "如果原草稿有多个动作，英文提示词至少写 30 个单词，不能缩成单一步骤。"
                    f"校验错误：{first_error}"
                )},
            ])
            repaired = complete()
            return validate(repaired)


@dataclasses.dataclass(frozen=True, slots=True)
class MotionIntent:
    intent_id: str
    actor: str
    action_id: str
    mask: str
    loop: bool
    reason: str
    source_event_id: str

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class Rule:
    keywords: tuple[str, ...]
    action_id: str
    mask: str = "full"
    loop: bool = False
    reason: str = "tag rule"
    patterns: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Rule":
        keywords = tuple(str(item).strip().casefold() for item in raw.get("keywords", []) if str(item).strip())
        patterns = tuple(str(item).strip() for item in raw.get("patterns", []) if str(item).strip())
        action_id = str(raw.get("action_id") or "").strip()
        mask = str(raw.get("mask") or "full").strip()
        if not keywords and not patterns:
            raise ValueError("rule keywords and patterns cannot both be empty")
        for pattern in patterns:
            re.compile(pattern)
        if not action_id:
            raise ValueError("rule action_id cannot be empty")
        if mask not in VALID_MASKS:
            raise ValueError(f"unsupported mask: {mask}")
        return cls(
            keywords=keywords,
            action_id=action_id,
            mask=mask,
            loop=bool(raw.get("loop", False)),
            reason=str(raw.get("reason") or "tag rule"),
            patterns=patterns,
        )


class IntentResolver:
    """Resolve only explicit user wording into one allowlisted action."""

    def __init__(self, actor: str, rules: Iterable[Rule], cooldown_seconds: float = 4.0):
        self.actor = actor
        self.rules = tuple(rules)
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._last_action_at: dict[str, float] = {}

    def resolve(
        self,
        user_text: str,
        assistant_text: str,
        source_event_id: str,
        *,
        now: float | None = None,
    ) -> MotionIntent | None:
        # User wording is authoritative.  Assistant prose is kept in the method
        # signature for future context-aware rules, but does not trigger actions
        # by itself; this avoids a greeting word in every reply causing a wave.
        del assistant_text
        text = user_text.casefold()
        timestamp = time.monotonic() if now is None else float(now)
        for index, rule in enumerate(self.rules):
            hits = [keyword for keyword in rule.keywords if keyword in text]
            hits.extend(
                match.group(0)
                for pattern in rule.patterns
                if (match := re.search(pattern, text)) is not None
            )
            if not hits:
                continue
            previous = self._last_action_at.get(rule.action_id, float("-inf"))
            if timestamp - previous < self.cooldown_seconds:
                return None
            self._last_action_at[rule.action_id] = timestamp
            event_id = source_event_id or f"local-{int(timestamp * 1000)}"
            return MotionIntent(
                intent_id=f"{event_id}:{index}",
                actor=self.actor,
                action_id=rule.action_id,
                mask=rule.mask,
                loop=rule.loop,
                reason=f"{rule.reason}: {', '.join(hits)}",
                source_event_id=source_event_id,
            )
        return None


class MotionHubClient:
    def __init__(self, base_url: str, timeout_seconds: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(0.2, float(timeout_seconds))
        # Loopback control traffic must never follow HTTP(S)_PROXY.  Besides
        # leaking local command paths, a proxy can turn "hub is offline" into
        # a misleading remote 502 response.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _json(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        method = "GET"
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            method = "POST"
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method, headers=headers
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                detail = ""
            raise RuntimeError(f"motion hub {path} returned HTTP {exc.code}: {detail}") from exc

    def status(self) -> dict[str, Any]:
        return self._json("/api/status")

    @staticmethod
    def actor_online(status: dict[str, Any], actor: str) -> bool:
        return any(
            item.get("profile_id") == actor
            for item in status.get("subscribers", [])
            if isinstance(item, dict)
        )

    @staticmethod
    def action_ids(status: dict[str, Any]) -> set[str]:
        return {
            str(item.get("id"))
            for item in status.get("action_catalog", [])
            if isinstance(item, dict) and item.get("id")
        }

    def dispatch(self, intent: MotionIntent, *, require_actor_online: bool = True) -> dict[str, Any]:
        status = self.status()
        if intent.action_id not in self.action_ids(status):
            raise RuntimeError(f"action is not present in the live hub catalog: {intent.action_id}")
        if require_actor_online and not self.actor_online(status, intent.actor):
            raise RuntimeError(f"actor is offline: {intent.actor}")
        return self._json(
            "/api/action",
            {
                "actor": intent.actor,
                "id": intent.action_id,
                "mask": intent.mask,
                "loop": intent.loop,
            },
        )

    def stop(self, actor: str) -> dict[str, Any]:
        return self._json("/api/action", {"actor": actor, "command": "stop"})

    def dispatch_generated(
        self,
        actor: str,
        filename: str,
        label: str,
        mask: str = "full",
        *,
        require_actor_online: bool = True,
    ) -> dict[str, Any]:
        status = self.status()
        if require_actor_online and not self.actor_online(status, actor):
            raise RuntimeError(f"actor is offline: {actor}")
        return self._json(
            "/api/generated-action",
            {"actor": actor, "file": filename, "label": label, "mask": mask, "loop": False},
        )


class StatusWriter:
    def __init__(self, path: Path, audit_path: Path | None = None):
        self.path = path
        self.audit_path = audit_path
        self.lock = threading.Lock()

    def write(self, **values: Any) -> None:
        payload = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "monotonic_ms": round(time.perf_counter() * 1000.0, 3),
            **values,
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(encoded, encoding="utf-8")
            temporary.replace(self.path)
            if self.audit_path is not None:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


class ArdyLibrary:
    """Persistent procedural memory containing only ARDY-derived motions."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save({"version": 1, "actions": []})

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"version": 1, "actions": []}
        if not isinstance(data, dict) or not isinstance(data.get("actions"), list):
            raise ValueError("invalid ARDY action library")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)

    def entries(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self._load()["actions"])

    def planner_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item["id"], "label": item["label"],
                "tags": item.get("tags", ["ARDY", "生成动作"]),
                "duration_seconds": item.get("duration_seconds"),
                "prompt": item.get("ardy_prompt", ""),
                "examples": item.get("user_examples", [])[-5:],
                "source": "ardy_saved",
            }
            for item in self.entries()
            if isinstance(item, dict) and item.get("id") and item.get("file")
        ]

    def get(self, action_id: str) -> dict[str, Any] | None:
        return next((item for item in self.entries() if item.get("id") == action_id), None)

    def record(
        self,
        generated: dict[str, Any],
        plan: dict[str, Any],
        user_text: str,
    ) -> dict[str, Any]:
        key = str(generated["cache_key"])
        action_id = "ardy-" + key
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self.lock:
            data = self._load()
            item = next((row for row in data["actions"] if row.get("id") == action_id), None)
            if item is None:
                item = {
                    "id": action_id,
                    "label": "ARDY · " + user_text.strip()[:48],
                    "tags": ["ARDY", "生成动作"],
                    "ardy_prompt": plan["ardy_prompt"],
                    "duration_seconds": plan["duration_seconds"],
                    "mask": plan["mask"],
                    "file": generated["clip_file"],
                    "npz": generated["npz"],
                    "frames": generated.get("frames"),
                    "fps": generated.get("fps"),
                    "model": f"ARDY-Core-RP-20FPS-Horizon40-{int(generated.get('steps') or 10)}steps",
                    "generation_steps": int(generated.get("steps") or 10),
                    "seed": int(generated.get("seed", -1)),
                    "text_encoder": "MiniLM ARDY Core40 distilled",
                    "created_at": now,
                    "last_used_at": now,
                    "use_count": 1,
                    "user_examples": [user_text.strip()],
                    "assistant_action_examples": [plan.get("assistant_action_text", "")]
                    if plan.get("assistant_action_text") else [],
                }
                data["actions"].append(item)
            else:
                item["last_used_at"] = now
                item["use_count"] = int(item.get("use_count", 0)) + 1
                examples = list(item.get("user_examples") or [])
                if user_text.strip() and user_text.strip() not in examples:
                    examples.append(user_text.strip())
                item["user_examples"] = examples[-20:]
                acting_examples = list(item.get("assistant_action_examples") or [])
                acting = str(plan.get("assistant_action_text") or "").strip()
                if acting and acting not in acting_examples:
                    acting_examples.append(acting)
                item["assistant_action_examples"] = acting_examples[-10:]
            self._save(data)
            return dict(item)

    def mark_used(self, action_id: str, user_text: str) -> dict[str, Any] | None:
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self.lock:
            data = self._load()
            item = next((row for row in data["actions"] if row.get("id") == action_id), None)
            if item is None:
                return None
            item["last_used_at"] = now
            item["use_count"] = int(item.get("use_count", 0)) + 1
            examples = list(item.get("user_examples") or [])
            if user_text.strip() and user_text.strip() not in examples:
                examples.append(user_text.strip())
            item["user_examples"] = examples[-20:]
            self._save(data)
            return dict(item)


class LayaReflex:
    """Persistent GPU System 1 worker for high-confidence saved-action reuse."""

    def __init__(self, raw: dict[str, Any] | None, base_dir: Path, status_writer: StatusWriter):
        raw = raw or {}
        self.enabled = bool(raw.get("enabled", False))
        self.preload_enabled = bool(raw.get("preload", True))
        self.status_writer = status_writer
        self.start_timeout = float(raw.get("start_timeout_seconds", 120.0))
        self.decision_timeout = float(raw.get("decision_timeout_seconds", 10.0))
        self.lock = threading.Lock()
        self.protocol: queue.Queue[dict[str, Any]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.reader: threading.Thread | None = None
        self.stderr_stream = None
        self.python = self.worker = self.log_path = None
        self.environment: dict[str, str] = {}
        if not self.enabled:
            return

        def resolve(value: str) -> Path:
            path = Path(os.path.expandvars(value))
            return path.resolve() if path.is_absolute() else (base_dir / path).resolve()

        self.python = resolve(str(raw.get("python") or ""))
        self.worker = resolve(str(raw.get("worker") or "laya_reflex_worker.py"))
        self.log_path = resolve(str(raw.get("log_file") or "laya-worker.log"))
        if not self.python.is_file() or not self.worker.is_file():
            raise ValueError("Laya worker runtime is incomplete")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.environment = {
            "HF_HOME": str(raw.get("hf_home") or r"D:\AI\Laya\cache"),
            "HF_HUB_OFFLINE": "1",
            "LAYA_DEVICE": str(raw.get("device") or "cuda"),
            "LAYA_EMBED_MODEL": str(raw.get("embed_model") or r"D:\AI\Laya\bge-small-zh-v1.5"),
            "LAYA_SEMANTIC_THRESHOLD": str(raw.get("semantic_threshold", 0.77)),
            "LAYA_SEMANTIC_OVERRIDE_THRESHOLD": str(raw.get("semantic_override_threshold", 0.80)),
            "LAYA_SEMANTIC_MARGIN": str(raw.get("semantic_margin", 0.12)),
            "LAYA_CHOICE_THRESHOLD": str(raw.get("choice_threshold", 0.65)),
            "LAYA_MOTION_THRESHOLD": str(raw.get("motion_threshold", 0.08)),
            "LAYA_NO_MOTION_THRESHOLD": str(raw.get("no_motion_threshold", 0.02)),
        }

    def _read_protocol(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self.protocol.put(payload)

    def _next(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Laya worker timed out")
        try:
            return self.protocol.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("Laya worker timed out") from exc

    def _ensure_started(self) -> None:
        if not self.enabled:
            raise RuntimeError("Laya reflex is disabled")
        if self.process is not None and self.process.poll() is None:
            return
        while not self.protocol.empty():
            self.protocol.get_nowait()
        self.stderr_stream = self.log_path.open("a", encoding="utf-8")
        env = os.environ.copy()
        env.update(self.environment)
        self.status_writer.write(state="laya_loading", device=self.environment["LAYA_DEVICE"])
        self.process = subprocess.Popen(
            [str(self.python), "-u", str(self.worker)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr_stream,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = threading.Thread(
            target=self._read_protocol, args=(self.process,), name="laya-protocol", daemon=True
        )
        self.reader.start()
        deadline = time.monotonic() + self.start_timeout
        while True:
            message = self._next(deadline)
            if message.get("status") == "ready":
                self.status_writer.write(state="laya_ready", **{
                    key: message.get(key) for key in ("device", "model", "embedder")
                })
                return
            if message.get("status") == "error":
                raise RuntimeError(str(message.get("message") or "Laya failed to load"))

    def preload(self) -> None:
        with self.lock:
            self._ensure_started()

    def decide(self, user_text: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            self._ensure_started()
            if self.process is None or self.process.stdin is None:
                raise RuntimeError("Laya worker is unavailable")
            self.process.stdin.write(json.dumps(
                {"cmd": "decide", "user_text": user_text, "actions": actions},
                ensure_ascii=True,
            ) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + self.decision_timeout
            while True:
                result = self._next(deadline)
                if result.get("status") == "decision":
                    return result
                if result.get("status") == "error":
                    raise RuntimeError(str(result.get("message") or "Laya decision failed"))

    def close(self) -> None:
        with self.lock:
            process = self.process
            self.process = None
            if process is not None and process.poll() is None:
                try:
                    if process.stdin:
                        process.stdin.write('{"cmd":"quit"}\n')
                        process.stdin.flush()
                    process.wait(timeout=5)
                except Exception:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
            if self.stderr_stream is not None:
                self.stderr_stream.close()
                self.stderr_stream = None


class ArdyGenerator:
    """One persistent local ARDY worker plus NPZ -> action-bus conversion."""

    def __init__(self, raw: dict[str, Any] | None, base_dir: Path, status_writer: StatusWriter):
        raw = raw or {}
        self.enabled = bool(raw.get("enabled", False))
        self.preload_enabled = bool(raw.get("preload", False))
        self.status_writer = status_writer
        self.start_timeout = float(raw.get("start_timeout_seconds", 180.0))
        self.generation_timeout = float(raw.get("generation_timeout_seconds", 240.0))
        self.keep_files = max(0, int(raw.get("keep_files", 0)))
        self.steps = max(1, min(10, int(raw.get("generation_steps", 10))))
        self.warmup_enabled = bool(raw.get("warmup", True))
        self.warmed = False
        self.lock = threading.Lock()
        self.protocol: queue.Queue[dict[str, Any]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.reader: threading.Thread | None = None
        self.stderr_stream = None
        self.python = self.runtime = self.converter = self.bus_source = None
        self.motion_dir = self.generated_dir = self.log_path = None
        self.converter_process: subprocess.Popen[str] | None = None
        self.converter_stderr = None
        if not self.enabled:
            return

        def resolve(value: str) -> Path:
            path = Path(os.path.expandvars(value))
            return path.resolve() if path.is_absolute() else (base_dir / path).resolve()

        self.python = resolve(str(raw.get("python") or ""))
        self.runtime = resolve(str(raw.get("runtime") or ""))
        self.converter = resolve(str(raw.get("converter") or "convert_ardy_clip.py"))
        self.bus_source = resolve(str(raw.get("bus_source") or ""))
        self.motion_dir = resolve(str(raw.get("motion_dir") or "generated-motions"))
        self.generated_dir = resolve(str(raw.get("generated_dir") or "generated-clips"))
        self.log_path = resolve(str(raw.get("log_file") or "ardy-worker.log"))
        for label, path in (
            ("ARDY python", self.python), ("ARDY runtime", self.runtime),
            ("ARDY converter", self.converter), ("motion-bus source", self.bus_source),
        ):
            if not path.exists():
                raise ValueError(f"{label} not found: {path}")
        self.motion_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_protocol(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self.protocol.put(value)

    def _next(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ARDY worker timed out")
        try:
            return self.protocol.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("ARDY worker timed out") from exc

    def _ensure_started(self) -> None:
        if not self.enabled:
            raise RuntimeError("ARDY generation is disabled")
        if self.process is not None and self.process.poll() is None:
            return
        while not self.protocol.empty():
            self.protocol.get_nowait()
        self.stderr_stream = self.log_path.open("a", encoding="utf-8")
        self.status_writer.write(state="ardy_loading")
        self.process = subprocess.Popen(
            [str(self.python), "-u", str(self.runtime)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr_stream,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = threading.Thread(
            target=self._read_protocol, args=(self.process,), name="ardy-protocol", daemon=True
        )
        self.reader.start()
        deadline = time.monotonic() + self.start_timeout
        while True:
            message = self._next(deadline)
            if message.get("status") == "ready":
                self.status_writer.write(
                    state="ardy_ready", model=message.get("model"), fps=message.get("fps")
                )
                return
            if message.get("status") == "error":
                raise RuntimeError(str(message.get("message") or "ARDY failed to load"))
            if self.process.poll() is not None:
                raise RuntimeError(f"ARDY worker exited with {self.process.returncode}")

    def preload(self) -> None:
        if not self.enabled:
            return
        with self.lock:
            self._ensure_converter()
            self._ensure_started()
            self._warmup_locked()

    def _ensure_converter(self) -> None:
        if self.converter_process is not None and self.converter_process.poll() is None:
            return
        assert self.python is not None and self.converter is not None and self.bus_source is not None
        converter_log = self.log_path.with_name("ardy-converter.log")
        self.converter_stderr = converter_log.open("a", encoding="utf-8")
        self.converter_process = subprocess.Popen(
            [str(self.python), "-u", str(self.converter), "--worker",
             "--bus-source", str(self.bus_source)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.converter_stderr,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert self.converter_process.stdout is not None
        line = self.converter_process.stdout.readline()
        message = json.loads(line) if line else {}
        if message.get("status") != "ready":
            raise RuntimeError("ARDY converter worker failed to start")

    def _convert_clip(self, npz_path: Path, clip_path: Path) -> dict[str, Any]:
        self._ensure_converter()
        assert self.converter_process is not None
        assert self.converter_process.stdin is not None and self.converter_process.stdout is not None
        self.converter_process.stdin.write(json.dumps({
            "cmd": "convert", "npz": str(npz_path), "clip": str(clip_path),
        }, ensure_ascii=False) + "\n")
        self.converter_process.stdin.flush()
        line = self.converter_process.stdout.readline()
        message = json.loads(line) if line else {}
        if message.get("status") != "done":
            raise RuntimeError(str(message.get("message") or "ARDY converter failed"))
        return message

    def _warmup_locked(self) -> None:
        if self.warmed or not self.warmup_enabled:
            return
        assert self.motion_dir is not None
        output = self.motion_dir / ".ardy-warmup.npz"
        self.status_writer.write(state="ardy_warming", steps=1)
        self._send({
            "cmd": "generate",
            "prompt": "A person gently shifts weight once and returns to a neutral standing posture.",
            "duration": 0.4, "seed": 0, "steps": 1,
            "guidance": 2.0, "postprocess": False, "output": str(output),
        })
        deadline = time.monotonic() + self.generation_timeout
        while True:
            message = self._next(deadline)
            status = message.get("status")
            if status == "done":
                break
            if status == "error":
                raise RuntimeError(str(message.get("message") or "ARDY warmup failed"))
        try:
            output.unlink(missing_ok=True)
        except OSError:
            pass
        self.warmed = True
        self.status_writer.write(state="ardy_warm", steps=1)

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None or self.process.poll() is not None:
            raise RuntimeError("ARDY worker is unavailable")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def generate(self, prompt: str, duration: float, event_id: str, seed: int = -1) -> dict[str, Any]:
        with self.lock:
            self._ensure_converter()
            self._ensure_started()
            self._warmup_locked()
            seed_key = f"\0seed{int(seed)}" if int(seed) >= 0 else ""
            cache_key = hashlib.sha256(
                (prompt.strip().casefold() + f"\0{duration:.3f}\0ardy-core40-minilm-v1-steps{self.steps}{seed_key}").encode("utf-8")
            ).hexdigest()[:20]
            npz_path = self.motion_dir / f"ardy-{cache_key}.npz"
            clip_path = self.generated_dir / f"ardy-{cache_key}.json"
            if npz_path.is_file() and clip_path.is_file():
                clip = json.loads(clip_path.read_text(encoding="utf-8"))
                return {
                    "npz": str(npz_path), "clip": str(clip_path),
                    "clip_file": clip_path.name, "frames": len(clip.get("frames") or []),
                    "fps": clip.get("fps"), "cache_key": cache_key,
                    "cached": True, "worker": None, "steps": self.steps, "seed": int(seed),
                }
            self.status_writer.write(
                state="ardy_generating", source_event_id=event_id,
                prompt=prompt, duration_seconds=duration, steps=self.steps,
            )
            self._send({
                "cmd": "generate", "prompt": prompt, "duration": duration,
                "seed": int(seed), "steps": self.steps, "guidance": 2.0, "postprocess": True,
                "output": str(npz_path),
            })
            deadline = time.monotonic() + self.generation_timeout
            done = None
            while True:
                message = self._next(deadline)
                status = message.get("status")
                if status == "progress":
                    self.status_writer.write(
                        state="ardy_progress", source_event_id=event_id,
                        message=message.get("message"),
                    )
                elif status == "done":
                    done = message
                    break
                elif status == "error":
                    raise RuntimeError(str(message.get("message") or "ARDY generation failed"))
                if self.process is not None and self.process.poll() is not None:
                    raise RuntimeError(f"ARDY worker exited with {self.process.returncode}")
            convert_started = time.perf_counter()
            info = self._convert_clip(npz_path, clip_path)
            conversion_seconds = round(time.perf_counter() - convert_started, 3)
            if not clip_path.is_file() or clip_path.stat().st_size <= 0:
                raise RuntimeError(f"ARDY converter reported success but clip is missing: {clip_path}")
            self._prune()
            return {
                "npz": str(npz_path), "clip": str(clip_path),
                "clip_file": clip_path.name, "frames": info.get("frames"),
                "fps": info.get("fps"), "cache_key": cache_key,
                "cached": False, "worker": done, "steps": self.steps,
                "conversion_seconds": conversion_seconds, "seed": int(seed),
            }

    def _prune(self) -> None:
        if self.keep_files <= 0:
            return
        for root, pattern in ((self.generated_dir, "mind-*.json"), (self.motion_dir, "mind-*.npz")):
            files = sorted(root.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
            for path in files[self.keep_files:]:
                try:
                    path.unlink()
                except OSError:
                    pass

    def close(self) -> None:
        with self.lock:
            converter = self.converter_process
            self.converter_process = None
            if converter is not None and converter.poll() is None:
                try:
                    if converter.stdin:
                        converter.stdin.write('{"cmd":"quit"}\n')
                        converter.stdin.flush()
                    converter.wait(timeout=5)
                except Exception:
                    converter.terminate()
            if self.converter_stderr is not None:
                self.converter_stderr.close()
                self.converter_stderr = None
            process = self.process
            self.process = None
            if process is not None and process.poll() is None:
                try:
                    if process.stdin:
                        process.stdin.write('{"cmd":"quit"}\n')
                        process.stdin.flush()
                    process.wait(timeout=5)
                except Exception:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
            if self.stderr_stream is not None:
                self.stderr_stream.close()
                self.stderr_stream = None


class SpeechWorker:
    """Latest-wins speech queue with hard interruption and mouth reset."""

    def __init__(
        self,
        raw: dict[str, Any] | None,
        base_dir: Path,
        status_writer: StatusWriter,
        state_callback=None,
        ready_callback=None,
    ):
        raw = raw or {}
        self.enabled = bool(raw.get("enabled", False))
        self.python = (base_dir / str(raw.get("python") or "")).resolve() if self.enabled else None
        self.script = (base_dir / str(raw.get("bridge_script") or "")).resolve() if self.enabled else None
        self.tts_url = str(raw.get("tts_url") or "http://127.0.0.1:8091/v1/audio/speech")
        self.voice_port = int(raw.get("voice_port", 39543))
        self.status_writer = status_writer
        self.state_callback = state_callback or (lambda _active: None)
        self.ready_callback = ready_callback or (lambda _source_event_id, _metadata: None)
        self.ready_dir = (base_dir / ".speech-ready").resolve()
        self.jobs: queue.Queue[tuple[str, str, str] | None] = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.current: subprocess.Popen[str] | None = None
        self.current_source = ""
        self.interrupted_pids: set[int] = set()
        self.thread: threading.Thread | None = None
        if self.enabled:
            if not self.python or not self.python.is_file():
                raise ValueError(f"speech python not found: {self.python}")
            if not self.script or not self.script.is_file():
                raise ValueError(f"speech bridge not found: {self.script}")
            self.thread = threading.Thread(target=self._run, name="speech-worker", daemon=True)
            self.thread.start()

    def submit(self, text: str, event_id: str, source_event_id: str = "") -> bool:
        if not self.enabled:
            return False
        clean = spoken_text(text)
        if not clean or is_presence_filler(text):
            if clean:
                self.status_writer.write(state="speech_skipped_presence", text=clean[:160])
            return False
        self.interrupt("new assistant reply")
        try:
            self.jobs.get_nowait()
        except queue.Empty:
            pass
        self.jobs.put_nowait((clean, event_id, source_event_id))
        return True

    def busy(self) -> bool:
        if not self.enabled:
            return False
        with self.lock:
            process = self.current
            running = process is not None and process.poll() is None
        return running or not self.jobs.empty()

    def interrupt(self, reason: str) -> bool:
        if not self.enabled:
            return False
        with self.lock:
            process = self.current
            if process is None or process.poll() is not None:
                return False
            self.interrupted_pids.add(process.pid)
            process.terminate()
        publish_closed_mouth(self.voice_port)
        self.state_callback(False)
        self.status_writer.write(state="speech_interrupted", reason=reason)
        return True

    def close(self) -> None:
        if not self.enabled:
            return
        self.stop_event.set()
        self.interrupt("bridge stopping")
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                pass
            self.jobs.put_nowait(None)
        if self.thread:
            self.thread.join(timeout=5)
        publish_closed_mouth(self.voice_port)

    def _run(self) -> None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        while not self.stop_event.is_set():
            job = self.jobs.get()
            if job is None:
                return
            text, event_id, source_event_id = job
            ready_file = self.ready_dir / f"{hashlib.sha256(event_id.encode('utf-8')).hexdigest()[:20]}.json"
            try:
                ready_file.unlink(missing_ok=True)
            except OSError:
                pass
            command = [
                str(self.python), str(self.script),
                "--text", text,
                "--tts-url", self.tts_url,
                "--voice-port", str(self.voice_port),
                "--ready-file", str(ready_file),
            ]
            started = time.perf_counter()
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            with self.lock:
                self.current = process
                self.current_source = source_event_id or event_id
            self.status_writer.write(
                state="speech_synthesizing", source_event_id=event_id,
                motion_source_event_id=source_event_id, text=text,
            )
            ready_metadata: dict[str, Any] = {}
            ready_announced = False
            while process.poll() is None and not self.stop_event.is_set():
                if ready_file.is_file():
                    try:
                        value = json.loads(ready_file.read_text(encoding="utf-8"))
                        if isinstance(value, dict):
                            ready_metadata = value
                    except (OSError, json.JSONDecodeError):
                        ready_metadata = {}
                    self.state_callback(True)
                    self.ready_callback(source_event_id, ready_metadata)
                    self.status_writer.write(
                        state="speaking", source_event_id=event_id,
                        motion_source_event_id=source_event_id, text=text,
                        audio=ready_metadata,
                    )
                    ready_announced = True
                    break
                time.sleep(0.015)
            stdout, stderr = process.communicate()
            with self.lock:
                if self.current is process:
                    self.current = None
                    self.current_source = ""
                interrupted = process.pid in self.interrupted_pids
                self.interrupted_pids.discard(process.pid)
            publish_closed_mouth(self.voice_port)
            if ready_announced:
                self.state_callback(False)
            try:
                ready_file.unlink(missing_ok=True)
            except OSError:
                pass
            if process.returncode == 0:
                result: dict[str, Any] = {}
                for line in reversed(stdout.splitlines()):
                    try:
                        candidate = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(candidate, dict):
                        result = candidate
                        break
                self.status_writer.write(
                    state="speech_complete",
                    source_event_id=event_id,
                    elapsed_seconds=round(time.perf_counter() - started, 3),
                    result=result,
                )
            elif interrupted:
                self.status_writer.write(
                    state="speech_cancelled",
                    source_event_id=event_id,
                    returncode=process.returncode,
                )
            elif not self.stop_event.is_set():
                self.status_writer.write(
                    state="speech_failed",
                    source_event_id=event_id,
                    returncode=process.returncode,
                    error=stderr[-1000:],
                )


class YuriEventBridge:
    def __init__(self, config: dict[str, Any], status_writer: StatusWriter, base_dir: Path | None = None):
        self.config = config
        self.status_writer = status_writer
        self.base_dir = base_dir or Path.cwd()
        self.stop_event = threading.Event()
        self.state_lock = threading.Lock()
        self.latest_user_event = ""
        self.plan_future: concurrent.futures.Future | None = None
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="intent"
        )
        self.last_user_by_session: dict[str, str] = {}
        self.last_user_event_by_session: dict[str, str] = {}
        self.last_user_fallback = ""
        self.last_user_event_fallback = ""
        self.motion_gates: dict[str, threading.Event] = {}
        self.assistant_motion_events: dict[str, threading.Event] = {}
        self.assistant_motion_text: dict[str, str] = {}
        self.pending_performance: dict[str, list[dict[str, Any]]] = {}
        self.speech_ready_meta: dict[str, dict[str, Any]] = {}
        self.motion_owned: set[str] = set()
        self.acting_from_reply: set[str] = set()
        self.seen = collections.deque(maxlen=512)
        self.seen_set: set[str] = set()
        self.last_user_activity = time.monotonic()
        self.rules = tuple(Rule.from_dict(item) for item in config.get("rules", []))
        self.resolver = IntentResolver(
            str(config["actor"]), self.rules, float(config.get("cooldown_seconds", 4.0))
        )
        self.hub = MotionHubClient(
            str(config.get("motion_hub_url", "http://127.0.0.1:39538")),
            float(config.get("motion_hub_timeout_seconds", 2.0)),
        )
        self.embodiment = EmbodimentController(config.get("embodiment"), self.base_dir)
        self.hearing_server: ThreadingHTTPServer | None = None
        hearing_cfg = config.get("hearing") or {}
        speech_cfg = config.get("speech") or {}
        self.hearing_owns_voice = bool(hearing_cfg.get("inbox_enabled", True)) and (
            str(speech_cfg.get("from_sse", "")).strip().casefold() not in {"1", "true", "yes"}
        )
        self.speech = SpeechWorker(
            config.get("speech"), self.base_dir, status_writer,
            state_callback=self.embodiment.set_speaking,
            ready_callback=self._on_speech_ready,
        )
        self.planner = AgnesMotionPlanner(config.get("planner"), self.base_dir)
        ardy_config = config.get("ardy") or {}
        library_value = str(ardy_config.get("library_file") or "../Mocap动作总线/clips/generated/library.json")
        library_path = Path(os.path.expandvars(library_value))
        if not library_path.is_absolute():
            library_path = (self.base_dir / library_path).resolve()
        self.library = ArdyLibrary(library_path)
        self.ardy = ArdyGenerator(ardy_config, self.base_dir, status_writer)
        self.laya = LayaReflex(config.get("laya"), self.base_dir, status_writer)
        recovery_config = config.get("pose_recovery") or {}
        recovery_value = str(
            recovery_config.get("request_file")
            or "../沃雅妮莎_AGI实时动捕/头发裙摆物理版/维护请求.json"
        )
        self.pose_recovery_file = Path(os.path.expandvars(recovery_value))
        if not self.pose_recovery_file.is_absolute():
            self.pose_recovery_file = (self.base_dir / self.pose_recovery_file).resolve()
        self.pose_recovery_enabled = bool(recovery_config.get("enabled", True))
        self.pose_recovery_duration = max(.2, min(2., float(recovery_config.get("duration_seconds", .8))))
        idle_config = config.get("curated_idle") or {}
        self.curated_idle_enabled = bool(idle_config.get("enabled", False))
        self.curated_idle_min_seconds = max(10.0, float(idle_config.get("min_idle_seconds", 45.0)))
        self.curated_idle_interval_min = max(15.0, float(idle_config.get("interval_min_seconds", 45.0)))
        self.curated_idle_interval_max = max(
            self.curated_idle_interval_min,
            float(idle_config.get("interval_max_seconds", 90.0)),
        )
        self.curated_idle_rng = random.Random(int(idle_config.get("seed", 20260926)))
        self.curated_idle_history = collections.deque(maxlen=3)
        self.curated_idle_actions: list[dict[str, Any]] = []
        catalog_value = str(idle_config.get("catalog_file") or "../Mocap动作总线/clips/generated/curated_catalog.json")
        catalog_path = Path(catalog_value)
        if not catalog_path.is_absolute():
            catalog_path = (self.base_dir / catalog_path).resolve()
        if self.curated_idle_enabled and catalog_path.is_file():
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.curated_idle_actions = curated_idle_candidates(catalog)
        self.next_idle_macro = time.monotonic() + self.curated_idle_min_seconds
        self.idle_thread: threading.Thread | None = None
        if self.ardy.enabled and self.ardy.preload_enabled:
            self.executor.submit(self._preload_ardy)
        if self.laya.enabled and self.laya.preload_enabled:
            self.executor.submit(self._preload_laya)
        if self.curated_idle_enabled and self.curated_idle_actions:
            self.status_writer.write(
                state="curated_idle_ready",
                actions=len(self.curated_idle_actions),
                interval_seconds=[self.curated_idle_interval_min, self.curated_idle_interval_max],
            )
            self.idle_thread = threading.Thread(
                target=self._idle_macro_loop, name="curated-idle", daemon=True
            )
            self.idle_thread.start()
        self._start_hearing_inbox()

    def stop(self) -> None:
        self.stop_event.set()
        with self.state_lock:
            self.pending_performance.clear()
        if self.hearing_server is not None:
            self.hearing_server.shutdown()
            self.hearing_server = None
        self.speech.close()
        self.embodiment.close()
        if self.idle_thread:
            self.idle_thread.join(timeout=3)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.laya.close()
        self.ardy.close()

    def _on_heard_speech(self, source: str = "mic") -> None:
        self.speech.interrupt("heard speech")
        self.embodiment.set_listening(12.0)
        if bool(self.config.get("interrupt_action_on_user_turn", True)):
            try:
                result = self.hub.stop(str(self.config["actor"]))
            except Exception as exc:
                LOG.debug("motion interrupt skipped: %s", exc)
                result = {"error": str(exc)}
            else:
                self.status_writer.write(state="heard_speech", source=source, result=result)

    def _start_hearing_inbox(self) -> None:
        hearing = self.config.get("hearing") or {}
        if not bool(hearing.get("inbox_enabled", True)):
            return
        port = int(hearing.get("listen_port") or 39545)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") != "/hearing":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                event = str(payload.get("event") or "")
                self.send_response(204)
                self.end_headers()
                threading.Thread(
                    target=bridge._handle_hearing_event,
                    args=(event, payload),
                    name="hearing-event",
                    daemon=True,
                ).start()

        try:
            class InboxServer(ThreadingHTTPServer):
                allow_reuse_address = True

            server = InboxServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            LOG.warning("hearing inbox unavailable on %s: %s", port, exc)
            return
        self.hearing_server = server
        threading.Thread(target=server.serve_forever, name="hearing-inbox", daemon=True).start()
        LOG.info("hearing inbox on 127.0.0.1:%s", port)

    def _handle_hearing_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "start":
            self._on_heard_speech("windows-whisper")
            return
        if event == "transcript":
            text = str(payload.get("text") or "").strip()
            if text:
                self._submit_semantic_plan(f"hearing-{time.time_ns()}", text)
            return
        if event != "reply":
            return
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        with self.state_lock:
            source_event_id = self.latest_user_event or f"hearing-reply-{time.time_ns()}"
            self.motion_gates.setdefault(source_event_id, threading.Event())
        speech_id = f"hearing-reply-{time.time_ns()}"
        self._record_assistant_motion_context(source_event_id, text)
        accepted = self.speech.submit(text, speech_id, source_event_id)
        if not accepted:
            with self.state_lock:
                self.pending_performance.pop(source_event_id, None)
            self._release_motion_gate(source_event_id)
        self._maybe_act_from_reply(source_event_id, text)

    def _preload_ardy(self) -> None:
        try:
            self.ardy.preload()
        except Exception as exc:
            LOG.warning("ARDY preload failed: %s", exc)
            self.status_writer.write(state="ardy_failed", error=str(exc))

    def _preload_laya(self) -> None:
        try:
            self.laya.preload()
        except Exception as exc:
            LOG.warning("Laya preload failed: %s", exc)
            self.status_writer.write(state="laya_failed", error=str(exc))

    def _idle_macro_loop(self) -> None:
        actor = str(self.config["actor"])
        while not self.stop_event.wait(0.75):
            now = time.monotonic()
            with self.state_lock:
                quiet_for = now - self.last_user_activity
                plan_future = self.plan_future
            if quiet_for < self.curated_idle_min_seconds or now < self.next_idle_macro:
                continue
            if self.speech.busy() or (plan_future is not None and not plan_future.done()):
                continue
            if self.embodiment.state(now) != "idle":
                continue
            try:
                live = self.hub.status()
                if (live.get("active_actions") or {}).get(actor):
                    continue
                if not self.hub.actor_online(live, actor):
                    continue
                recent = set(self.curated_idle_history)
                choices = [item for item in self.curated_idle_actions if item.get("id") not in recent]
                if not choices:
                    choices = list(self.curated_idle_actions)
                chosen = self.curated_idle_rng.choice(choices)
                result = self.hub.dispatch_generated(
                    actor, str(chosen["file"]), str(chosen["label"]),
                    str(chosen.get("mask") or "upper"), require_actor_online=True,
                )
                self.curated_idle_history.append(str(chosen["id"]))
                self._note_action(result)
                self.status_writer.write(
                    state="idle_macro_dispatched", route="curated_idle",
                    action=chosen, result=result, quiet_seconds=round(quiet_for, 1),
                )
                self.next_idle_macro = now + float(result.get("duration_seconds") or 3.0) + self.curated_idle_rng.uniform(
                    self.curated_idle_interval_min, self.curated_idle_interval_max
                )
            except Exception as exc:
                self.status_writer.write(state="idle_macro_deferred", error=str(exc))
                self.next_idle_macro = now + 10.0

    def _submit_semantic_plan(self, event_id: str, user_text: str) -> None:
        if not self.planner.enabled:
            return
        with self.state_lock:
            self.latest_user_event = event_id
            self.last_user_fallback = user_text
            self.last_user_event_fallback = event_id
            self.motion_gates[event_id] = threading.Event()
            self.assistant_motion_events[event_id] = threading.Event()
            self.assistant_motion_text[event_id] = ""
            self.speech_ready_meta.pop(event_id, None)
            previous = self.plan_future
            if previous is not None and not previous.running():
                previous.cancel()
            self.plan_future = self.executor.submit(
                self._plan_and_execute, event_id, user_text
            )
        self.embodiment.set_thinking(12.0)
        if local_motion_plan(user_text, "") is not None:
            accepted = self.speech.submit("好。", f"fast:{event_id}", event_id)
            if not accepted:
                self._release_motion_gate(event_id)

    def _is_current(self, event_id: str) -> bool:
        with self.state_lock:
            return event_id == self.latest_user_event and not self.stop_event.is_set()

    def _acting_still_live(self, event_id: str) -> bool:
        if self.stop_event.is_set() or not event_id:
            return False
        with self.speech.lock:
            playing_source = self.speech.current_source if self.speech.current is not None else ""
        with self.state_lock:
            if event_id == self.latest_user_event:
                return True
            if event_id in self.speech_ready_meta and playing_source in {event_id, ""}:
                return True
            return playing_source == event_id

    def _current_pose_context(self, live: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            live = live or self.hub.status()
        except Exception:
            live = {}
        actor = str(self.config["actor"])
        snapshot = ((live.get("pose_snapshots") or {}).get(actor) or {})

        def angle(name: str) -> float | None:
            raw = snapshot.get(name)
            if not isinstance(raw, list) or len(raw) != 4:
                return None
            norm = math.sqrt(sum(float(value) ** 2 for value in raw))
            if norm < 1e-8:
                return None
            w = max(-1.0, min(1.0, abs(float(raw[0]) / norm)))
            return round(math.degrees(2.0 * math.acos(w)), 2)

        sources = {str(item.get("id")): item for item in live.get("sources", []) if isinstance(item, dict)}
        active = (live.get("active_actions") or {}).get(actor)
        facing_angles = [value for value in (angle("Hips"), angle("Chest"), angle("Head")) if value is not None]
        arm_angles = [value for value in (
            angle("LeftUpperArm"), angle("RightUpperArm"),
            angle("LeftLowerArm"), angle("RightLowerArm"),
            angle("LeftHand"), angle("RightHand"),
        ) if value is not None]
        return {
            "motion": "acting" if active else "idle",
            "active_action": active,
            "agi_active": bool(sources.get("agi", {}).get("active", False)),
            "facing_user": bool(not active and facing_angles and max(facing_angles) < 30.0),
            "hands_neutral": bool(not active and arm_angles and max(arm_angles) < 25.0),
            "bone_angles_deg": {
                name: value for name in (
                    "Hips", "Chest", "Head", "LeftUpperArm", "RightUpperArm",
                    "LeftLowerArm", "RightLowerArm", "LeftHand", "RightHand",
                ) if (value := angle(name)) is not None
            },
        }

    def _record_assistant_motion_context(self, event_id: str, text: str) -> None:
        if not event_id:
            return
        extracted = stage_directions(text)
        current_pose = self._current_pose_context()
        cues = performance_plan(text, current_pose)
        with self.state_lock:
            event = self.assistant_motion_events.get(event_id)
            if event is None:
                event = threading.Event()
                self.assistant_motion_events[event_id] = event
            self.pending_performance[event_id] = cues
            self.assistant_motion_text[event_id] = extracted
            event.set()
        self.status_writer.write(
            state="assistant_motion_context", source_event_id=event_id,
            stage_directions=extracted, performance_cues=cues,
            current_pose=current_pose,
        )

    def _wait_for_assistant_motion_context(self, event_id: str) -> tuple[str, bool]:
        with self.state_lock:
            event = self.assistant_motion_events.get(event_id)
        if event is None:
            return "", False
        deadline = time.monotonic() + self.planner.assistant_context_timeout
        while not event.wait(0.1):
            if not self._is_current(event_id) or time.monotonic() >= deadline:
                break
        with self.state_lock:
            return self.assistant_motion_text.get(event_id, ""), event.is_set()

    def _release_motion_gate(self, event_id: str) -> None:
        with self.state_lock:
            gate = self.motion_gates.get(event_id)
        if gate is not None:
            gate.set()

    def _on_speech_ready(self, event_id: str, metadata: dict[str, Any]) -> None:
        with self.state_lock:
            cues = self.pending_performance.pop(event_id, [])
            self.speech_ready_meta[event_id] = dict(metadata)
        self.embodiment.start_performance(
            cues, float(metadata.get("audio_seconds") or 0.0)
        )
        self._release_motion_gate(event_id)
        self.status_writer.write(
            state="speech_motion_sync_ready", source_event_id=event_id,
            audio=metadata, performance_cues=cues,
        )

    def _claim_motion(self, event_id: str) -> bool:
        if not event_id:
            return False
        with self.state_lock:
            if event_id in self.motion_owned:
                return False
            self.motion_owned.add(event_id)
            return True

    def _maybe_act_from_reply(self, event_id: str, text: str) -> None:
        if not self.planner.enabled or not event_id:
            return
        directions = stage_directions(text)
        if not directions:
            return
        with self.state_lock:
            if event_id in self.motion_owned or event_id in self.acting_from_reply:
                return
            self.acting_from_reply.add(event_id)
            self.motion_gates.setdefault(event_id, threading.Event())
        self.executor.submit(self._generate_from_stage_directions, event_id, directions)

    def _generate_from_stage_directions(self, event_id: str, directions: str) -> None:
        if not self._claim_motion(event_id):
            return
        with self.state_lock:
            user_text = self.last_user_fallback
        self.status_writer.write(
            state="planning_acting", source_event_id=event_id,
            stage_directions=directions, provider=self.planner.provider,
        )
        try:
            live = self.hub.status()
            current_pose = self._current_pose_context(live)
            catalog = list(live.get("action_catalog") or []) + self.library.planner_catalog()
            plan = self.planner.plan(user_text, catalog, directions, current_pose)
            plan["assistant_action_text"] = directions
            plan["current_pose"] = current_pose
            self.status_writer.write(
                state="motion_planned", route="spark_acting",
                source_event_id=event_id, plan=plan,
            )
            if not self._acting_still_live(event_id):
                self.status_writer.write(state="motion_plan_stale", source_event_id=event_id, route="spark_acting")
                return
            self._execute_plan(event_id, user_text, plan, route="spark_acting")
        except Exception as exc:
            LOG.warning("stage-direction ARDY planning failed: %s", exc)
            self.status_writer.write(
                state="motion_plan_failed", source_event_id=event_id,
                route="spark_acting", error=str(exc),
            )

    def _execute_plan(self, event_id: str, user_text: str, plan: dict[str, Any], route: str) -> None:
        if plan["mode"] == "expression":
            if not self._wait_for_speech_gate(event_id):
                return
            self.embodiment.set_expression(
                str(plan.get("expression") or "Joy"),
                float(plan.get("duration_seconds") or 3.0),
                float(plan.get("intensity") or 0.8),
            )
            self.status_writer.write(
                state="expression_dispatched", route=route,
                source_event_id=event_id, plan=plan,
            )
            return
        acting = bool(str(plan.get("assistant_action_text") or "").strip())
        if plan["mode"] == "none" or (
            plan["confidence"] < self.planner.confidence_threshold and not acting
        ):
            self.status_writer.write(state="no_motion", source_event_id=event_id, plan=plan)
            return
        if plan["mode"] == "catalog":
            saved = self.library.get(str(plan["action_id"]))
            if saved is not None:
                if not self._wait_for_speech_gate(event_id):
                    return
                result = self.hub.dispatch_generated(
                    str(self.config["actor"]), str(saved["file"]),
                    str(saved["label"]), str(saved.get("mask") or plan["mask"]),
                    require_actor_online=bool(self.config.get("require_actor_online", True)),
                )
                saved = self.library.mark_used(str(saved["id"]), user_text)
                self.status_writer.write(
                    state="planned_dispatched", route="ardy_cache",
                    source_event_id=event_id, plan=plan,
                    saved_action=saved, result=result,
                )
                self._note_action(result)
                return
            intent = MotionIntent(
                intent_id=f"{event_id}:semantic",
                actor=str(self.config["actor"]), action_id=str(plan["action_id"]),
                mask=str(plan["mask"]), loop=False,
                reason=f"Spark acting planner: {plan['reason']}",
                source_event_id=event_id,
            )
            self._dispatch_catalog(intent)
            return
        if plan["mode"] == "generate":
            generated = self.ardy.generate(
                str(plan["ardy_prompt"]), float(plan["duration_seconds"]), event_id
            )
            if not self._acting_still_live(event_id) and not self._is_current(event_id):
                self.status_writer.write(
                    state="ardy_result_stale", source_event_id=event_id,
                    generated=generated,
                )
                return
            if not self._wait_for_speech_gate(event_id):
                return
            result = self.hub.dispatch_generated(
                str(self.config["actor"]), str(generated["clip_file"]),
                f"ARDY · {(str(plan.get('assistant_action_text') or user_text))[:48]}",
                str(plan["mask"]),
                require_actor_online=bool(self.config.get("require_actor_online", True)),
            )
            saved = self.library.record(generated, plan, user_text)
            self.status_writer.write(
                state="planned_dispatched", route=route,
                source_event_id=event_id, plan=plan,
                generated=generated, saved_action=saved, result=result,
            )
            self._note_action(result)

    def _wait_for_speech_gate(self, event_id: str, timeout: float = 60.0) -> bool:
        with self.state_lock:
            if event_id in self.speech_ready_meta:
                return self._acting_still_live(event_id)
            gate = self.motion_gates.get(event_id)
        if gate is None:
            return True
        deadline = time.monotonic() + timeout
        while not gate.wait(0.1):
            if not self._acting_still_live(event_id) and not self._is_current(event_id):
                return False
            if time.monotonic() >= deadline:
                self.status_writer.write(
                    state="motion_gate_timeout", source_event_id=event_id
                )
                return False
        return self._acting_still_live(event_id) or self._is_current(event_id)

    def _note_action(self, result: dict[str, Any]) -> None:
        self.embodiment.set_action(
            float(result.get("duration_seconds") or 0.0),
            str(result.get("mask") or "full"),
        )
        if self.pose_recovery_enabled:
            threading.Thread(
                target=self._recover_pose_after_action,
                args=(dict(result),),
                name="pose-recovery",
                daemon=True,
            ).start()

    def _recover_pose_after_action(self, result: dict[str, Any]) -> None:
        duration = max(0., float(result.get("duration_seconds") or 0.))
        if self.stop_event.wait(duration + .12):
            return
        expected_id = str(result.get("id") or "")
        try:
            live = self.hub.status()
            active = (live.get("active_actions") or {}).get(str(self.config["actor"]))
            # A newer action owns the body now; its own completion will issue
            # the corresponding recovery request.
            if active and str(active.get("id") or "") != expected_id:
                return
            if active:
                if self.stop_event.wait(.25):
                    return
                live = self.hub.status()
                active = (live.get("active_actions") or {}).get(str(self.config["actor"]))
                if active:
                    return
            request = {
                "action": "blend_to_rest",
                "mask": str(result.get("mask") or "full"),
                "duration": self.pose_recovery_duration,
                "action_id": expected_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            self.pose_recovery_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.pose_recovery_file.with_suffix(self.pose_recovery_file.suffix + ".tmp")
            temporary.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.pose_recovery_file)
            self.status_writer.write(state="pose_recovery_requested", request=request)
        except Exception as exc:
            self.status_writer.write(state="pose_recovery_failed", error=str(exc))

    def _dispatch_catalog(self, intent: MotionIntent) -> None:
        if not self._wait_for_speech_gate(intent.source_event_id):
            return
        result = self.hub.dispatch(
            intent,
            require_actor_online=bool(self.config.get("require_actor_online", True)),
        )
        self.status_writer.write(
            state="planned_dispatched", route="catalog",
            intent=intent.as_dict(), result=result,
        )
        self._note_action(result)

    def _plan_and_execute(self, event_id: str, user_text: str) -> None:
        started = time.perf_counter()
        self.status_writer.write(
            state="planning_motion", source_event_id=event_id, user_text=user_text
        )
        try:
            live = self.hub.status()
            current_pose = self._current_pose_context(live)
            saved_catalog = self.library.planner_catalog()
            fast_plan = local_motion_plan(user_text, "")
            if fast_plan is not None:
                self._claim_motion(event_id)
                fast_plan["assistant_action_text"] = ""
                fast_plan["current_pose"] = current_pose
                self.status_writer.write(
                    state="motion_planned", route="local_reflex",
                    source_event_id=event_id,
                    elapsed_seconds=round(time.perf_counter() - started, 3),
                    plan=fast_plan,
                )
                if fast_plan["mode"] == "expression":
                    if not self._wait_for_speech_gate(event_id):
                        return
                    self.embodiment.set_expression(
                        str(fast_plan.get("expression") or "Joy"),
                        float(fast_plan.get("duration_seconds") or 3.0),
                        float(fast_plan.get("intensity") or 0.8),
                    )
                    self.status_writer.write(
                        state="expression_dispatched", route="local_reflex",
                        source_event_id=event_id, plan=fast_plan,
                    )
                    return
                generated = self.ardy.generate(
                    str(fast_plan["ardy_prompt"]),
                    float(fast_plan["duration_seconds"]), event_id,
                )
                if not self._is_current(event_id) or not self._wait_for_speech_gate(event_id):
                    return
                result = self.hub.dispatch_generated(
                    str(self.config["actor"]), str(generated["clip_file"]),
                    f"ARDY · {user_text[:48]}", str(fast_plan["mask"]),
                    require_actor_online=bool(self.config.get("require_actor_online", True)),
                )
                saved = self.library.record(generated, fast_plan, user_text)
                self.status_writer.write(
                    state="reflex_dispatched", route="local_reflex",
                    source_event_id=event_id, plan=fast_plan,
                    generated=generated, saved_action=saved, result=result,
                )
                self._note_action(result)
                return
            if self.laya.enabled and saved_catalog:
                try:
                    reflex = self.laya.decide(user_text, saved_catalog)
                    self.status_writer.write(
                        state="laya_decision", source_event_id=event_id,
                        decision=reflex,
                    )
                    action_id = str(reflex.get("reflex_action_id") or "")
                    if action_id and self._is_current(event_id) and not needs_assistant_context(user_text):
                        saved = self.library.get(action_id)
                        if saved is not None:
                            self._claim_motion(event_id)
                            if not self._wait_for_speech_gate(event_id):
                                return
                            result = self.hub.dispatch_generated(
                                str(self.config["actor"]), str(saved["file"]),
                                str(saved["label"]), str(saved.get("mask") or "full"),
                                require_actor_online=bool(self.config.get("require_actor_online", True)),
                            )
                            saved = self.library.mark_used(action_id, user_text)
                            self.status_writer.write(
                                state="reflex_dispatched", route="laya",
                                source_event_id=event_id, decision=reflex,
                                saved_action=saved, result=result,
                            )
                            self._note_action(result)
                            return
                    if (
                        bool(reflex.get("reflex_no_motion"))
                        and local_motion_plan(user_text, "") is None
                        and self._is_current(event_id)
                    ):
                        self.status_writer.write(
                            state="reflex_no_motion", route="laya",
                            source_event_id=event_id, decision=reflex,
                        )
                        return
                except Exception as exc:
                    LOG.warning("Laya reflex failed; using Spark: %s", exc)
                    self.status_writer.write(
                        state="laya_failed", source_event_id=event_id, error=str(exc)
                    )
            catalog = list(live.get("action_catalog") or []) + saved_catalog
            assistant_action_text, context_received = self._wait_for_assistant_motion_context(event_id)
            with self.state_lock:
                already_owned = event_id in self.motion_owned or event_id in self.acting_from_reply
            if already_owned:
                return
            if not context_received:
                self.status_writer.write(
                    state="motion_context_timeout", source_event_id=event_id,
                    waited_seconds=self.planner.assistant_context_timeout,
                )
                return
            plan = local_motion_plan(user_text, assistant_action_text)
            if plan is None:
                plan = self.planner.plan(user_text, catalog, assistant_action_text, current_pose)
            plan["assistant_action_text"] = assistant_action_text
            plan["current_pose"] = current_pose
            self.status_writer.write(
                state="motion_planned", source_event_id=event_id,
                elapsed_seconds=round(time.perf_counter() - started, 3), plan=plan,
            )
            if not self._is_current(event_id):
                self.status_writer.write(state="motion_plan_stale", source_event_id=event_id)
                return
            if not self._claim_motion(event_id):
                return
            self._execute_plan(event_id, user_text, plan, route="spark")
        except Exception as exc:
            LOG.warning("semantic motion planning failed: %s", exc)
            self.status_writer.write(
                state="motion_plan_failed", source_event_id=event_id, error=str(exc)
            )
            if not self._is_current(event_id):
                return
            fallback = self.resolver.resolve(user_text, "", event_id)
            if fallback is None:
                return
            try:
                if not self._wait_for_speech_gate(event_id):
                    return
                result = self.hub.dispatch(
                    fallback,
                    require_actor_online=bool(self.config.get("require_actor_online", True)),
                )
            except Exception as fallback_exc:
                self.status_writer.write(
                    state="deferred", intent=fallback.as_dict(), error=str(fallback_exc)
                )
            else:
                self.status_writer.write(
                    state="fallback_dispatched", intent=fallback.as_dict(), result=result
                )
                self._note_action(result)
        finally:
            with self.state_lock:
                self.assistant_motion_events.pop(event_id, None)
                self.assistant_motion_text.pop(event_id, None)

    def _remember_event(self, event_id: str) -> bool:
        if not event_id:
            return True
        if event_id in self.seen_set:
            return False
        if len(self.seen) == self.seen.maxlen:
            self.seen_set.discard(self.seen[0])
        self.seen.append(event_id)
        self.seen_set.add(event_id)
        return True

    def handle_event(self, event: dict[str, Any]) -> MotionIntent | None:
        if event.get("type") != "message":
            return None
        role = str(event.get("role") or "")
        text = str(event.get("text") or "")
        session_id = str(event.get("session_id") or "")
        event_id = str(event.get("id") or "")
        if not self._remember_event(event_id):
            return None
        if role == "user":
            with self.state_lock:
                self.last_user_activity = time.monotonic()
                self.next_idle_macro = self.last_user_activity + self.curated_idle_min_seconds
            if session_id:
                self.last_user_by_session[session_id] = text
                self.last_user_event_by_session[session_id] = event_id
            self.last_user_fallback = text
            self.last_user_event_fallback = event_id
            return None
        if role != "assistant":
            return None
        user_text = self.last_user_by_session.get(session_id, self.last_user_fallback)
        return self.resolver.resolve(user_text, text, event_id)

    def _event_url(self) -> str:
        base = str(self.config.get("yurios_character_url") or "").rstrip("/")
        if not base:
            raise ValueError("yurios_character_url is required")
        # The multi-character host dispatches
        # /api/characters/<id>/events -> the child's /api/events route.
        return f"{base}/events?presence=0&body=0"

    def _stream_once(self) -> None:
        request = urllib.request.Request(
            self._event_url(), headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        )
        timeout = float(self.config.get("sse_timeout_seconds", 35.0))
        try:
            response_cm = LOCAL_OPENER.open(request, timeout=timeout)
        except (OSError, TimeoutError, urllib.error.URLError):
            response_cm = None
        if response_cm is not None:
            with response_cm as response:
                self.status_writer.write(state="connected", event_url=self._event_url(), via="direct")
                self._consume_sse(response)
            return
        proc = subprocess.Popen(
            ["wsl", "-d", "Ubuntu-24.04", "--", "curl", "-sS", "-N", "--no-buffer",
             "-H", "Accept: text/event-stream", self._event_url()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            self.status_writer.write(state="connected", event_url=self._event_url(), via="wsl-curl")
            self._consume_sse(proc.stdout)
        finally:
            if proc.poll() is None:
                proc.terminate()

    def _consume_sse(self, response) -> None:
        for raw in response:
                if self.stop_event.is_set():
                    return
                if isinstance(raw, str):
                    line = raw.strip()
                else:
                    line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    LOG.warning("invalid SSE JSON ignored")
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "user_speaking":
                    self._on_heard_speech("yurios-mic")
                    continue
                if event.get("type") == "message" and event.get("role") == "user":
                    self.speech.interrupt("new user turn")
                    self.embodiment.set_listening(1.2)
                    if bool(self.config.get("interrupt_action_on_user_turn", True)):
                        try:
                            result = self.hub.stop(str(self.config["actor"]))
                        except Exception as exc:
                            LOG.debug("motion interrupt skipped: %s", exc)
                        else:
                            self.status_writer.write(
                                state="action_interrupted",
                                source_event_id=str(event.get("id") or ""),
                                result=result,
                            )
                intent = self.handle_event(event)
                if event.get("type") == "message" and event.get("role") == "user":
                    self._submit_semantic_plan(
                        str(event.get("id") or ""), str(event.get("text") or "")
                    )
                if self.planner.enabled and event.get("type") == "message" and event.get("role") == "assistant":
                    intent = None
                if intent is not None:
                    try:
                        result = self.hub.dispatch(
                            intent,
                            require_actor_online=bool(self.config.get("require_actor_online", True)),
                        )
                    except Exception as exc:  # keep the event stream alive
                        LOG.warning("intent %s was not dispatched: %s", intent.intent_id, exc)
                        self.status_writer.write(
                            state="deferred", intent=intent.as_dict(), error=str(exc)
                        )
                    else:
                        LOG.info("dispatched %s -> %s", intent.intent_id, intent.action_id)
                        self.status_writer.write(
                            state="dispatched", intent=intent.as_dict(), result=result
                        )
                        self._note_action(result)
                if event.get("type") == "message" and event.get("role") == "assistant":
                    session_id = str(event.get("session_id") or "")
                    source_event_id = self.last_user_event_by_session.get(
                        session_id, self.last_user_event_fallback
                    )
                    assistant_text = str(event.get("text") or "")
                    self._record_assistant_motion_context(source_event_id, assistant_text)
                    proactive = bool(event.get("proactive") or event.get("unheard"))
                    if self.hearing_owns_voice or proactive:
                        continue
                    accepted = self.speech.submit(
                        assistant_text, str(event.get("id") or ""),
                        source_event_id,
                    )
                    if not accepted:
                        with self.state_lock:
                            self.pending_performance.pop(source_event_id, None)
                        self._release_motion_gate(source_event_id)
                    self._maybe_act_from_reply(source_event_id, assistant_text)

    def run(self) -> None:
        delay = 1.0
        self.status_writer.write(state="starting", rules=len(self.rules))
        while not self.stop_event.is_set():
            try:
                self._stream_once()
                delay = 1.0
            except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
                if self.stop_event.is_set():
                    break
                LOG.warning("YuriOS event stream unavailable: %s", exc)
                self.status_writer.write(state="reconnecting", error=str(exc), retry_seconds=delay)
                self.stop_event.wait(delay)
                delay = min(delay * 2.0, 15.0)
        self.status_writer.write(state="stopped")


def _wsl_ipv4() -> str:
    try:
        raw = subprocess.check_output(
            ["wsl", "-d", "Ubuntu-24.04", "--", "hostname", "-I"],
            text=True, timeout=5,
        )
    except Exception:
        return ""
    for token in raw.split():
        if token.count(".") == 3 and not token.startswith("172.17."):
            return token
    return (raw.split() or [""])[0]


def _yurios_reachable(url: str) -> bool:
    try:
        LOCAL_OPENER.open(url, timeout=2).read(64)
        return True
    except Exception:
        return False


def resolve_yurios_character_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    hosts = [parsed.hostname or "127.0.0.1"]
    wsl_ip = _wsl_ipv4()
    if wsl_ip and wsl_ip not in hosts:
        hosts.append(wsl_ip)
    for host in hosts:
        health = f"{parsed.scheme}://{host}:{parsed.port or 8768}/api/health"
        if _yurios_reachable(health):
            netloc = f"{host}:{parsed.port}" if parsed.port else host
            return urllib.parse.urlunparse(parsed._replace(netloc=netloc))
    return url


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")
    if not str(data.get("actor") or "").strip():
        raise ValueError("actor is required")
    character_url = str(data.get("yurios_character_url") or "").strip()
    if character_url:
        data["yurios_character_url"] = resolve_yurios_character_url(character_url)
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--resolve", help="resolve one user sentence and print JSON without dispatching")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    rules = tuple(Rule.from_dict(item) for item in config.get("rules", []))
    if args.resolve is not None:
        intent = IntentResolver(str(config["actor"]), rules, cooldown_seconds=0).resolve(
            args.resolve, "", "manual", now=0
        )
        print(json.dumps(intent.as_dict() if intent else None, ensure_ascii=False, indent=2))
        return
    status_path = args.config.parent / str(config.get("status_file", "运行状态.json"))
    audit_path = args.config.parent / str(config.get("audit_file", "审计记录.jsonl"))
    bridge = YuriEventBridge(
        config,
        StatusWriter(status_path, audit_path),
        args.config.parent.resolve(),
    )
    signal.signal(signal.SIGINT, lambda *_: bridge.stop())
    signal.signal(signal.SIGTERM, lambda *_: bridge.stop())
    bridge.run()


if __name__ == "__main__":
    main()
