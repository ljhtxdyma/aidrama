# 03 工作流 SOP：从一句话到成片

本文按实际制作顺序，写清每一步要做什么：用什么命令、产出什么、**哪里必须人工把关**、不满意怎么返工。

命令里的 `ad` 指命令入口：Windows 是 `.\aidrama.bat`，Linux 是 `./aidrama.sh`；也可以写成 `python -m aidrama`。
示例中的工程目录是 `projects/myshow`，集号是 `ep01`。

> 原则：**能在前面改的，不要留到后面改。** 剧本、设定图、关键帧这些前期环节，改一次只要几十秒；视频阶段每抽一条要十分钟左右。
> 所以最重要的三个人工检查点是：**分镜 → 设定图/音色 → 关键帧**。

---

## 0. 每次开工前

```bash
bash install/linux/start_all.sh            # Windows：install\windows\start_all.ps1
ad doctor                                  # 全部打勾再开始
```

## 1. 立项：剧集圣经（LLM）

```bash
ad new projects/myshow --idea "外卖员意外继承百亿遗产，却发现遗嘱里藏着一桩命案" --episodes 10 --seconds 90
```

产出 `projects/myshow/project.yaml`，内容包括剧名、一句话梗概、风格、角色、场景和分集梗概。

**人工检查（20 分钟，值得花）：**

- **角色外貌（`appearance_en`）**：要具体、有辨识度，比如脸型、眼睛、标志性痣、发型。主要角色之间要明显不同。
  不要写成明星脸，平台会做跨剧人脸相似度比对，“AI 模板脸”会被驳回。
- **服装（`outfits`）**：每套服装一个 key（如 `default`、`wedding`），写清颜色、材质和配饰。同一套服装在全剧必须用同一段文字。
- **音色描述（`voice.description`）**：用中文写年龄、性别、音高、语速、质感，例如“三十岁男性，声音低沉略带沙哑，语速平稳”。
- **场景（`locations`）**：用英文描述布局、陈设和光线；重要场景会生成一张空景图，作为后续所有镜头的场景参考。

改完运行 `ad check projects/myshow`，检查角色 id、服装 key、场景引用是否一致。

## 2. 分镜（LLM）

```bash
ad storyboard projects/myshow ep01 --notes "第一集结尾要留钩子：律师说出遗嘱第七条"
```

LLM 按要求生成分场分镜，写回 `project.yaml` 的 `episodes[].scenes[].shots[]`。字段的写法见 [04-提示词指南](04-提示词指南.md)。

**人工检查：**

- 每个镜头只做**一件事**；有对白的镜头里，**只有说话的人张嘴**，听的人列在 `characters` 里即可。
- 单句台词在 20 个字以内。长台词要拆成多句，或拆到多个镜头里。
- 多音字可以直接在台词里标读音，例如 `银<行|HANG2>`。标注只影响配音，字幕和画面提示词里会自动去掉。
- 景别要有变化（远—中—近—特写），避免全集都是正面中景。
- 特殊镜头在 `method` 里指定：

| method | 用途 | 生成方式 |
|---|---|---|
| `auto`（默认） | 绝大多数镜头 | 同场连续镜头合成一个 H3 Ref2VA 段；单镜头无对白用 H3 FL2VA |
| `flf2v` | 需要精确落幅的镜头（转身、走到某处停下） | 首帧 + 尾帧两张关键帧，H3 FL2VA |
| `t2v` | 空镜、氛围镜头 | 不出关键帧，纯文生 |
| `continue` | 长镜头接续 | 用上一段所选视频的最后一帧当首帧 |
| `animate` | 打戏、复杂走位、需要真人表演 | Wan Animate 2，用你拍的驱动视频（见第 11 节） |

- 转场写在 `transition`（`cut`/`fade`/`dissolve`/`flash`）。转场不是 `cut` 的地方，后面的镜头会另起一个生成段。

## 3. 定妆：设定图 + 场景图 + 角色音色

```bash
ad cast projects/myshow
```

| 产物 | 位置 | 模型 |
|---|---|---|
| 定妆照（正脸半身） | `assets/characters/<id>/front.png` | Qwen-Image-2512 |
| 每套服装一张三视图设定图（正/侧/背） | `assets/characters/<id>/sheet_<服装>.png` | Qwen-Image-Edit-2511（以定妆照为参考） |
| 场景空景图 | `assets/locations/<id>/plate.png` | Qwen-Image-2512 |
| 角色音色样本 | `assets/characters/<id>/voice.wav` | Qwen3-TTS VoiceDesign |

**人工检查（最关键的一步：这里定下的脸和声音会用于全剧）：**

- 脸是否符合设定、有辨识度、不像某个明星；三视图里的脸、发型、服装是否一致。
- 声音是否符合人设、听起来是否自然。

