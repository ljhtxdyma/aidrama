"""Mock 后端：不需要 GPU / 模型 / 网络，用占位素材把整条流水线跑通（云端 CI 与本地联调用）。

同时内置一部示例短剧《第七封信》的剧集圣经和第 1 集分镜，examples/demo 就是用它生成的。
"""
from __future__ import annotations

import json
import math
import re
import struct
import wave
from pathlib import Path

from . import ffmpeg_utils as ff

DEMO_BIBLE = {
    "series": {
        "title": "第七封信",
        "logline": "档案修复师林晚在一批无人认领的旧信里，发现了一封写给自己、却迟到三年的信；送信的律师顾沉，似乎知道她失去的那段记忆。",
        "genre": "都市悬疑 / 情感",
        "style_en": "Live-action, cinematic",
        "image_style_en": "photorealistic live-action film still, natural skin texture, cinematic lighting, shallow depth of field",
    },
    "characters": [
        {
            "id": "lin_wan", "name": "林晚", "gender": "女", "age": "25",
            "appearance": "瓜子脸，眼尾微挑的杏眼，左眼下一颗小泪痣，低马尾黑发，皮肤白皙，身形纤瘦",
            "identity_en": "a slender woman in her mid-20s with a low black ponytail",
            "appearance_en": "oval face, almond-shaped eyes with slightly lifted outer corners, a small beauty mark under the left eye, "
                             "straight dark eyebrows, fair skin with natural texture, thin lips, sleek black hair tied in a low ponytail, "
                             "slender build, East Asian woman in her mid-20s",
            "outfits": {"default": "an ivory silk blouse with a thin black ribbon tie, small pearl earrings"},
            "personality": "冷静、克制、心思细，越紧张越安静",
            "voice": {
                "description": "二十五岁女性，声音清亮偏冷，中低音，语速偏慢，咬字清楚，真人自然不播音腔",
                "voice_en": "a clear, cool, mid-pitched young female voice",
                "design_text": "这批信在库房里放了整整三年，没有人来认领。可这一封，信封上写的是我的名字，还有我从没去过的地址。",
            },
        },
        {
            "id": "gu_chen", "name": "顾沉", "gender": "男", "age": "30",
            "appearance": "棱角分明的方脸，剑眉，单眼皮深邃眼神，短发侧分，下颌线清晰，高瘦",
            "identity_en": "a tall man in his early 30s with short side-parted black hair",
            "appearance_en": "angular face with a defined jawline, thick straight eyebrows, deep-set monolid eyes, short side-parted black hair, "
                             "light stubble, tall lean build, East Asian man in his early 30s",
            "outfits": {"default": "a charcoal three-piece suit, white shirt without a tie, a silver wristwatch"},
            "personality": "沉稳，话少，习惯把真相藏在规则后面",
            "voice": {
                "description": "三十岁男性，声音低沉略带沙哑，语速平稳，气息稳，真人自然",
                "voice_en": "a low, calm, slightly husky male voice",
                "design_text": "有些信寄出去的那一刻，就注定送不到。我只是在等一个合适的时间，把它交到你手上。",
            },
        },
    ],
    "locations": [
        {"id": "archive_room", "name": "档案修复室（夜）", "description": "老式档案库，铁架、旧纸箱、工作台上一盏绿色台灯",
         "description_en": "a dim archive restoration room at night: tall grey metal shelves packed with old cardboard boxes and bundled letters, "
                           "a wooden worktable with a green banker's lamp, scattered yellowed envelopes, dust floating in the lamp light, "
                           "cool blue moonlight from a high window"},
        {"id": "law_office", "name": "顾沉的律所办公室（夜）", "description": "高层办公室，落地窗外城市夜景，胡桃木办公桌，黄铜台灯",
         "description_en": "a high-rise law office at night: a floor-to-ceiling window with blurred city lights, a dark walnut desk, "
                           "a warm brass desk lamp, a black leather chair, bookshelves of legal files, low-key warm practical light"},
    ],
    "episodes": [
        {"id": "ep01", "title": "迟到三年的信",
         "synopsis": "深夜，林晚在库房旧信里发现一封写给自己的信。陌生律师顾沉突然出现，要求她交出信件。林晚追问信为何迟到三年，顾沉沉默。镜头转到顾沉的办公室，抽屉里还有六封一模一样的信——寄信人正是他自己。",
         "hook": "林晚在旧信里看到自己的名字",
         "cliffhanger": "林晚出现在顾沉办公室门口：原来寄信的人是你"},
    ],
}

