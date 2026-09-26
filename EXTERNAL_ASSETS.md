# 外部仓库、模型与私有资产清单

本仓库不上传大型模型、第三方完整仓库、角色资产、声纹或密钥。接手 AI 应按本文件从官方来源下载，并遵守各上游许可证。除特别说明外，不要把下载内容重新提交到本仓库。

## 1. 快速来源表

| 用途 | 官方来源 | 本项目固定版本/标识 | 放置与配置 |
| --- | --- | --- | --- |
| 心智、记忆、角色事件 | [yuri-os/YuriOS](https://github.com/yuri-os/YuriOS) | `d074a0b3ed14269c748757074734a7bba2122d28` | WSL；设置 `YURIOS_ROOT` |
| ARDY 动作生成源码 | [nv-tlabs/ardy](https://github.com/nv-tlabs/ardy) | `693f74d13b3d04a0a22ce127ee79c929dd89756b` | 独立 Python/CUDA 环境 |
| ARDY Core40 权重 | [nvidia/ARDY-Core-RP-20FPS-Horizon40](https://huggingface.co/nvidia/ARDY-Core-RP-20FPS-Horizon40) | 模型 ID 同左 | 配置 `ardy.runtime` 所在工程的模型目录 |
| 本项目 MiniLM ARDY 工具 | [intsuc/ardy-mini](https://github.com/intsuc/ardy-mini) | `5e9ce2da35af26583646cc8b73ac04b13a7c604a` | 独立目录；训练产物不在 Git |
| MiniLM 基座 | [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` | 仅重新蒸馏时需要 |
| ARDY 蒸馏文本资料 | [nvidia/SEED-Timeline-Annotations](https://huggingface.co/datasets/nvidia/SEED-Timeline-Annotations) | `b2cf916d8ef7a1e49fc4f0ce9e00c1981d3b9d8f` | 仅重新蒸馏时需要 |
| 千问语音源码 | [QwenLM/Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) | `022e286b98fbec7e1e916cb940cdf532cd9f488e` | TTS 独立环境 |
| 当前语音权重 | [Qwen/Qwen3-TTS-12Hz-1.7B-Base](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base) | 精确模型 ID同左 | TTS 模型目录；端口 `8091` |
| Qwen 在线服务运行时 | [vllm-project/vllm-omni](https://github.com/vllm-project/vllm-omni) | 需按 Qwen3-TTS 上游兼容说明选择版本 | WSL/Linux 独立环境 |
| Windows 听觉 | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 模型 `Systran/faster-whisper-base`，本机快照 `ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66` | 配置 `hearing.model_path` |
| 中文嵌入 | [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) | 模型 ID 同左，512 维 | YuriOS 与 Laya 各配置本地路径 |
| Laya System One | [convaiinnovations/laya](https://huggingface.co/convaiinnovations/laya) | PyPI `laya==0.3.20` | 独立 Windows venv；配置 `laya.python` |
| Blender MMD 导入 | [sugiany/blender_mmd_tools](https://github.com/sugiany/blender_mmd_tools) | 未从现有副本恢复提交号 | Blender 插件目录 |
| Blender VMC 接收 | [CashewTeam/VMC_Link_Reforged](https://github.com/CashewTeam/VMC_Link_Reforged) | 现有安装缺少 Git 元数据 | Blender extension；需保留本项目胯骨补丁 |

## 2. 建议下载目录

不要复制原作者机器上的 `C:/Users/mozi/...` 路径。新机器可在仓库外建立：

```text
D:/vody-runtime/
  src/yurios/
  src/ardy/
  src/ardy-mini/
  src/qwen3-tts/
  models/ardy-core40/
  models/qwen3-tts-1.7b-base/
  models/faster-whisper-base/
  models/bge-small-zh-v1.5/
  private/voices/
  private/characters/
```

路径可以不同，但随后必须同步修改 `outputs/虚拟偶像心智桥/config.json`、各 `.cmd` 和角色启动脚本。

## 3. 固定源码仓库

### YuriOS

```bash
git clone https://github.com/yuri-os/YuriOS.git /opt/vody-runtime/src/yurios
git -C /opt/vody-runtime/src/yurios checkout d074a0b3ed14269c748757074734a7bba2122d28
```

本项目验证的是此提交，不是任意最新版本。按照 YuriOS 自带的 `docs/getting-started.md` 建立 venv/runtime，再将 `启动YuriOS心智服务.cmd` 内路径改为本机位置。中文记忆必须使用下文的 BGE 模型，维度 512。

### ARDY 与 MiniLM 适配

```powershell
git clone https://github.com/nv-tlabs/ardy.git D:/vody-runtime/src/ardy
git -C D:/vody-runtime/src/ardy checkout 693f74d13b3d04a0a22ce127ee79c929dd89756b

git clone https://github.com/intsuc/ardy-mini.git D:/vody-runtime/src/ardy-mini
git -C D:/vody-runtime/src/ardy-mini checkout 5e9ce2da35af26583646cc8b73ac04b13a7c604a
```

`ardy-mini` 的 `THIRD_PARTY_MODELS_AND_DATA.md` 是权威的许可证和数据来源说明。当前运行链使用已蒸馏的 MiniLM 工件；该工件没有公共下载地址，见“私有资产”一节。若必须重新训练，才下载 MiniLM 基座与 SEED Timeline Annotations，并严格使用固定 revision。

### Qwen3-TTS

```powershell
git clone https://github.com/QwenLM/Qwen3-TTS.git D:/vody-runtime/src/qwen3-tts
git -C D:/vody-runtime/src/qwen3-tts checkout 022e286b98fbec7e1e916cb940cdf532cd9f488e
```

本项目调用的是 OpenAI 兼容接口 `POST /v1/audio/speech`，监听 `127.0.0.1:8091`。官方 Qwen 仓库只保证其公开示例；本机使用的 vLLM-Omni 流式封装和启动脚本若未单独交接，接手 AI 需要依据接口契约重新实现或从资产提供者处取得。不要把官方 README 中“当前仅离线推理”的示例误认为本项目的在线端点。

## 4. 下载大型模型

先安装 Hugging Face CLI：

```powershell
python -m pip install -U huggingface_hub
```

下载当前运行需要的模型：

```powershell
huggingface-cli download nvidia/ARDY-Core-RP-20FPS-Horizon40 `
  --local-dir D:/vody-runtime/models/ardy-core40

huggingface-cli download Qwen/Qwen3-TTS-12Hz-1.7B-Base `
  --local-dir D:/vody-runtime/models/qwen3-tts-1.7b-base

huggingface-cli download Systran/faster-whisper-base `
  --revision ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66 `
  --local-dir D:/vody-runtime/models/faster-whisper-base

huggingface-cli download BAAI/bge-small-zh-v1.5 `
  --local-dir D:/vody-runtime/models/bge-small-zh-v1.5
```

Qwen 模型也可从 [ModelScope 的 Qwen3-TTS 集合](https://modelscope.cn/collections/Qwen/Qwen3-TTS)下载。只能更换下载镜像，模型 ID和版本必须保持一致。

ARDY 上游默认文本编码器涉及 gated 的 Meta Llama 3 权重。当前生产链使用本项目蒸馏的 MiniLM 工件，因此不应为了运行现有桥额外拉取 Llama 3；只有重建原始教师链时才按 ARDY 上游 README 申请许可。

## 5. 安装 Laya 与听觉依赖

```powershell
py -3.11 -m venv D:/vody-runtime/envs/laya
& D:/vody-runtime/envs/laya/Scripts/python.exe -m pip install -U pip
& D:/vody-runtime/envs/laya/Scripts/python.exe -m pip install 'laya==0.3.20' 'sentence-transformers==6.1.0'

& 'outputs/虚拟偶像语音桥/.venv/Scripts/python.exe' -m pip install faster-whisper
```

原验证环境还使用 `torch==2.11.0+cu128`、`transformers==5.17.0`；接手 AI 应根据目标 CUDA 驱动安装兼容的 PyTorch 构建，不要盲目照搬 CUDA wheel。安装后验证：

```powershell
& D:/vody-runtime/envs/laya/Scripts/python.exe -m pip show laya
```

输出应显示版本 `0.3.20`，主页为 `https://huggingface.co/convaiinnovations/laya`。

## 6. Blender 插件

从上游安装：

- MMD Tools: `https://github.com/sugiany/blender_mmd_tools`
- VMC Link Reforged: `https://github.com/CashewTeam/VMC_Link_Reforged`

本项目现有 VMC 插件曾为 ARDY 胯骨高度做过本地修改：只允许受控 Hips 高度分量，不允许把全身骨长平移直接套到沃雅妮莎。公共上游不能自动恢复这项补丁。资产提供者应另行交接补丁，或接手 AI 根据 `outputs/交接_MiniLM与胯骨高度.md` 重新实现并做 Blender 视觉验收。

Blender 版本、插件提交和角色场景是一个兼容组合；在没有视觉验收前，不要自动升级其中任何一项。

## 7. 无法从公共上游恢复的私有资产

以下内容必须由资产拥有者通过私有文件传输、私有 Release 或受控对象存储交接，不能让 AI 从互联网猜或替换：

| 私有资产 | 原用途 | 接手要求 |
| --- | --- | --- |
| 沃雅妮莎/薇斯纳 `.blend`、PMX、纹理、物理场景 | 最终角色身体 | 连同授权说明和文件 SHA-256 交接 |
| `akari2.wav` | Qwen3-TTS 声纹克隆 | 必须确认声音使用授权；配置 `DEFAULT_REFERENCE_AUDIO` |
| `minilm-ardy-core40-timeline-100e` | 当前低延迟 ARDY 文本编码器 | 无公共下载地址；建议压缩后放私有 Release 并记录 SHA-256 |
| YuriOS 角色 Vault/runtime 数据 | 人格、记忆、目标、日志 | 含隐私；只走加密私有传输，不进 Git |
| Spark MaaS API key | `spark-x2.5-4b` 对话和动作润色 | 写入外部 `.env` 的 `YURIOS_MODEL_API_KEY_SPARK` |
| Qwen vLLM-Omni 在线流式封装/启动脚本 | 提供本项目的 `/v1/audio/speech` | 若不是纯上游文件，应单独进入私有源码仓库或本仓库的小型源码目录 |
| VMC Link 本地补丁 | 受控胯高映射 | 单独保存 patch，不能只留在 Blender 安装目录 |

私有资产交接时至少提供：文件名、用途、许可证/授权边界、字节大小、SHA-256、目标目录和对应配置键。没有这些元数据时，接手 AI 应停止完整链路恢复，只运行不依赖该资产的层。

## 8. 可选训练资料

只有重新蒸馏 MiniLM 时才执行：

```powershell
huggingface-cli download sentence-transformers/all-MiniLM-L6-v2 `
  --revision 1110a243fdf4706b3f48f1d95db1a4f5529b4d41 `
  --local-dir D:/vody-runtime/models/all-MiniLM-L6-v2

huggingface-cli download nvidia/SEED-Timeline-Annotations `
  --repo-type dataset `
  --revision b2cf916d8ef7a1e49fc4f0ce9e00c1981d3b9d8f `
  --local-dir D:/vody-runtime/data/seed-timeline-annotations
```

固定数据 revision 中 `timelines.jsonl` 的预期 SHA-256 是：

```text
379d6a5b86cea06b7201d485d19ee53512cc58449352b3cf113a95d1d27603d8
```

不要下载 BONES-SEED，除非确实要运行 ARDY 的可选运动约束 demo，并且已经独立接受其受限许可证。现有虚拟偶像运行链不需要该数据集。

## 9. 下载后检查

接手 AI 完成下载后应输出一张本地映射表，至少包含：

```text
component | upstream | revision/model id | local path | config key | verified
```

随后执行：

```powershell
rg -n 'C:/Users/mozi|D:/AI|D:/ardy-mini|/home/mozi' `
  AI_HANDOFF.md EXTERNAL_ASSETS.md outputs `
  -g '*.py' -g '*.cmd' -g '*.json'
```

逐项消除运行配置中的旧机器路径。历史验证 JSON 和来源元数据中的旧绝对路径可以保留，但必须确认运行代码不再读取它们。