**返工：**

- 只重做某一个角色：把 `project.yaml` 里该角色的 `refs:` 清空（或删掉对应文件路径），再运行 `ad cast`。已有的图会跳过。
- 全部重做：`ad cast projects/myshow --force`（你自己提供的录音不会被覆盖）。
- 想要另一张脸：修改 `appearance_en`（种子由角色 id 决定，描述变了，脸就会变），或者直接用自己的图替换 `front.png`，再删掉 `sheet_*` 引用，让它重新生成三视图。
- 声音不满意：修改 `voice.description` 或 `voice.design_text`（试音文本），清空 `voice.ref_audio` 后重跑。
  也可以把**获得授权**的真人录音（10–15 秒，干净无底噪）放进去，作为 `ref_audio`。
- 想让某个情绪更到位：在 `voice.emotion_refs` 里给情绪配一段参考音频，例如 `angry: assets/characters/gu_chen/angry.wav`。IndexTTS 会模仿它的情绪。

## 4. 配音

```bash
ad voice projects/myshow ep01
```

IndexTTS-2.5 用角色音色逐句配音：

- 情绪由台词的 `emotion` 决定；需要精细控制时，可以用 `emo_vector` 写 8 维向量。
- 配好的音频会去掉首尾静音，并统一到 -18 LUFS。
- 产物在 `episodes/ep01/audio/<镜头>_<8位哈希>.wav`（哈希由台词、情绪和音色算出），真实时长回写到 `project.yaml`。

**人工检查：** 逐句听一遍，重点听多音字、语气、语速。

**返工：**

- 改了台词、读音标注、情绪后直接重跑 `ad voice`：配音文件名里带着这些内容的哈希，改过的句子会自动重配，其他句子不受影响（插入、删除台词也不会覆盖别的句子）。`--force` 会重配全部。
- 想用真人录音：把该句的 `audio:` 指向你的音频文件即可，流水线不会覆盖它（`--force` 除外）。
- 语气不对：换一个 `emotion`，或写 `emo_vector`，或配一段情绪参考音频。

## 5. 规划生成段

```bash
ad plan projects/myshow ep01
```

编排器按配音的真实时长重排每个镜头的时间轴：

- 台词前留 0.35 秒，句间留 0.25 秒，句末留 0.45 秒。
- 同一场里连续的镜头会合成一个 **H3 生成段**：每段不超过 12 秒、4 个镜头、3 个角色，同一次生成里完成切镜，人物和光线更连贯。

输出示例：

```
[plan] ep01_g01 h3_ref2va  3 镜 10.70s（生成 10.70s）: s01_01, s01_02, s01_03
[plan] ep01_g03 h3_fl2va   1 镜  3.00s（生成 4.00s）: s02_01
```

想调整分段，可以改镜头时长或转场，或者把某个镜头的 `method` 改成 `flf2v`/`t2v`，让它单独成段。

- 某个镜头的台词按真实配音排下来超过 15 秒（H3 单次上限），`plan` 会直接报错并指出镜头，请把台词拆到两个镜头里。
- 重新规划时，内容没变的段会保留已经抽好的视频，即使段号因为前面的改动变了（按镜头内容签名匹配，人工提示词 `.manual.txt` 也会跟着改名）；内容变了的段（改了提示词、台词、关键帧）会重新生成。
可调参数在 `video.max_segment_seconds`、`video.max_cuts_per_segment`。

## 6. 关键帧

```bash
ad keyframes projects/myshow ep01
```

每个镜头生成一张首帧（`flf2v` 镜头另有尾帧）：

- 产物在 `episodes/ep01/keyframes/<镜头>.png`。
- 模型是 Qwen-Image-Edit-2511 多参考编辑，最多 3 张参考图：先放出场角色的三视图设定图，剩下的名额给场景空景图。提示词里用 “the woman from Picture 1” 指代角色。
- 关键帧之后会成为 H3 每个切点的锚点：既作为参考图，也会被 AddGuide 钉在切点帧上。

**人工检查（第二重要的检查点）：** 逐张看构图、站位、脸、服装、道具和手。**关键帧不对，视频一定不对。**

**返工：**

- 重做单张：`ad keyframes projects/myshow ep01 --only s01_03 --force`。
- 改画面：修改该镜头的 `keyframe_prompt`；也可以在镜头上写 `seed: 123`，换一个随机种子再重做。
- 自己修图：可以直接用 PS 修改关键帧 PNG（文件名不变），后续步骤会使用修过的图。

## 7. 视频：抽卡 + 自动质检

```bash
ad video projects/myshow ep01                    # 默认 quality 预设，每段 2 条
ad video projects/myshow ep01 --preset balanced  # 先快速过一遍
```

视频阶段分三遍执行，同一时刻只有一个模型占用显存：

