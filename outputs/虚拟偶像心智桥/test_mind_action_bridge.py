from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from mind_action_bridge import (
    ArdyLibrary,
    IntentResolver,
    MotionHubClient,
    Rule,
    YuriEventBridge,
    StatusWriter,
    load_config,
    local_motion_plan,
    is_presence_filler,
    needs_assistant_context,
    is_dance_command,
    curated_idle_candidates,
    parse_agnes_plan,
    performance_plan,
    stage_directions,
    spoken_text,
)
from embodiment_controller import EmbodimentController
from laya_reflex_worker import exact_saved_action, select_reflex


class _FakeWriter(StatusWriter):
    def __init__(self):
        self.rows = []

    def write(self, **values):
        self.rows.append(values)


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.rules = (
            Rule(("你好", "挥手"), "wave", "arms", False, "greet"),
            Rule(("跳舞",), "dance", "full", False, "dance"),
        )

    def test_explicit_user_word_resolves_allowlisted_action(self):
        intent = IntentResolver("vodyanitsa", self.rules).resolve(
            "你好，挥挥手", "很高兴见到你", "evt-1", now=10
        )
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action_id, "wave")
        self.assertEqual(intent.mask, "arms")

    def test_assistant_word_alone_never_triggers(self):
        intent = IntentResolver("vodyanitsa", self.rules).resolve(
            "介绍一下你自己", "你好，我是……", "evt-2", now=10
        )
        self.assertIsNone(intent)

    def test_cooldown_suppresses_repeat(self):
        resolver = IntentResolver("vodyanitsa", self.rules, cooldown_seconds=4)
        self.assertIsNotNone(resolver.resolve("你好", "", "evt-1", now=10))
        self.assertIsNone(resolver.resolve("你好", "", "evt-2", now=12))
        self.assertIsNotNone(resolver.resolve("你好", "", "evt-3", now=15))

    def test_event_pairing_and_deduplication(self):
        bridge = object.__new__(YuriEventBridge)
        bridge.last_user_by_session = {}
        bridge.last_user_event_by_session = {}
        bridge.last_user_fallback = ""
        bridge.last_user_event_fallback = ""
        bridge.state_lock = threading.Lock()
        bridge.last_user_activity = 0.0
        bridge.next_idle_macro = 0.0
        bridge.curated_idle_min_seconds = 30.0
        bridge.seen = __import__("collections").deque(maxlen=8)
        bridge.seen_set = set()
        bridge.resolver = IntentResolver("vodyanitsa", self.rules, cooldown_seconds=0)
        self.assertIsNone(bridge.handle_event(
            {"type": "message", "id": "u1", "role": "user", "text": "跳舞", "session_id": "s1"}
        ))
        intent = bridge.handle_event(
            {"type": "message", "id": "a1", "role": "assistant", "text": "好", "session_id": "s1"}
        )
        self.assertEqual(intent.action_id, "dance")
        self.assertIsNone(bridge.handle_event(
            {"type": "message", "id": "a1", "role": "assistant", "text": "好", "session_id": "s1"}
        ))

    def test_shipped_config_has_no_legacy_action_fallbacks(self):
        config = load_config(Path(__file__).with_name("config.json"))
        self.assertEqual(config["rules"], [])
        self.assertTrue(config["planner"]["enabled"])
        self.assertEqual(config["planner"].get("provider"), "spark")
        self.assertTrue(config["ardy"]["enabled"])
        self.assertFalse(config["speech"].get("from_sse"))

    def test_ordinary_chat_stage_directions_queue_ardy(self):
        bridge = object.__new__(YuriEventBridge)
        bridge.state_lock = threading.Lock()
        bridge.planner = type("P", (), {"enabled": True})()
        bridge.motion_owned = set()
        bridge.acting_from_reply = set()
        bridge.motion_gates = {}
        queued = []
        bridge.executor = type("E", (), {
            "submit": lambda _self, fn, *args: queued.append(args),
        })()
        bridge._maybe_act_from_reply("u1", "今晚很安静。")
        self.assertEqual(queued, [])
        bridge._maybe_act_from_reply("u1", "*她轻轻点头，望向你。* 我在。")
        self.assertEqual(queued, [("u1", "她轻轻点头，望向你。")])
        self.assertIn("u1", bridge.acting_from_reply)
        queued.clear()
        bridge.motion_owned.add("u2")
        bridge._maybe_act_from_reply("u2", "*她挥挥手。* 你好")
        self.assertEqual(queued, [])

    def test_presence_filler_lines_are_not_spoken(self):
        self.assertTrue(is_presence_filler("Hey. I'm here. I've been waiting."))
        self.assertTrue(is_presence_filler("*she smiles* I'm here."))
        self.assertFalse(is_presence_filler("你好，今天过得怎么样？"))
        self.assertFalse(is_presence_filler("The weather is quiet and I saved you tea."))

    def test_tts_text_removes_stage_directions_and_emotion_tags(self):
        self.assertEqual(
            spoken_text("*她挥挥手。* [happy] 你好，很高兴见到你。"),
            "你好，很高兴见到你。",
        )

    def test_stage_directions_are_extracted_as_motion_context(self):
        self.assertEqual(
            stage_directions("好。 *她轻轻摇曳。* 然后 *脚尖在地面画出小弧线。*"),
            "她轻轻摇曳。 脚尖在地面画出小弧线。",
        )

    def test_stage_directions_build_timed_speech_performance(self):
        plan = performance_plan(
            "*她缓缓转身，望向你。* 我在这里。*她微微低头，露出羞涩笑意。* 你呢？"
        )
        kinds = [cue["kind"] for cue in plan]
        self.assertIn("turn", kinds)
        self.assertIn("look", kinds)
        self.assertIn("lower_head", kinds)
        self.assertIn("smile", kinds)
        first = min(cue["start"] for cue in plan if cue["kind"] == "turn")
        second = min(cue["start"] for cue in plan if cue["kind"] == "lower_head")
        self.assertLess(first, second)
        already_facing = performance_plan(
            "*她缓缓转身，望向你。* 你好。", {"facing_user": True}
        )
        self.assertNotIn("turn", [cue["kind"] for cue in already_facing])
        self.assertIn("look", [cue["kind"] for cue in already_facing])

    def test_assistant_motion_context_is_attached_to_matching_user_turn(self):
        bridge = object.__new__(YuriEventBridge)
        bridge.state_lock = threading.Lock()
        bridge.assistant_motion_events = {"u1": threading.Event()}
        bridge.assistant_motion_text = {"u1": ""}
        bridge.pending_performance = {}
        bridge.status_writer = _FakeWriter()
        bridge.config = {"actor": "vodyanitsa"}
        bridge.hub = type("Hub", (), {"status": lambda _self: {}})()
        bridge._record_assistant_motion_context("u1", "好。*她原地轻轻摇曳。*")
        self.assertTrue(bridge.assistant_motion_events["u1"].is_set())
        self.assertEqual(bridge.assistant_motion_text["u1"], "她原地轻轻摇曳。")

    def test_status_writer_keeps_append_only_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = StatusWriter(root / "status.json", root / "audit.jsonl")
            writer.write(state="connected")
            writer.write(state="speaking", event="a1")
            rows = [json.loads(line) for line in (root / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["state"] for row in rows], ["connected", "speaking"])
            self.assertTrue(all("monotonic_ms" in row for row in rows))
            self.assertEqual(json.loads((root / "status.json").read_text(encoding="utf-8"))["state"], "speaking")

    def test_embodiment_publishes_blink_gaze_and_speech_state(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = EmbodimentController(
                {"enabled": True, "seed": 7, "status_file": "state.json"},
                Path(directory), autostart=False,
            )
            controller.next_blink = controller.started
            controller.sample(controller.started + 0.01)
            bones, blends, mode = controller.sample(controller.started + 0.08)
            self.assertEqual(mode, "idle")
            self.assertIn("LeftEye", bones)
            self.assertGreater(blends["Blink"], 0.9)
            controller.set_speaking(True)
            _, _, mode = controller.sample(controller.started + 0.08)
            self.assertEqual(mode, "speaking")
            controller.set_expression("Joy", 2.0, 0.9)
            _, blends, _ = controller.sample(time.monotonic())
            self.assertEqual(blends["Joy"], 0.9)
            now = controller.started + 1.6
            before, _, _ = controller.sample(now)
            controller.start_performance([
                {"kind": "lower_head", "start": 0.0, "duration": 1.0, "intensity": 1.0},
                {"kind": "smile", "start": 0.0, "duration": 1.0, "intensity": 1.0},
            ], 2.0)
            controller.performance_start = now - 0.5
            after, blends, _ = controller.sample(now)
            self.assertLess(abs(sum(a * b for a, b in zip(before["Head"], after["Head"]))), 0.9999)
            self.assertGreater(blends["Joy"], 0.4)
            controller.set_speaking(False)
            controller.performance_duration = 0.0
            controller.next_idle_cue = controller.started
            controller.sample(controller.started + 3.0)
            controller.sample(controller.idle_cue_start + controller.idle_cue_duration * 0.5)
            self.assertTrue(any(value.startswith("idle:") for value in controller.performance_active))

    def test_idle_layers_breathe_shift_weight_and_keep_relaxed_arms(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = EmbodimentController(
                {"enabled": True, "seed": 11, "status_file": "state.json"},
                Path(directory), autostart=False,
            )
            controller.next_idle_cue = controller.started + 1000.0
            chest_x = []
            hip_z = []
            head_y = []
            left_xs = []
            for step in range(0, 240):
                now = controller.started + step / 30.0
                bones, blends, mode = controller.sample(now)
                self.assertEqual(mode, "idle")
                chest_x.append(abs(bones["Chest"][1]))
                hip_z.append(abs(bones["Hips"][3]))
                head_y.append(abs(bones["Head"][2]))
                arms = controller.arm_directions(bones)
                left_xs.append(abs(arms["left_upper"][0]))
                self.assertLess(abs(arms["left_upper"][0]), 0.32)
                self.assertGreater(abs(arms["left_upper"][0]), 0.16)
                self.assertLess(arms["left_upper"][1], -0.94)
                self.assertGreater(abs(arms["right_upper"][0]), 0.16)
                self.assertIn("Blink", blends)
            self.assertGreater(max(chest_x), 0.012)
            self.assertGreater(max(hip_z), 0.008)
            self.assertGreater(max(head_y), 0.008)
            self.assertNotAlmostEqual(max(chest_x), min(chest_x), places=4)

    def test_listening_is_quieter_and_speaking_nods_more(self):
        with tempfile.TemporaryDirectory() as directory:
            idle = EmbodimentController(
                {"enabled": True, "seed": 3, "status_file": "idle.json"},
                Path(directory), autostart=False,
            )
            listen = EmbodimentController(
                {"enabled": True, "seed": 3, "status_file": "listen.json"},
                Path(directory), autostart=False,
            )
            speak = EmbodimentController(
                {"enabled": True, "seed": 3, "status_file": "speak.json"},
                Path(directory), autostart=False,
            )
            listen.set_listening(30)
            speak.set_speaking(True)
            now = idle.started + 1.7
            idle_bones, _, idle_mode = idle.sample(now)
            listen_bones, _, listen_mode = listen.sample(now)
            speak_bones, _, speak_mode = speak.sample(now)
            self.assertEqual((idle_mode, listen_mode, speak_mode), ("idle", "listening", "speaking"))
            self.assertGreater(abs(idle_bones["Hips"][3]), abs(listen_bones["Hips"][3]))
            self.assertGreater(abs(speak_bones["Head"][1]), abs(idle_bones["Head"][1]) * 0.9)

    def test_thinking_looks_away_after_listening(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = EmbodimentController(
                {"enabled": True, "seed": 5, "status_file": "think.json"},
                Path(directory), autostart=False,
            )
            controller.set_listening(8)
            listen_mode = controller.state(controller.started + 0.2)
            controller.set_thinking(8)
            controller.next_gaze = controller.started
            think_now = controller.started + 0.4
            bones, _, think_mode = controller.sample(think_now)
            self.assertEqual(listen_mode, "listening")
            self.assertEqual(think_mode, "thinking")
            self.assertGreater(abs(controller.target_yaw), 6.0)
            controller.set_speaking(True)
            self.assertEqual(controller.state(think_now), "speaking")
            controller.set_speaking(False)
            self.assertEqual(controller.state(time.monotonic()), "settling")

    def test_idle_fidgets_include_body_cues(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = EmbodimentController(
                {"enabled": True, "seed": 21, "status_file": "state.json"},
                Path(directory), autostart=False,
            )
            seen = set()
            now = controller.started
            for _ in range(40):
                controller.next_idle_cue = now
                controller.sample(now)
                seen.add(controller.idle_cue_kind)
                now = controller.next_idle_cue
            self.assertTrue({"look", "look_around", "weight_settle"} <= seen)

    def test_common_motion_templates_avoid_second_model_call(self):
        jump = local_motion_plan("跳跃", "她踮脚跃起后稳稳落地。")
        self.assertEqual(jump["mode"], "generate")
        self.assertIn("both feet clearly leaving the floor", jump["ardy_prompt"])
        dance = local_motion_plan("跳舞", "她轻轻摇曳。")
        self.assertIn("clearly visible dance", dance["ardy_prompt"])
        self.assertTrue(is_dance_command("跳支舞"))
        self.assertIsNotNone(local_motion_plan("来一段舞", ""))
        run = local_motion_plan("跑步", "")
        self.assertIn("jogs visibly in place", run["ardy_prompt"])
        smile = local_motion_plan("笑一个", "她露出笑容。")
        self.assertEqual((smile["mode"], smile["expression"]), ("expression", "Joy"))
        self.assertTrue(needs_assistant_context("跳舞"))
        self.assertFalse(needs_assistant_context("跳跃"))

    def test_curated_idle_only_uses_reviewed_idle_actions(self):
        candidates = curated_idle_candidates({"actions": [
            {"id": "safe", "category": "idle", "human_reviewed": True, "quality": {"safe_for_idle": True, "review_required": False}},
            {"id": "review", "category": "idle", "quality": {"safe_for_idle": False, "review_required": True}},
            {"id": "wave", "category": "conversation", "quality": {"safe_for_idle": True, "review_required": False}},
        ]})
        self.assertEqual([item["id"] for item in candidates], ["safe"])

    def test_laya_semantic_override_uses_clear_saved_motion_match(self):
        action, basis, margin = select_reflex(
            {"penguin": 0.8197, "spin": 0.5353}, "spin", 0.5555, 0.9553,
            semantic_threshold=0.75, semantic_override_threshold=0.80,
            semantic_margin=0.12, choice_threshold=0.62, motion_threshold=0.08,
        )
        self.assertEqual((action, basis), ("penguin", "semantic_override"))
        self.assertGreater(margin, 0.28)

    def test_laya_semantic_override_rejects_ambiguous_neighbors(self):
        action, basis, _ = select_reflex(
            {"wave-left": 0.83, "wave-right": 0.80}, "wave-right", 0.55, 0.95,
            semantic_threshold=0.75, semantic_override_threshold=0.80,
            semantic_margin=0.12, choice_threshold=0.62, motion_threshold=0.08,
        )
        self.assertIsNone(action)
        self.assertEqual(basis, "slow_planner")

    def test_laya_exact_user_example_handles_short_saved_command(self):
        self.assertEqual(
            exact_saved_action("跳舞！", [
                {"id": "dance", "examples": ["跳舞"]},
                {"id": "bow", "examples": ["鞠躬"]},
            ]),
            "dance",
        )

    def test_ardy_library_persists_and_learns_examples(self):
        with tempfile.TemporaryDirectory() as directory:
            library = ArdyLibrary(Path(directory) / "library.json")
            generated = {
                "cache_key": "abc123", "clip_file": "ardy-abc123.json",
                "npz": "ardy-abc123.npz", "frames": 100, "fps": 20.0,
            }
            plan = {
                "ardy_prompt": "A person waddles two steps then waves the left hand.",
                "duration_seconds": 5.0, "mask": "full",
                "assistant_action_text": "她像企鹅一样摇摆两步，然后挥左手。",
            }
            saved = library.record(generated, plan, "像企鹅一样走两步再挥手")
            self.assertEqual(saved["id"], "ardy-abc123")
            reused = library.mark_used(saved["id"], "企鹅步然后挥挥手")
            self.assertEqual(reused["use_count"], 2)
            self.assertEqual(saved["assistant_action_examples"], ["她像企鹅一样摇摆两步，然后挥左手。"])
            self.assertEqual(len(library.planner_catalog()), 1)
            reloaded = ArdyLibrary(Path(directory) / "library.json")
            self.assertEqual(reloaded.get(saved["id"])["use_count"], 2)

    def test_semantic_plan_accepts_catalog_or_bounded_ardy(self):
        catalog = parse_agnes_plan(
            '{"mode":"catalog","action_id":"dance","mask":"full","confidence":0.91}',
            {"dance"},
        )
        self.assertEqual(catalog["action_id"], "dance")
        generated = parse_agnes_plan(
            '{"mode":"generate","ardy_prompt":"A person walks like a penguin, waves left, then returns to neutral.",'
            '"duration_seconds":12,"mask":"full","confidence":0.88}',
            {"dance"},
        )
        self.assertEqual(generated["mode"], "generate")
        self.assertEqual(generated["duration_seconds"], 10.0)

    def test_semantic_plan_rejects_hallucinated_catalog_action(self):
        with self.assertRaisesRegex(ValueError, "outside the live catalog"):
            parse_agnes_plan(
                '{"mode":"catalog","action_id":"invented","confidence":1}', {"dance"}
            )

    def test_scene_validation_rejects_unavailable_window_interaction(self):
        with self.assertRaisesRegex(ValueError, "unavailable scene objects"):
            parse_agnes_plan(
                '{"mode":"generate","ardy_prompt":"A person walks to a window and touches it.",'
                '"duration_seconds":5,"confidence":0.9}',
                set(), {"window", "chair"},
            )


class HubClientTests(unittest.TestCase):
    def setUp(self):
        captured = self.captured = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send({
                    "action_catalog": [{"id": "wave"}],
                    "subscribers": [{"profile_id": "vodyanitsa"}],
                })

            def do_POST(self):
                size = int(self.headers["Content-Length"])
                captured.append(json.loads(self.rfile.read(size)))
                self._send({"playing": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = MotionHubClient(f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_dispatch_validates_live_actor_and_catalog(self):
        from mind_action_bridge import MotionIntent
        result = self.client.dispatch(MotionIntent("i1", "vodyanitsa", "wave", "arms", False, "x", "a1"))
        self.assertTrue(result["playing"])
        self.assertEqual(self.captured, [{
            "actor": "vodyanitsa", "id": "wave", "mask": "arms", "loop": False
        }])

    def test_loopback_client_has_proxy_bypass_handler(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            direct = MotionHubClient(f"http://127.0.0.1:{self.server.server_port}")
            self.assertIn("wave", direct.action_ids(direct.status()))

    def test_stop_uses_actor_scoped_command(self):
        self.client.stop("vodyanitsa")
        self.assertEqual(self.captured, [{"actor": "vodyanitsa", "command": "stop"}])

    def test_generated_dispatch_uses_separate_endpoint(self):
        self.client.dispatch_generated(
            "vodyanitsa", "mind-abc.json", "ARDY semantic motion", "full"
        )
        self.assertEqual(self.captured, [{
            "actor": "vodyanitsa", "file": "mind-abc.json",
            "label": "ARDY semantic motion", "mask": "full", "loop": False,
        }])


if __name__ == "__main__":
    unittest.main()
