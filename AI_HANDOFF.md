# AI 接手与复现手册

这份文件是新机器克隆仓库后的首要入口。目标不是假装仓库已经包含所有运行资产，而是让接手的 AI 能先验证源码，再明确补齐外部组件，最后恢复完整的“听觉 -> 心智 -> 语音/口型 -> 动作总线 -> Blender 角色”链路。

## 1. 不可破坏的架构约束

- YuriOS 管理角色、记忆、目标和对话事件；Blender/PMX 与 VMC 动作总线是身体的权威端。
- `voice_avatar_bridge.py` 独占实际音频播放和 A/I/U/E/O 口型时序。心智层不得直接写嘴部 shape key。
- `behavior` 以 30 Hz 连续提供待机身体、眼神和眨眼；一次性 ARDY 动作结束后必须释放其骨骼 mask。
- 沃雅妮莎的中立姿势不是四元数 identity，也不是 ARDY 首尾帧。双臂应接近向下、略向外约 12 度。
- 不要恢复已回滚的 Hips 根位移跳跃。它会破坏裙摆刚体；真正离地需要单独的物理感知方案。
- 密钥只放环境变量或外部 `.env`，不得提交到 Git、角色 Vault、日志或验证报告。

## 2. 仓库包含与不包含的内容

仓库已包含：

- 心智动作桥、听觉桥、语音口型桥的 Python 源码与测试。
- VMC 动作总线源码、路由测试和已转换的 `clips/generated/ardy-*.json` 动作。
- 沃雅妮莎/薇斯纳的 Blender 集成脚本与历史验证记录。
- 当前动作库元数据和架构、组件使用说明。

仓库因体积、授权或安全原因不包含：

- YuriOS 源码、WSL 虚拟环境、角色 Vault、中文 BGE 模型和模型 API 密钥。
- Qwen3-TTS 1.7B vLLM-Omni 服务、`akari2.wav` 声纹和各 Python 虚拟环境。
- ARDY Core/MiniLM 权重及运行环境；Laya System One 环境及 BGE 模型。
- Blender 可执行文件、MMD Tools 插件、`.blend`/`.pmx`/纹理和其他角色资产。
- PyInstaller 产物 `Mocap动作总线.exe`。可先直接运行仓库内的 Python 源码。

因此，纯 Git 克隆可以执行静态检查和单元测试，也能启动源码动作总线；完整角色链路还需要资产提供者传递上述本地组件。

## 3. 首次克隆后的自动检查

在仓库根目录使用 PowerShell：

```powershell
git status --short --branch
python --version
python -m compileall -q `
  'outputs/虚拟偶像心智桥' `
  'outputs/虚拟偶像语音桥' `
  'outputs/Mocap动作总线/source'