1. **编译提示词**。按 H3 官方格式生成提示词：
   - 台词逐字锁定；
   - 切镜时间精确到毫秒；
   - 听的人强制闭嘴；
   - 校验通过后，再由本地 LLM 扩写画面细节；如果扩写破坏了规则，自动退回确定性版本。
   - 产物在 `episodes/ep01/prompts/<段>.txt`。
2. **ComfyUI 抽卡**：
   - Ref2VA 段的参考包括：每个切点的关键帧、角色设定图，以及本段对白音轨（`<Audio 1>`，嘴型跟着它动）。
   - 每张关键帧都用 AddGuide 钉在对应切点帧上。
   - 每条生成完立即检查黑场、冻帧和时长。
3. **对白回读质检**：用 Qwen3-ASR 识别每条视频里的对白，和剧本比对字错率（默认 >15% 判不合格）。之后自动选出最好的一条。

产物在 `episodes/ep01/segments/<段>_<签名>_t<n>.mp4`；实际提交给 ComfyUI 的工作流保存在 `graphs/`，可以直接拖进 ComfyUI 复现。

- 中途出错或按了 Ctrl+C：已经生成的条会先完成对白质检和选条；直接重跑同一条命令即可从断点继续。
- `--force` 重抽时，用 `add-take` 登记的外部视频会保留。
- `continue` 续写段：上一段被接上的那一条会自动锁定（之后补抽不会自动改选它）。想换上一段的条，先 `pick`，再对续写段 `--only <段> --force` 重抽。

## 8. 审片与选条

```bash
ad review projects/myshow ep01       # 生成 episodes/ep01/review.html（run 结束时也会自动生成）
```

用浏览器打开 `review.html`，每段的所有条目并排播放，旁边显示质检问题、字错率、关键帧、台词和提示词。
每条下面都有现成的 pick 命令，可以直接复制：

```bash
ad pick projects/myshow ep01 ep01_g02 3        # 人工选定第 3 条（之后补抽也不会被自动改选）
```

人工审片时，除了自动质检的项目，还要看这些（平台硬伤清单见 [05-质检与返工](05-质检与返工.md)）：

- 脸有没有漂移或崩坏；
- 手和肢体是否正常；
- 物体有没有穿模；
- 听的人有没有在动嘴；
- 表演是否到位；
- 切镜点是否落在台词之间。

**返工的选择（从便宜到贵）：**

| 问题 | 做法 |
|---|---|
| 几条都差一点 | 补抽：`ad video projects/myshow ep01 --only ep01_g02 --takes 4`（在已有 2 条基础上再抽 2 条） |
| 动作、走位总不对 | 修改该段对应镜头的 `motion_prompt`、`camera_en`，然后 `--only ep01_g02 --force` 重抽 |
| 提示词需要手调 | 复制 `prompts/ep01_g02.txt` 为 `prompts/ep01_g02.manual.txt` 并修改，再 `--force` 重抽。流水线会优先使用 manual 文件，只提示格式问题、不拦截 |
| 构图或人物本身不对 | 回到第 6 步重做关键帧，再重抽 |
| 一段里镜头太多、互相干扰 | 把部分镜头改成单独成段（改 `transition` 或 `method`），重新 `plan` 再抽 |
| 长独白口型反复不过 | 走第 11 节的 InfiniteTalk 兜底 |

## 9. 超分

```bash
ad upscale projects/myshow ep01
```

用 SeedVR2 7B 把所选的每条视频从 768×1344 超分到 1080×1920，带 lab 色彩校正，产物在 `episodes/ep01/final/<段>.mp4`。
重新 pick 过的段会自动重做超分。

**人工检查：** 脸部有没有过度锐化或“塑料感”。如果有，在 `aidrama.yaml` 里改用 `upscale.model: 3b`（更保守），或设置 `upscale.enabled: false`，改为只做普通缩放。

## 10. 配乐与合成

```bash
ad music projects/myshow ep01        # MiniMax Music 3 按本集 bgm_prompt 生成纯音乐
ad assemble projects/myshow ep01
```

- 配乐：修改本集的 `bgm_prompt` 后用 `--force` 重做；也可以把 `ep.bgm` 指向自己有版权的音乐文件。
- 合成做的事：
  - 各段拼接，`fade`/`dissolve`/`flash` 转场；
  - 对白时 BGM 自动压低（sidechain ducking）；
  - 两遍响度归一，到 -14 LUFS / -1 dBTP；
  - 烧录字幕（思源黑体，底部留 520 像素给平台按钮和文案）；
  - 右上角显式 AI 标识；
  - 写入 GB 45438-2025 的 AIGC 元数据。
