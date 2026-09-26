# 交接：沃雅妮莎实时动作、MiniLM 蒸馏、胯骨高度

写于 2026-09-25。下一位直接接着做，不要重做蒸馏，不要再训 MiniLM。

用户的目标是让 Blender 里的角色一边说话一边做动作。当前只打通了「英文动作句 → MiniLM ARDY → 动作总线 → 沃雅妮莎」。没有接千问、没有语音、没有自主决策。

## 现在机器上还开着

不要随便结束这些进程。Blender 是用 `Start-Process` 拉起来的，父 PowerShell 若退出，窗口会一起被关掉。

| 进程 | PID | 说明 |
|---|---|---|
| blender.exe | 41184 | 沃雅妮莎，头发裙摆物理。已订阅动作总线 |
| Mocap动作总线.exe | 16500 | 面板 `http://127.0.0.1:39538/`，状态文件里的 pid 就是它 |
| Mocap动作总线.exe | 15704 | 另一个总线进程，不要当垃圾清掉，先查它占了什么 |
| Mocap屏幕桥.exe | 39484 | 窗口捕获，给 AGI 用。这次实验没动它 |

订阅：`沃雅妮莎` / `vodyanitsa`，回传端口 63084。AGI 和 Kimodo 没开。

打开角色必须沿用这个方式，工作目录和用户脚本路径都不能省：