python -m unittest discover -s 'outputs/Mocap动作总线/source' -p 'test_*.py'
python -m unittest discover -s 'outputs/虚拟偶像心智桥' -p 'test_*.py'
```

先记录失败原因，不要为通过测试擅自删除角色特定的安全逻辑。`sounddevice`、`faster-whisper`、CUDA 或本地模型缺失导致的集成测试跳过/失败，应归类为环境缺失。

语音桥的基础依赖文件在 `outputs/虚拟偶像语音桥/requirements.txt`。建议新建仓库本地环境：

```powershell
py -3.11 -m venv 'outputs/虚拟偶像语音桥/.venv'
& 'outputs/虚拟偶像语音桥/.venv/Scripts/python.exe' -m pip install -U pip
& 'outputs/虚拟偶像语音桥/.venv/Scripts/python.exe' -m pip install -r 'outputs/虚拟偶像语音桥/requirements.txt'
```

听觉还需要 `faster-whisper`，动作规划/生成所需依赖由 Laya 和 ARDY 各自的外部环境提供。不要把多个互相冲突的 CUDA 栈强塞进同一个环境。

## 4. 必须先改的机器路径

复制 `outputs/虚拟偶像心智桥/config.json` 为本机配置前，逐项修改：

| 配置/文件 | 当前含义 | 新机器动作 |
| --- | --- | --- |
| `hearing.model_path` | faster-whisper-base 快照 | 改为本机模型目录，或先关闭听觉 |
| `laya.python`、`laya.hf_home`、`laya.embed_model` | Laya/BGE 环境 | 改路径；未安装时设 `laya.enabled=false` |
| `ardy.python`、`ardy.runtime`、`ardy.motion_dir` | ARDY 环境 | 改路径；未安装时设 `ardy.enabled=false` |
| `planner.credential_wsl_file` | WSL YuriOS `.env` | 改为本机 WSL 用户和 runtime 路径 |
| `voice_avatar_bridge.py` 的 `DEFAULT_REFERENCE_AUDIO` | `akari2.wav` | 提供合法声纹并改路径 |
| `启动YuriOS心智服务.cmd` | 固定 WSL 用户目录 | 改 `YURIOS_ROOT`、源码和 venv 路径 |
| `启动沃雅妮莎.py` | 固定 MMD Tools addons 路径 | 改为本机插件目录 |

可用下面的审计命令继续寻找绝对路径：

```powershell
rg -n 'C:/|C:\\Users|D:/|/home/' outputs -g '*.py' -g '*.cmd' -g '*.json'
```

`clips/generated/library.json` 中旧 NPZ 绝对路径是来源记录。现成 JSON clip 播放不依赖这些 NPZ；只有重新生成或重转时才需要更新。

## 5. 外部服务契约

恢复完整链路前逐项满足：

| 服务 | 地址/端口 | 最低验收 |
| --- | --- | --- |
| YuriOS | `http://127.0.0.1:8768` | 角色 `yuri` 存在，`/api/chat` 可提交消息 |
| 动作总线 HTTP | `http://127.0.0.1:39538` | 首页/API 可访问，角色订阅可见 |
| AGI/Kimodo/ARDY 输入 | UDP `39539/39541/39542` | 来源包计数增长 |
| 语音/待机 | UDP `39543/39544` | mouth/behavior 来源包计数增长 |
| 听觉 inbox | TCP `39545` | 心智桥启动后监听 |
| Qwen3-TTS | `http://127.0.0.1:8091/v1/audio/speech` | 返回可播放的 24 kHz PCM/WAV 流 |
| Spark MaaS | `https://maas-api.cn-huabei-1.xf-yun.com/v2` | `spark-x2.5-4b` 请求成功 |

Spark 密钥环境变量名为 `YURIOS_MODEL_API_KEY_SPARK`。当前实现也可以从配置指定的 WSL `.env` 读取同名变量。不要把值写进 `config.json`。

## 6. 分层启动顺序

### A. 只验证动作总线源码

若克隆中没有 `Mocap动作总线.exe`，直接启动源码：

```powershell
python 'outputs/Mocap动作总线/source/vmc_hub.py' `
  --state-dir 'outputs/Mocap动作总线/运行状态'
```

然后访问 `http://127.0.0.1:39538/`。先确认端口没有被旧进程占用。

### B. 恢复角色身体

1. 安装与原场景兼容的 Blender、MMD Tools 和 VMC 插件。
2. 从资产提供者取得沃雅妮莎的 `.blend`/PMX/纹理与头发裙摆物理场景。
3. 修正 `outputs/沃雅妮莎_AGI实时动捕/启动沃雅妮莎.py` 内插件路径。
4. 启动动作总线后，再运行 `打开沃雅妮莎头发裙摆物理版.cmd`。
5. 在总线面板确认 actor `vodyanitsa` 在线，并订阅 `behavior`、`voice/mouth` 和动作来源。

### C. 恢复心智与语言模型