- 产物在 `episodes/ep01/out/`，包括：
  - `ep01.mp4`：成片；
  - `ep01_clean.mp4`：不烧字幕的版本，方便平台外挂字幕或二次剪辑；
  - `ep01.srt` / `ep01.ass`：字幕文件；
  - `ep01_timeline.json`：时间线。

**人工检查：** 完整看一遍成片，重点看字幕和口型是否同步、转场是否生硬、BGM 是否盖住台词。
想在剪映里精修，可以用 `ep01_clean.mp4` 加 `ep01.srt`。

## 11. 兜底与增强

### InfiniteTalk：长独白、口型兜底

H3 一段最长 15 秒。超过 15 秒的独白，或者反复抽都对不上口型的段，可以换用 InfiniteTalk：

- 输入关键帧和配音，生成口型精确、时长不限的说话视频；
- 原生 480p，之后由 SeedVR2 超分；
- 表演自然度比 H3 稍弱。

1. 下载模型组：`python scripts/download_models.py --comfy <ComfyUI> --groups lipsync`。
2. 在 ComfyUI 中打开 `workflows/11_infinitetalk_fallback_lipsync.json`，把 LoadImage 换成该镜头的关键帧，LoadAudio 换成该段对白音轨 `episodes/ep01/audio/<段>_dialogue.wav`，然后运行。
3. 登记回流水线：`ad add-take projects/myshow ep01 ep01_g05 ComfyUI/output/aidrama/talk_00001_.mp4`。

### Wan Animate 2：打戏、复杂走位、需要真人表演

1. 镜头设 `method: animate`。规划时它会单独成段，`video` 阶段会跳过它。
2. 用手机竖拍真人表演（侧光、背景干净、全身入画），时长和镜头一致。
3. 下载模型组 `animate`。在 ComfyUI 中打开 `workflows/12_wan_animate2_performance_transfer.json`：
   - `reference_image` 用角色设定图或关键帧；
   - 驱动视频用你拍的素材；
   - 提示词写 “Character Description: … Background description: …”。
4. 跑完后登记：`ad add-take projects/myshow ep01 ep01_g07 <输出视频>`。
5. 这类镜头生成的视频没有对白声音。如果镜头有台词，`ad video` 会为它准备好对白音轨，路径会打印出来，一般是 `episodes/ep01/audio/<段>_dialogue.wav`。
   先把音轨混进视频，再执行 add-take：
   `ffmpeg -i animate.mp4 -i episodes/ep01/audio/ep01_g07_dialogue.wav -map 0:v -map 1:a -c:v copy -shortest animate_dub.mp4`

### 付费增强（可选）

本地方案已经能做到开源第一档。少数关键镜头（大场面、高难度动作、需要 2K 原生画质）可以付费处理：

- 用 ComfyUI 合作节点中的 MiniMax Hailuo 03 Context-IR / Regenerate-2K；
- 或者用 Seedance 等商用 API 重做。

做完后同样用 `ad add-take` 登记，后续的选条、超分和合成流程不变。

## 12. 发布前检查

按 [07-合规与授权](07-合规与授权.md) 逐项确认：

- 是否已备案；
- 每集是否有显式 AI 标识，以及 AIGC 元数据；
- 角色是否原创，有没有撞脸明星；
- 声音授权；
- 模型许可证是否允许你的商用规模。

## 附：一集的目录结构

```
projects/myshow/
  project.yaml                  全部设定、分镜、产物路径、选条记录（核心文件，建议用 git 管理）
  aidrama.yaml                  本工程的参数覆盖（可选）
  assets/characters/<id>/       front.png  sheet_<服装>.png  voice.wav
  assets/locations/<id>/        plate.png
  graphs/                       每次实际提交给 ComfyUI 的工作流（可复现、可留档）
  episodes/ep01/
    audio/                      逐句配音、每段对白音轨、bgm
    keyframes/                  每个镜头的首帧（尾帧）
    prompts/                    每段的 H3 提示词（.txt 实际使用，.draft.txt 确定性版本，.manual.txt 人工覆盖）
    segments/                   所有抽卡结果
    qc/                         质检用的音频切片
    final/                      超分后的所选条目
    out/                        成片、无字幕版、字幕、时间线
    review.html                 审片页
```

## 附：典型日程（一集 90 秒，quality 预设，估算）

| 时间 | 人 | 机器 |
|---|---|---|
| 上午 | 写/改分镜（1–2 小时），检查设定图、音色、配音、关键帧（1 小时） | cast / voice / keyframes（约 1 小时） |
| 下班前 | 启动 `ad video` | 夜里抽卡（约 3–5 小时） |
| 次日上午 | 审片、pick、补抽少数段 | 补抽 + upscale + music + assemble（约 1–2 小时） |
| 次日中午 | 看成片、走发布检查 | |

成熟后可以流水作业：机器夜里跑第 N 集视频的同时，人白天准备第 N+1 集的分镜和关键帧。