DEMO_STORYBOARD = {
    "title": "迟到三年的信",
    "hook": "林晚在旧信里看到自己的名字",
    "cliffhanger": "原来寄信的人是你",
    "bgm_prompt": "Minimal suspense score: sparse felt piano, low sustained cello drone, soft ticking clock texture, 70 BPM, "
                  "slowly building tension, a brief swell on the final reveal.",
    "scenes": [
        {
            "id": "sc01", "location": "archive_room", "time_of_day": "night", "mood": "悬疑",
            "summary": "林晚发现写给自己的信，顾沉闯入",
            "lighting_en": "low-key light from a green banker's lamp with cool blue moonlight from a high window",
            "sound_en": "Quiet archive room tone with a faint ticking clock and the soft hum of an old lamp.",
            "shots": [
                {"id": "s01_01", "duration": 3.5, "shot_size": "close-up", "camera_en": "The camera pushes in with small amplitude at slow speed toward her face.",
                 "characters": ["lin_wan"], "outfit": {"lin_wan": "default"},
                 "action": "林晚在台灯下翻到一封旧信，看清信封上的名字，愣住",
                 "keyframe_prompt": "Close-up at eye level of [lin_wan] seated at a wooden worktable, holding a yellowed envelope at chest height, "
                                    "her face lit warmly from the green banker's lamp on the left, cool moonlight rimming her hair, "
                                    "shelves of old boxes softly blurred behind her, focused neutral expression.",
                 "motion_prompt": "[lin_wan] reads the address on the envelope; her eyes widen slightly, her breath catches, and her fingers tighten on the paper.",
                 "dialogue": [{"speaker": "lin_wan", "text": "这封信，写的是我的名字。", "emotion": "surprise", "delivery": "in a low, stunned whisper"}]},
                {"id": "s01_02", "duration": 3.0, "shot_size": "medium-wide", "camera_en": "The camera holds a static shot.",
                 "characters": ["lin_wan", "gu_chen"], "outfit": {"lin_wan": "default", "gu_chen": "default"},
                 "action": "库房门被推开，顾沉逆光站在门口，林晚回头",
                 "keyframe_prompt": "Medium-wide shot from behind the worktable: [lin_wan] in the foreground on frame left, seen from behind over her shoulder, "
                                    "turning toward the doorway; [gu_chen] stands in the open doorway on frame right, backlit by a cold corridor light, "
                                    "his face half in shadow, rows of metal shelves between them.",
                 "motion_prompt": "The door swings open and [gu_chen] steps one pace into the room and stops; [lin_wan] turns her head toward him, holding the envelope close.",
                 "sfx": "The old door hinge creaks and footsteps stop on the concrete floor.", "dialogue": []},
                {"id": "s01_03", "duration": 3.0, "shot_size": "medium-close-up", "camera_en": "The camera pushes in with small amplitude at slow speed.",
                 "characters": ["gu_chen"], "outfit": {"gu_chen": "default"},
                 "action": "顾沉看着她手里的信，语气平静",
                 "keyframe_prompt": "Medium close-up of [gu_chen] standing between metal shelves, facing camera slightly off-axis to frame left, "
                                    "cool corridor light on one side of his face and warm lamp light on the other, calm unreadable expression.",
                 "motion_prompt": "[gu_chen] lowers his gaze to the envelope in her hands, then raises his eyes to her face without moving his body.",
                 "dialogue": [{"speaker": "gu_chen", "text": "林小姐，这封信你不该打开。", "emotion": "neutral", "delivery": "quietly and evenly"}]},
                {"id": "s01_04", "duration": 3.0, "shot_size": "close-up", "camera_en": "The camera holds a static shot.",
                 "characters": ["lin_wan"], "outfit": {"lin_wan": "default"},
                 "action": "林晚抬起下巴，直视他",
                 "keyframe_prompt": "Close-up of [lin_wan] facing slightly toward frame right, chin lifted, eyes fixed on someone off-screen, "
                                    "green lamp light on her cheek, envelope edge visible at the bottom of the frame.",
                 "motion_prompt": "[lin_wan] lifts her chin and holds his gaze, her jaw tightening as she speaks.",
                 "dialogue": [{"speaker": "lin_wan", "text": "三年前寄出的信，为什么今天才到？", "emotion": "angry", "delivery": "coldly, with restrained anger"}]},
                {"id": "s01_05", "duration": 2.5, "shot_size": "close-up", "camera_en": "The camera holds a static shot.",
                 "characters": ["gu_chen"], "outfit": {"gu_chen": "default"},
                 "action": "顾沉沉默，下颌绷紧",
                 "keyframe_prompt": "Close-up of [gu_chen] facing slightly toward frame left, half of his face in shadow, lips pressed together.",
                 "motion_prompt": "[gu_chen] does not answer; his jaw tightens and he swallows once, eyes drifting away for a moment.",
                 "dialogue": []},
            ],
        },
        {
            "id": "sc02", "location": "law_office", "time_of_day": "night", "mood": "反转",
            "summary": "顾沉的办公室：还有六封信",
            "lighting_en": "warm brass desk lamp as the key light with cool blue city light through the window",
            "sound_en": "Low air-conditioning hum and muffled city traffic behind the glass.",
            "shots": [
                {"id": "s02_01", "duration": 3.0, "shot_size": "establishing", "camera_en": "The camera pushes in with small amplitude at slow speed toward the window.",
                 "characters": [], "action": "空镜：夜色中的律所，落地窗外城市灯火", "method": "t2v",
                 "keyframe_prompt": "Empty high-rise law office at night seen from the doorway, floor-to-ceiling window with blurred city lights, "
                                    "dark walnut desk with a warm brass lamp switched on, black leather chair, no people.",
                 "motion_prompt": "City lights twinkle softly beyond the glass while the desk lamp glows steadily in the empty office.",
                 "dialogue": []},
                {"id": "s02_02", "duration": 4.0, "shot_size": "medium", "camera_en": "The camera holds a static shot.",
                 "characters": ["gu_chen"], "outfit": {"gu_chen": "default"},
                 "action": "顾沉坐在桌后，桌面上整齐摆着六封一模一样的旧信",
                 "keyframe_prompt": "Medium shot of [gu_chen] seated behind the dark walnut desk, six identical yellowed envelopes laid in a neat row "
                                    "on the desk in front of him, warm brass lamp on frame left, city lights behind him, his face tired and tense.",
                 "motion_prompt": "[gu_chen] looks down at the row of envelopes and slowly exhales, his shoulders sinking slightly.",
                 "dialogue": [{"speaker": "gu_chen", "text": "第七封，终于送到她手里了。", "emotion": "sad", "delivery": "softly, almost to himself"}]},
                {"id": "s02_03", "duration": 3.5, "shot_size": "medium-close-up", "camera_en": "The camera pushes in with small amplitude at slow speed.",
                 "characters": ["lin_wan"], "outfit": {"lin_wan": "default"},
                 "action": "林晚出现在办公室门口，手里拿着那封信",
                 "keyframe_prompt": "Medium close-up of [lin_wan] standing in the office doorway, the yellowed envelope held at her side, "
                                    "cool corridor light behind her and warm lamp light on her face, eyes locked on someone off-screen.",
                 "motion_prompt": "[lin_wan] steps into the doorway and stops, her expression shifting from shock to cold certainty.",
                 "dialogue": [{"speaker": "lin_wan", "text": "原来，寄信的人是你。", "emotion": "contempt", "delivery": "slowly, with cold certainty"}],
                 "transition": "fade"},
            ],
        },
    ],
}