1. 在 Ubuntu 24.04 WSL 安装并固定 YuriOS；原验证提交是 `d074a0b3ed14269c748757074734a7bba2122d28`。
2. 为中文记忆配置本地 `bge-small-zh-v1.5`，维度 512。不要换回英文默认 embedder。
3. 在 YuriOS runtime 的 `.env` 写入 `YURIOS_MODEL_API_KEY_SPARK=...`。
4. 创建角色 `yuri`，确认 chat/utility 模型为 OpenAI 兼容的 `spark-x2.5-4b`。
5. 修正并运行 `outputs/虚拟偶像心智桥/启动YuriOS心智服务.cmd`，打开 `8768` 面板验证。

### D. 恢复语音、口型和听觉

1. 启动 Qwen3-TTS 1.7B Base 的 vLLM-Omni 服务，监听 `8091`。
2. 提供 `akari2` 参考音频，修改 `DEFAULT_REFERENCE_AUDIO`。
3. 先运行 `outputs/虚拟偶像语音桥/测试实时口型.cmd`，确认能听见声音且总线 `voice` 来源更新。
4. 安装/下载 faster-whisper-base，更新 `hearing.model_path` 与 `hearing.input_device`。
5. 最后启动 `启动心智动作桥.cmd`，再启动 `启动听觉.cmd`。

### E. 可选增强

Laya 用于低延迟已保存动作检索，ARDY 用于生成未见动作。缺失时先在配置中关闭对应模块，跑通聊天、语音和确定性 `behavior`；不要用虚假的占位模型掩盖失败。开启 ARDY 后首次启动会预热，生成 clip 必须实际出现在 `outputs/Mocap动作总线/clips/generated` 才算成功。

## 7. 端到端验收

按以下顺序检查，不要只看进程是否存在：

1. `8768` 的 YuriOS 能在重启后回忆中文事实。
2. `39538` 面板显示 `vodyanitsa` 在线，`behavior` 约 30 FPS。
3. `头发裙摆物理版/实时连接状态.json` 中 `visible_blinks` 增长，`blink_peak` 大于 0。
4. 安静站立时上臂方向约为 `(±0.20, 0.00, -0.98)`，不是 A-pose。
5. 文本输入后收到角色回复；TTS 播放时 mouth 来源更新，结束后回到 `rest`。
6. 说“挥手”或“鞠躬”，动作播放后所有 mask 释放并回到待机。
7. 用户在角色说话时插话，旧语音、嘴型和动作同时取消。
8. 新生成动作先人工看 Blender 画面，再允许进入自动复用库。数值范围通过不等于视觉通过。

状态与诊断入口：

- 心智桥：`outputs/虚拟偶像心智桥/运行状态.json`、`审计记录.jsonl`。
- 拟人层：`outputs/虚拟偶像心智桥/拟人状态.json`。
- 角色：`outputs/沃雅妮莎_AGI实时动捕/头发裙摆物理版/实时连接状态.json`。
- 动作总线：`http://127.0.0.1:39538/` 与 `outputs/Mocap动作总线/运行状态`。

这些运行文件被 `.gitignore` 排除。排障时保留本地副本，但不要提交。

## 8. AI 接手工作规则

1. 开始前读本文件、`outputs/交接_沃雅妮莎心智语音动作.md` 及相关组件的 `使用说明.md`。
2. 先执行 `git status --short`；不得覆盖或回滚未知的本地改动。
3. 修改配置前做去密钥检查；修改动作/物理前保存可恢复的本地场景副本。
4. 每次只恢复一层，记录命令、端口、提交、模型版本和真实验收证据。
5. 不要把“HTTP 200”“收到 UDP 包”或“测试数值正常”当作最终角色视觉验收。
6. 不要把大型模型、角色资产或密钥直接提交到这个仓库；用明确的外部资产清单和校验值交接。

当前实现的详细行为、历史修复与实测数据见 `outputs/虚拟偶像心智桥/使用说明.md`。
