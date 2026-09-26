from pathlib import Path

whisper = Path("/home/mozi/yurios-eval/src/yurios/desktop/voice/backends/stt_whisper.py")
text = whisper.read_text(encoding="utf-8")
old = 'audio, language="en", beam_size=1, vad_filter=False)'
new = "audio, language=None, beam_size=1, vad_filter=False)"
if old not in text:
    raise SystemExit("whisper transcribe snippet not found")
whisper.write_text(text.replace(old, new, 1), encoding="utf-8")

voice = Path("/home/mozi/yurios-eval/src/yurios/world/routes/voice_ws.py")
v = voice.read_text(encoding="utf-8")
old_heard = "    heard = Utterance(rt, guard)\n"
new_heard = "    heard = Utterance(rt, guard)\n    heard_announced = False\n"
if old_heard not in v:
    raise SystemExit("heard = Utterance not found")
if "heard_announced = False" not in v:
    v = v.replace(old_heard, new_heard, 1)

needle = "                    heard.feed(msg[\"bytes\"])"
insert = """                    if not heard_announced:
                        heard_announced = True
                        rt.hub.publish("user_speaking", {"source": "mic"})
                    heard.feed(msg["bytes"])"""
if "user_speaking" not in v:
    if needle not in v:
        raise SystemExit("heard.feed not found")
    v = v.replace(needle, insert, 1)

old_reset = """            if kind == "reset_audio":
                heard.reset()
                continue
"""
new_reset = """            if kind == "reset_audio":
                heard.reset()
                heard_announced = False
                continue
"""
if old_reset in v and "heard_announced = False\n                continue" not in v:
    v = v.replace(old_reset, new_reset, 1)

voice.write_text(v, encoding="utf-8")
print("patched whisper + voice_ws")