def mock_llm(messages: list[dict]) -> str:
    system = messages[0]["content"] if messages else ""
    user = messages[-1]["content"] if messages else ""
    if "[TASK:bible]" in system:
        return json.dumps(DEMO_BIBLE, ensure_ascii=False)
    if "[TASK:storyboard]" in system:
        return json.dumps(DEMO_STORYBOARD, ensure_ascii=False)
    m = re.search(r"<<<\n(.*?)\n>>>", user, re.S)   # H3 refine：原样返回确定性草稿
    if m:
        return m.group(1)
    return "{}"


# ---------------------------------------------------------------------------
# 占位媒体
# ---------------------------------------------------------------------------

def tone_wav(path: str | Path, seconds: float, freq: float = 220.0, sr: int = 24000) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * sr)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / sr))) for i in range(n)))
    return path


def placeholder_image(path: str | Path, width: int, height: int, label: str, color: str = "0x334455") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    txt = re.sub(r"[^A-Za-z0-9_\-. ]", "", label)[:40] or "img"
    ff.run(["-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:d=1", "-frames:v", "1",
            "-vf", f"drawtext=text='{txt}':fontcolor=white:fontsize={max(24, width // 18)}:x=(w-tw)/2:y=(h-th)/2",
            str(path)])
    return path


def placeholder_video(path: str | Path, width: int, height: int, seconds: float, label: str,
                      audio: str | Path | None = None, first_frame: str | Path | None = None) -> Path:
    """有首帧就用首帧做缓慢推近，没有就用测试图；可附带音轨。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    txt = re.sub(r"[^A-Za-z0-9_\-. ]", "", label)[:40] or "clip"
    frames = max(1, int(round(seconds * 24)))
    if first_frame:
        args = ["-loop", "1", "-i", str(first_frame)]
        vf = (f"scale={width * 2}:{height * 2},zoompan=z='min(zoom+0.0008,1.12)':d={frames}:s={width}x{height}:fps=24,"
              f"drawtext=text='{txt}':fontcolor=white:fontsize={max(20, width // 20)}:x=20:y=20")
    else:
        args = ["-f", "lavfi", "-i", f"testsrc2=s={width}x{height}:r=24"]
        vf = f"drawtext=text='{txt}':fontcolor=white:fontsize={max(20, width // 20)}:x=20:y=20"
    if audio:
        args += ["-i", str(audio)]
    else:
        args += ["-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.02:r=48000"]
    ff.run(args + ["-vf", vf, "-af", "apad", "-frames:v", str(frames), "-t", f"{seconds:.3f}", "-map", "0:v", "-map", "1:a",
                   "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", str(path)])
    return path
