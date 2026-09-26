# Vodyanitsa Virtual Idol Runtime

Windows-based virtual idol integration for Vodyanitsa, combining a conversational
mind, local speech recognition and synthesis, VMC motion routing, generated
actions, Blender avatar control, and hair/skirt physics.

## Main components

- `outputs/虚拟偶像心智桥`: hearing, dialogue, embodiment, action planning, and tests
- `outputs/虚拟偶像语音桥`: Qwen3-TTS playback and viseme bridge
- `outputs/Mocap动作总线`: VMC publish/subscribe hub, routing, action clips, and tests
- `outputs/沃雅妮莎_AGI实时动捕`: Vodyanitsa Blender/VMC integration scripts
- `outputs/薇斯纳_AGI实时动捕`: Vesna Blender/VMC integration scripts
- `outputs/Mocap屏幕桥`: window capture bridge source
- `outputs/自主AI人开源方案`: architecture and implementation notes

Each component contains its own Chinese usage notes and launch scripts. The most
recent end-to-end handoff is in `outputs/交接_沃雅妮莎心智语音动作.md`.

## Local-only assets

Large Blender scenes, character models, textures, audio files, packaged runtimes,
virtual environments, logs, and machine-local configuration are intentionally
excluded from Git. They remain in the original workspace and must be supplied
locally when reproducing the full runtime. Secrets are read from environment
variables or external runtime configuration and must not be committed.
