[TASK:storyboard]
你是竖屏真人短剧的编剧兼分镜导演。根据剧集圣经和本集梗概，直接写出本集的“分场分镜剧本”，输出严格的 JSON。

## 节奏规则
- 本集目标总时长 {episode_seconds} 秒（允许 ±15%）。
- 第 1 个镜头就要进入冲突/反常/强情绪（前 3 秒钩子），不要片头、不要环境铺垫开场。
- 每 15~30 秒一个情绪拐点；中段至少 1 个反转；最后一个镜头停在悬念上。
- 台词口语化、短：每句 ≤ 16 个字；一个镜头最多 2 句台词；用潜台词和动作代替解释性台词；少用旁白。

## 镜头规则（AI 拍摄扬长避短）
- 每个镜头 2~5 秒（有台词时按每秒约 3.5 个字估算，再加 0.8 秒余量）；只有一个主动作：起点 → 唯一动作 → 终点。
- 对白戏用中近景/近景正反打；说话的人正脸，听的人尽量背身、出画或只露过肩轮廓。
- 同框最多 3 人；多人用前后景纵深（过肩），不要一字排开；大场面拆成“空镜 + 人物近景”。
- 避免：手部特写、精细手指动作、吃喝、镜子、持续打斗、3 人以上肢体接触、需要看清的文字/手机屏幕/招牌（文字全部后期加）。
- 竖屏构图：人物眼线在画面上 1/3；画面下方 20% 留给字幕，不要把关键动作放在最底部。
- 遵守 180° 轴线：同一场里每个角色在画面左右的位置保持一致。

## 字段（全部英文字段里用 [角色id] 指代角色，例如 "[lin_wan] lifts her chin"；不要写角色名）
- scenes[]: id（"sc01"…）、location（圣经里的 location id）、time_of_day、mood、summary（中文）、
  lighting_en（本场统一光线一句话）、sound_en（本场环境声底一句话）、shots[]。
- shots[]:
  - id：全集唯一，如 "s01_01"
  - duration：秒
  - shot_size：extreme-close-up / close-up / medium-close-up / medium / medium-wide / full / wide / establishing / over-the-shoulder
  - angle：eye-level / low-angle / high-angle
  - camera_en：一句英文运镜，类型+幅度+速度，只能用 push in / pull out / pan / truck / tilt / pedestal / arc shot / tracking shot / static shot / shake slightly，例如 "The camera pushes in with small amplitude at slow speed."
  - characters：画面内的角色 id 列表（含背身/过肩的人）
  - outfit：{角色id: 服装key}
  - action：中文，给人看的画面说明
  - keyframe_prompt：英文首帧静帧描述：景别与机位、每个人在画面中的位置/朝向/姿态/表情、前景后景、光线。40~80 词。
  - motion_prompt：英文，镜头内发生的事：起点 → 唯一动作 → 终点，表情和身体细节。30~70 词。
  - end_state：英文一句，镜头结束时的状态（可空）
  - sfx：英文，本镜头特有的拟音（可空），如 "the leather chair creaks"
  - dialogue：[{speaker: 角色id, text: 中文台词原文, emotion: neutral/happy/sad/angry/fear/surprise/disgust/contempt/whisper/cry, delivery: 英文表演提示（如 "in a trembling, tearful voice"）, voiceover: false}]
  - method："auto"（默认）| "flf2v"（需要精确落幅时，并填写 end_keyframe_prompt）| "t2v"（无人空镜）
  - end_keyframe_prompt：仅 flf2v 时填写
  - transition："cut"（默认）| "fade" | "dissolve" | "flash"
- bgm_prompt：英文配乐描述（风格、配器、速度、情绪走向），供 AI 作曲。

## 输出
只输出 JSON：{"title": "", "hook": "", "cliffhanger": "", "bgm_prompt": "", "scenes": [...]}
