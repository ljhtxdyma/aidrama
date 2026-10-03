"""导出各阶段的 ComfyUI API 工作流示例（可直接拖进 ComfyUI 打开、改参数、手动跑）。"""
from __future__ import annotations

import json
from pathlib import Path

from .graphs.h3 import Guide, H3Job, build_h3
from .graphs.post import InterpJob, MusicJob, UpscaleJob, build_interp, build_music, build_seedvr2
from .graphs.qwen_edit import QwenEditJob, QwenT2IJob, build_qwen2511_edit, build_qwen2512_t2i
from .graphs.qwen_image import QwenImageJob, build_qwen_image
from .graphs.talk import InfiniteTalkJob, build_infinitetalk
from .graphs.animate import AnimateJob, build_wan_animate2

DIALOGUE_PROMPT = """subject_definitions:
<Subject 1> (S1) is the person in <Picture 2>, a slender woman in her mid-20s with a low black ponytail, wearing an ivory silk blouse with a thin black ribbon tie.
<Picture 1> is the first frame of [Shot 1].
<Audio 1> is the pre-recorded dialogue track of the target video, containing the spoken lines of <Subject 1> (S1).

summary:
[keyframe completion + reference generation + audio reuse] The target video is a 1-shot scene with <Subject 1>. It begins exactly from <Picture 1>. <Audio 1> supplies the exact dialogue, and the speakers' lips are synchronized to it.

retention_analysis:
<Subject 1> (appears in the shots where visible): fully_preserved - facial identity, hairstyle, skin tone, body shape and costume are retained exactly; pose and expression follow each shot.
<Picture 1> ([Shot 1] first frame): fully_preserved - framing, subject positions, costumes and lighting are matched exactly at that moment.
<Audio 1>: partially_copy - every spoken word of <Audio 1> is reused verbatim and in sync as the dialogue, while room tone and physical sounds are added around it.

detailed_description:
The target video is in a live-action, cinematic style, low-key light from a green banker's lamp with cool blue moonlight from a high window.
[Shot 1] The shot begins from <Picture 1>. <Subject 1> reads the address on the envelope; her eyes widen slightly, her breath catches, and her fingers tighten on the paper. The camera pushes in with small amplitude at slow speed toward her face. <Subject 1>, with a clear, cool, mid-pitched young female voice, (S1) says in a low, stunned whisper: <d>[Chinese] 这封信，写的是我的名字。</d> As soon as each line ends, the speaker's lips close and the jaw stops moving. Every syllable and mouth movement follows <Audio 1> exactly.
No subtitles, captions or on-screen text appear at any time.

overall_soundscape:
Quiet archive room tone with a faint ticking clock and the soft hum of an old lamp.

non_diegetic_music:
N/A"""


def export_workflows(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    jobs = {
        "01_character_portrait_qwen2512_t2i": build_qwen2512_t2i(QwenT2IJob(
            prompt="Vertical portrait photograph, head and shoulders, front view, neutral expression, a slender woman in her mid-20s, "
                   "oval face, small beauty mark under the left eye, low black ponytail. Plain light-grey studio backdrop, soft studio lighting, "
                   "photorealistic, natural skin texture.", width=1024, height=1536)),
        "02_character_sheet_qwen2511_edit": build_qwen2511_edit(QwenEditJob(
            prompt="Character reference sheet of the person in Picture 1 on a plain light-grey background, full-body front, three-quarter and back "
                   "views side by side, wearing an ivory silk blouse with a thin black ribbon tie. Keep the face of Picture 1 exactly.",
            refs=["aidrama/front.png"], width=1536, height=1024)),
        "03_keyframe_qwen2511_edit_multiref": build_qwen2511_edit(QwenEditJob(
            prompt="Vertical 9:16 live-action film still, close-up. The woman from Picture 1 sits at a wooden worktable holding a yellowed envelope. "
                   "The setting is the location shown in Picture 2. Keep the face, hairstyle and costume of the person in Picture 1 exactly.",
            refs=["aidrama/sheet_default.png", "aidrama/plate.png"])),
        "03b_keyframe_qwen_image_2.1_noncommercial": build_qwen_image(QwenImageJob(
            prompt="Vertical film still: the woman from <image1> in the room from <image2>.", refs=["aidrama/sheet_default.png", "aidrama/plate.png"])),
        "04_h3_fl2va_i2v_quality": build_h3(H3Job(prompt="For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully "
                                                         "referenced.\n\nintegrated_multimodal_description: [Shot 1] ...\n\noverall_soundscape: ...\n\nnon_diegetic_music: N/A",
                                                  mode="fl2va", first_frame="aidrama/s01_02.png", seconds=4.5, steps=20, scheduler="simple")),
        "05_h3_ref2va_dialogue_tts_quality": build_h3(H3Job(prompt=DIALOGUE_PROMPT, mode="ref2va", seconds=4.5,
                                                            ref_images=["aidrama/s01_01.png", "aidrama/sheet_lin_wan.png"],
                                                            ref_audios=["aidrama/ep01_g01_dialogue.wav"], anchor_first_frame=False,
                                                            guides=[Guide(0, image="aidrama/s01_01.png")], steps=25, scheduler="beta")),
        "06_h3_ref2va_multicut_segment": build_h3(H3Job(prompt="(三镜头段：见 docs/04-提示词指南.md 的示例)", mode="ref2va", seconds=9.0,
                                                        ref_images=["aidrama/kf1.png", "aidrama/kf2.png", "aidrama/kf3.png", "aidrama/sheet_a.png", "aidrama/sheet_b.png"],
                                                        ref_audios=["aidrama/seg_dialogue.wav"], anchor_first_frame=False,
                                                        guides=[Guide(0, image="aidrama/kf1.png"), Guide(72, image="aidrama/kf2.png"), Guide(144, image="aidrama/kf3.png")])),
        "07_h3_fasth3_draft": build_h3(H3Job(prompt="...", mode="fl2va", fast=True, first_frame="aidrama/kf.png", width=480, height=864)),
        "08_seedvr2_upscale_7b": build_seedvr2(UpscaleJob(video="aidrama/segment.mp4")),
        "09_film_interpolation_x2": build_interp(InterpJob(video="aidrama/segment.mp4")),
        "10_minimax_music3_bgm": build_music(MusicJob(caption="Minimal suspense score: sparse felt piano, low cello drone, 70 BPM. Instrumental only.")),
        "11_infinitetalk_fallback_lipsync": build_infinitetalk(InfiniteTalkJob(image="aidrama/kf.png", audio="aidrama/line.wav", prompt="A woman is talking.")),
        "12_wan_animate2_performance_transfer": build_wan_animate2(AnimateJob(reference_image="aidrama/sheet_a.png", driving_video="aidrama/actor.mp4",
                                                                              prompt="A woman in an ivory silk blouse in a dim archive room.")),
    }
    paths = []
    for name, api in jobs.items():
        p = out / f"{name}.json"
        p.write_text(json.dumps(api, ensure_ascii=False, indent=1), encoding="utf-8")
        paths.append(p)
    return paths