```powershell
$env:BLENDER_USER_SCRIPTS = 'C:\Users\mozi\Documents\Codex\2026-08-16\wo\work\mmd_auto\vendor\blender_user_scripts'
$wd = 'C:\Users\mozi\Documents\Codex\2026-09-22\codex-threads-01a0c798-e451-7d22-bd56-4\outputs\沃雅妮莎_AGI实时动捕'
$blender = 'C:\Users\mozi\Documents\Codex\2026-08-16\wo\work\mmd_auto\vendor\blender\blender-5.2.0-windows-x64\blender-5.2.0-windows-x64\blender.exe'
$script = Join-Path $wd '启动沃雅妮莎.py'
Start-Process -FilePath $blender -ArgumentList @('--factory-startup','--threads','4','--python',"`"$script`"",'--','--hair-physics') -WorkingDirectory $wd -PassThru
```

父进程要活着。用计划任务、`explorer.exe` 或重定向标准输出启动，窗口会马上退出。要关掉当前窗口，往下面这个文件写 `{"action":"snapshot_and_exit"}`，等进程消失后再开新的，并先删掉这个请求，否则新窗口会一订阅就退出：

`outputs\沃雅妮莎_AGI实时动捕\头发裙摆物理版\维护请求.json`

## 两条目录

动作和角色在：

`C:\Users\mozi\Documents\Codex\2026-09-22\codex-threads-01a0c798-e451-7d22-bd56-4`

ARDY 与蒸馏在：

- 工人：`C:\Users\mozi\Documents\Codex\2026-09-14\ardy-github-blender`
- 蒸馏仓库：`D:\ardy-mini`（[intsuc/ardy-mini](https://github.com/intsuc/ardy-mini)）
- Python：`C:\Users\mozi\Documents\Codex\2026-09-14\ardy-github-blender\work\.ardy-venv\Scripts\python.exe`
- Core40 检查点：`work\models\ARDY-Core-RP-20FPS-Horizon40`
- 去噪器 SHA-256：`1019D0BF269CF8D1B3E3E9B4A384A58C112672959B071279DDB65814D77660CD`，与 ardy-mini 测过的是同一个文件
- 显卡：RTX 5090 D v2，24 GB

## 已完成

### MiniLM 换掉 15 GB 文字编码器

教师缓存 64287 句，`D:\ardy-mini\artifacts\teacher-core40-timeline`，约 62 分钟。学生是 `sentence-transformers/all-MiniLM-L6-v2`，修订 `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`，本地目录 `D:\ardy-mini\artifacts\minilm-base`。

留下的是 100 轮那份（验证集第 94 轮最好）：

- 根条件余弦 0.9769，身体 0.9671
- 50 轮是 0.9759 / 0.9656，较差，不要用
- 目录：`D:\ardy-mini\artifacts\minilm-ardy-core40-timeline-100e`
- 联接：`D:\ardy-mini\artifacts\minilm-ardy-core40` 指向上面那个目录

第 90 轮之后验证集不再上升。同一套数据和 L6，继续加轮数没有用。用户同意不再为指标续训。

工人已经改过：

- `outputs\Ardy_Citlali_Bridge\runtime_config.json` 的 `ardy_source` 是 `D:\ardy-mini`
- `runtime.py` 设置 `TEXT_ENCODER=minilm` 和上面的路径
- 冒烟测试：一句英文编码成 `(1, 1, 2048)`，显存大约 0.91 GB。以前单是 Llama 3 8B 编码器就要大约 15 GB

加载 MiniLM 时必须 `HF_HUB_OFFLINE=1`，权重都在本机。直连 Hugging Face 很慢；下文件用 `hf-mirror.com` 且不要走 `127.0.0.1:10808` 代理。

### 同一句、同一种子，MiniLM 和 8B 的差别

5 秒、20 FPS、10 步、引导 2、种子 0。

| 提示 | 条件余弦 | 对齐后关节差 | 根节点差 |
|---|---:|---:|---:|
| A person walks forward and waves their right hand. | 0.990 | 3.5 cm | 5.4 cm |
| A person dances happily, stepping side to side and moving both arms. | 0.974 | 2.6 cm | 2.8 cm |
| A person jumps. | 0.964 | 1.3 cm | 24 cm |
| A person stands still. | 0.997 | 0.4 cm | 0.1 cm |

8B 自己对走路挥手换一个种子：对齐后关节差 14.6 cm，根节点差 27.7 cm。蒸馏误差小于原版换种子。用户看过 MiniLM 的六段，除了跳和转身，认为差不多正确。

### 跳和转身不是蒸馏弄坏的

短句 `A person jumps.`：MiniLM 胯最高只比站直高 0.4 cm，8B 是 0.2 cm。两边都是下蹲大约 15 cm 再站起，数据里就没有离地。

写明离地的句子，MiniLM 会跳：

`A person crouches down and jumps high into the air, both feet leaving the ground, then lands.`

胯从约 96 cm 蹲到 64 cm，再升到 132 cm，比站直高 36 cm，有 9 帧明显高于 10 cm。文件：

- `work\..\motions\ardy_minilm_jump-high.npz`（在 `ardy-github-blender\outputs\motions`）
- 总线片段 `outputs\Mocap动作总线\clips\ardy-live-jump-high.json`

转身 `A person turns to the left and then faces forward again.`：MiniLM 胸约 78°、胯 62°、大腿 37–39°、脚 3–5°。8B 是胸 74°、胯 59°、大腿 36–39°、脚 3–4°。都是上身转、脚钉在地上。

### 接收端：只跟随胯的上下

插件：

`C:\Users\mozi\AppData\Roaming\Blender Foundation\Blender\5.2\extensions\user_default\vmc_link\main.py`

Blender 实际加载的是 `bl_ext.user_default.vmc_link`。2026-09-25 改了两处：

- `CROUCH_ROOT_ENABLED = False`。旧的下蹲补偿只在双腿明显缩短时把整个模型往下移，每帧跟上 35%。跳的腾空段里腿一收，它会把人往下拉。这段跳跃上，补偿和真实胯高的平均绝对误差约 7 cm，最坏超过 70 cm。
- 骨骼位移不再整具骨架打开。只处理 `Hips`：用第一帧胯的高度做发送端静止值，角色胯骨 `head_local.z` 除以它得到缩放，只把垂直变化写进胯的 `location`。其他骨头的 `location` 保持 0。

`启动沃雅妮莎.py` 里 `scene.vmc_link_apply_bone_translation` 仍是 `False`。胯的高度不走这个开关，走上面的硬编码分支。

整具骨架位移仍然不能开。VMC 的 `localPosition` 是发送端自己的骨长偏移。ARDY / AGI 的骨长和沃雅妮莎对不上，套上去会拉长骨头，走路还会把人滑出原地。

用户还没有回看「胯跟着上下」这一版。五次播放已经送到新窗口，但是否真的离地、会不会过高或陷进地里，需要看窗口确认。缩放若不对，先查 `head_local.z / 发送端第一帧胯高`，不要把所有骨头的位移打开。

## 怎么把一段生成播到窗口上

总线目录 `outputs\Mocap动作总线`。播放时从磁盘读片段，目录表是启动时载入的。正在跑的是打包后的 exe，改 Python 源不会进进程。

现成做法：把要播的片段临时盖住 `clips\ardy-walk-wave.json`，然后

```http
POST http://127.0.0.1:39538/api/action
{"actor":"vodyanitsa","id":"daily-walk-wave-ardy","mask":"full","loop":false}
```

请求返回后立刻把原文件拷回去。帧已经在总线内存里。不要留着覆盖，否则面板上的「走路挥手」会变成别的动作。

转换脚本：`outputs\Mocap动作总线\source\ardy_npz_to_clip.py`。用现有 `ardy-walk-wave.json` 对过 `walk_wave.npz`，位置和四元数误差约 1e-7。生成脚本在 `D:\ardy-mini\play_sequence.py`、`try_jump_prompts.py`、`compare_same_prompts.py`。

已生成、可直接再播的片段：

| 文件 | 内容 |
|---|---|
| `clips\ardy-live-walk-wave.json` | 向前走并挥右手 |
| `clips\ardy-live-dance.json` | 左右步进跳舞 |
| `clips\ardy-live-jump.json` | 短句，蹲一下，没有离地 |
| `clips\ardy-live-bow.json` | 鞠躬 |
| `clips\ardy-live-stretch.json` | 双臂上举 |
| `clips\ardy-live-turn.json` | 向左转再转回，脚几乎不转 |
| `clips\ardy-live-jump-high.json` | 写明离地的跳，数据里高出 36 cm |

对应 npz 在 `ardy-github-blender\outputs\motions\`，MiniLM 前缀 `ardy_minilm_`，8B 前缀 `ardy_8b_`。

人物站在原地。水平根位移没有放开。走路是原地迈步。

## 不要重做的事

- 不要把 ARDY 蒸馏或微调进本机千问。千问是 `D:\AI\Models\Qwen3.8-27B-UD-IQ3_XXS\Qwen3.8-27B-UD-IQ3_XXS.gguf`，约 10.9 GB，推理用 GGUF，不能直接 LoRA。
- 不要为了余弦再训这个 MiniLM-L6。
- 不要把动捕骨骼轨迹加进 MiniLM 的损失。它学的是文字条件，不是姿态。
- 不要走「简化图喂给 AGI」那条。已在 `outputs\AGI_AI姿势下限测试\实验状态.md` 停止。
- 标签动作的腿部修复试过并撤回了，交叉问题还在。不要在没看实流的情况下把那版改动找回来硬套。
- 不要给正在用的 AGI DLL 打内存补丁。长运行涨内存的调查在 `outputs\AGI_Mocap长运行卡顿调查.md`，只重启 AGI 能恢复。调试写盘 `agi_pose.bin` 是关的。
- Yosuri `127.0.0.1:18082` 这次返回 401，没有接上千问。

## 建议的下一步

用户还没确认新的胯高看起来对不对。先看 `ardy-live-jump-high.json` 再播一次。若陷地或飞起，只调胯的缩放，不要打开四肢位移。

确认之后再做这些，按这个顺序：

1. 给总线加一个不覆盖「走路挥手」的播放入口。现在每播一段都在偷换 `ardy-walk-wave.json`。exe 不读源码，新接口要么重打包总线，要么继续用这个临时盖文件的办法并保证还原。
2. 千问或本机 OpenAI 兼容接口写英文动作句，再交给 MiniLM。短句 `A person jumps.` 不会离地，要写成包含下蹲、双脚离地、落地的句子。
3. 转身若要迈步，得换提示或换动作来源。8B 和 MiniLM 对现在这句都是扭上身、脚钉地。
4. 声音、口型、自主选择动作都还没做。动作总线已有 `POST /api/action` 的标签片段，短动作仍应走标签，不必每句都生成。

## 端口

| 端口 | 用途 |
|---|---|
| 39538 | 动作总线 HTTP |
| 39539 | AGI 的 VMC 入口，总线占用。角色不直接听这个端口 |
| 39541 | Kimodo |
| 39542 | ARDY |
| 63084 | 当前沃雅妮莎的回传，重开窗口会变 |
