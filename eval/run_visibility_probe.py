"""Oracle Visibility / Perception Probe ("能不能看见" 测试) for Chagee CCTV AI Audit.

Purpose
-------
After Step 2 prompt tuning (r00 baseline 42.1%, r01 Layer-1 35.5%, r02 Layer-2 42.8%) early-stopped
with 6 dev misses (`R02..R06, R20`) and 4 persistent holdout misses (`R07, R10, R16, R17`) largely
unmoved across 5 full runs, we need to separate two root causes before deciding the next engineering
investment:

  - Hypothesis A (`VISIBLE_WHEN_CROPPED` — Attention Dilution in 5-min clip):
    When the 5-minute video is cropped to a 60-75s window around the labelled OSD timestamp and the
    model is only asked about that station, can the VLM see the micro-action?
      * `A1_FLASH_CROPPED_RECOVERABLE`: Active Flash model sees it when cropped -> validates a
        2-stage coarse-to-fine pipeline (station/event localization -> 60s cropped zoom) using Flash.
      * `A2_PRO_ESCALATION_RECOVERABLE`: Only newest Gemini Pro sees it when cropped -> validates
        2-stage localization + Pro escalation on candidate windows.

  - Hypothesis B (`B_VISUAL_CEILING` — Camera / Visual Limit):
    Neither Flash nor Pro sees the action even in a 60-75s cropped clip under either focused SOP
    auditing or open-ended second-by-second visual diary mode -> camera distance, occlusion, or
    timestamp discrepancy; must be aligned with the customer rather than chased with prompts.

Methodology (Zero Leading-Question Sycophancy)
----------------------------------------------
Never ask a leading yes/no question ("Did the partner apply soap before wetting hands?"). Instead,
each cropped clip (`[start_sec, end_sec]`) is evaluated under 2 non-leading modes x 2 models
(Active Flash from Tab 0 vs Newest Gemini Pro discovered dynamically via `resolve_judge_model`):

  1. `focused_sop`: Standard `AgenticAuditor.analyze_segment` (`WindowResult` JSON) using the r00
     Layer 1 template and ONLY the 3-4 SOP rules for that station. Graded by the calibrated
     `eval/score_run.py` LLM Judge (3-pass median vote).
     Note: `VideoSliceSegment.start_offset_sec` is set to `0.0` so `Finding.with_segment_context`
     keeps `global_offset_sec` relative to the sub-clip (`0..duration`), ensuring
     `score_run.finding_osd_times` computes the sub-clip's true start OSD (`own - global_offset_sec`).
  2. `neutral_diary`: Open-ended, non-leading second-by-second visual observation diary (lists every
     person's hand, sink, faucet, soap, towel, hair/face/glasses, spit-guard, mop, chemical bottle,
     ice-maker part, and tea-container actions with OSD timestamps, without mentioning any SOP rule
     or expected violation). Graded by the newest Gemini Pro judge (3-pass median vote) into
     `1.0` (clearly observed), `0.5` (station/person seen at that time, micro-detail missed or
     ambiguous), or `0.0` (not observed / misidentified).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

def _apply_env_aliases() -> None:
    for alias, name in (
        ("GCP_PROJECT_ID", "GCP_PROJECT"),
        ("GCS_STAGING_BUCKET", "STAGING_BUCKET"),
        ("SOP_SHEET_ID", "MASTER_PROMPT_SHEET_ID"),
    ):
        if os.environ.get(alias) and not os.environ.get(name):
            os.environ[name] = os.environ[alias]
    os.environ.setdefault("VIDEO_MEDIA_PROCESSING", "agentic")


_apply_env_aliases()

from eval.run_gcp_round import (  # noqa: E402
    CLIP_MAX_ATTEMPTS,
    CLIP_RETRY_BASE_SEC,
    DEFAULT_GOLDEN_PATH,
    DEFAULT_ROUNDS_DIR,
    ClipCheckpointStore,
    analyze_clip_with_retry,
    load_folder_specs,
    resolve_or_ingest_video_slice,
    upload_directory_to_gcs,
)
from google.genai import types  # noqa: E402

import cctv_audit.agentic_auditor as _ca_mod  # noqa: E402
from cctv_audit.agentic_auditor import (  # noqa: E402
    AgenticAuditor,
    build_video_part as _orig_build_video_part,
)
from cctv_audit.config import AuditConfig, config  # noqa: E402
from cctv_audit.gcp import generate_content_with_retry  # noqa: E402
from cctv_audit.prompt_manager import (  # noqa: E402
    GoogleSheetsConfigClient,
    PromptManager,
    PromptModelConfig,
    render_v25_system_instruction,
)
from cctv_audit.video_ingestor import VideoSliceSegment  # noqa: E402
from eval import score_run as sr  # noqa: E402
from eval.tune_loop import deserialize_rules_json  # noqa: E402


def _media_processing_for_model(model_name: str) -> str:
    """Vertex AI only enables MediaProcessing.AGENTIC on Gemini 3.x Flash models, not Pro models."""
    if "pro" in (model_name or "").lower():
        return "static"
    return "agentic"


def build_video_part(
    segment: VideoSliceSegment, model_name: str, media_processing: str | None = None
) -> types.Part:
    """Routes Flash models to agentic video mode and Pro models to static video mode."""
    effective_mode = (
        media_processing
        if media_processing is not None
        else _media_processing_for_model(model_name)
    )
    return _orig_build_video_part(segment, model_name, media_processing=effective_mode)


_ca_mod.build_video_part = build_video_part

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("eval.run_visibility_probe")

DEFAULT_PROBE_OUT_DIR = THIS_DIR / "results" / "visibility_probe"


@dataclasses.dataclass(frozen=True)
class ProbeWindowSpec:
    """Specification of one 60-75s cropped sub-clip around a golden item's labelled OSD time."""

    probe_id: str
    item_ids: tuple[str, ...]
    role: str  # "dev_miss" | "holdout_miss" | "positive_control"
    video_file_id: str
    video_filename: str
    start_sec: float
    end_sec: float
    target_osd: str
    station_focus: str
    rule_ids: tuple[str, ...]
    note: str


# All clip offsets verified against OSD-to-clip calibration across 5 runs (spread <= 1s):
# - 1Yyu9FIUSXFKQxAoxpi_dzt7CRtbDrzEN (HW-C Footage2): start OSD = 08:05:00
# - 1UspaDFLNrbmkkq3T0f9aOdv6CXWhG8WJ (HW-C Footage3): start OSD = 12:00:01
# - 1wWjhOh0lm3aa4rOF0H1byAudzmh8e0NI (HW-C Footage4): start OSD = 12:05:01
# - 1nkT-Tsmc1Da9sSbyn9d7imseSCKXkpm6 (ICE-B Footage2): start OSD = 17:40:00
# - 1CNW742qkkufq2AGEU2BNQ8zTYvirCmtb (ICE-B Footage3): start OSD = 17:44:58
# - 1kjXuFFgq03q0atGYSwfSN4Cygw_7xdgO (HW-B Footage1): start OSD = 07:59:58
# - 1fS6tKNxpfacwaph35bUSBncEYSLM1bJG (HW-B Footage2): start OSD = 08:05:00
# - 1912Z6XrJcVerwr2vZjNDvT23GvbNl1eF (HW-B Footage4): start OSD = 12:05:00
# - 1hVp87SjOdfbAhnsSLLxQVbVGF-jlGFdI (ICE-C Footage9): start OSD = 13:19:59
# - 1ilmpdq_GOxy4_m_LjctbkhsPQoXqJHZS (ICE-C Footage10): start OSD = 13:25:01
# - 1jCGFpG8UHhQmr9GUp8Ii0bUPr50lTn0F (ICE-C Footage13): start OSD = 13:40:00
PROBE_SPECS: tuple[ProbeWindowSpec, ...] = (
    # --- 6 Dev Misses (7 windows because R20 spans Footage 2 & Footage 3 while R04+R05 share 1 window) ---
    ProbeWindowSpec(
        probe_id="P01_R02_hw_c_f2",
        item_ids=("R02",),
        role="dev_miss",
        video_file_id="1Yyu9FIUSXFKQxAoxpi_dzt7CRtbDrzEN",
        video_filename="Footage2-20260910165815394_GD2902390_hcDownloadP_C 8_8_video (2).mov",
        start_sec=0.0,
        end_sec=55.0,
        target_osd="08:05:18",
        station_focus="handwashing_sink",
        rule_ids=("A1", "A2", "A3", "A4"),
        note="R02 (dev): 08:05:18 (clip 18s) Partner not dry hand with hand towel after handwashing",
    ),
    ProbeWindowSpec(
        probe_id="P02_R03_hw_c_f3",
        item_ids=("R03",),
        role="dev_miss",
        video_file_id="1UspaDFLNrbmkkq3T0f9aOdv6CXWhG8WJ",
        video_filename="Footage3-20260910170324004_GD2902390_hcDownloadP_C 8_8_video (2).mov",
        start_sec=10.0,
        end_sec=75.0,
        target_osd="12:00:40",
        station_focus="hygiene_and_ice_maker",
        rule_ids=("A1", "A2", "A3", "A4"),
        note="R03 (dev): 12:00:40 (clip 39s) Partner touches hair & spectacles and touches ice maker without handwashing",
    ),
    ProbeWindowSpec(
        probe_id="P03_R04_R05_hw_c_f4",
        item_ids=("R04", "R05"),
        role="dev_miss",
        video_file_id="1wWjhOh0lm3aa4rOF0H1byAudzmh8e0NI",
        video_filename="Footage4-20260910170336393_GD2902390_hcDownloadP_C 8_8_video (1).mov",
        start_sec=110.0,
        end_sec=175.0,
        target_osd="12:07:20",
        station_focus="sink_and_front_bar",
        rule_ids=("A1", "A2", "A3", "A4"),
        note="R05 (12:07:20, clip 139s) apply soap before wet hand + R04 (12:07:23, clip 142s) touches spit guard",
    ),
    ProbeWindowSpec(
        probe_id="P04_R06_hw_c_f4",
        item_ids=("R06",),
        role="dev_miss",
        video_file_id="1wWjhOh0lm3aa4rOF0H1byAudzmh8e0NI",
        video_filename="Footage4-20260910170336393_GD2902390_hcDownloadP_C 8_8_video (1).mov",
        start_sec=235.0,
        end_sec=301.0,
        target_osd="12:09:46",
        station_focus="tea_maker_bar",
        rule_ids=("C8", "A1", "A2"),
        note="R06 (dev): 12:09:46 (clip 285s) Not stir tea base within 30s",
    ),
    ProbeWindowSpec(
        probe_id="P05_R20_ice_b_f2",
        item_ids=("R20",),
        role="dev_miss",
        video_file_id="1nkT-Tsmc1Da9sSbyn9d7imseSCKXkpm6",
        video_filename="Footage2-20260910162517431_GE7411586_hcDownloadP_C 4_4_video.mov",
        start_sec=185.0,
        end_sec=255.0,
        target_osd="17:43:40",
        station_focus="ice_maker_chemicals",
        rule_ids=("B0", "B2", "B3", "B4", "C1"),
        note="R20 (dev, Footage 2 window around 17:43:40, clip 220s): Use multipurpose chemical to clean white cover & tube",
    ),
    ProbeWindowSpec(
        probe_id="P06_R20_ice_b_f3",
        item_ids=("R20",),
        role="dev_miss",
        video_file_id="1CNW742qkkufq2AGEU2BNQ8zTYvirCmtb",
        video_filename="Footage3-20260910162554360_GE7411586_hcDownloadP_C 4_4_video.mov",
        start_sec=0.0,
        end_sec=65.0,
        target_osd="17:45:18",
        station_focus="ice_maker_chemicals",
        rule_ids=("B0", "B2", "B3", "B4", "C1"),
        note="R20 (dev, Footage 3 window 17:44:58-17:46:03 where white cover & water curtain are brushed)",
    ),
    # --- 4 Persistent Holdout Misses ---
    ProbeWindowSpec(
        probe_id="P07_R07_hw_b_f1",
        item_ids=("R07",),
        role="holdout_miss",
        video_file_id="1kjXuFFgq03q0atGYSwfSN4Cygw_7xdgO",
        video_filename="Footage1-20260910171219785_GE7411586_hcDownloadP_C 4_4_video (1).mov",
        start_sec=10.0,
        end_sec=75.0,
        target_osd="08:00:39",
        station_focus="handwashing_sink",
        rule_ids=("A1", "A2", "A3", "A4"),
        note="R07 (holdout): 08:00:39 (clip 41s) Soaping time is less than 20s",
    ),
    ProbeWindowSpec(
        probe_id="P08_R10_hw_b_f4",
        item_ids=("R10",),
        role="holdout_miss",
        video_file_id="1912Z6XrJcVerwr2vZjNDvT23GvbNl1eF",
        video_filename="Footage4-20260910171448959_GE7411586_hcDownloadP_C 4_4_video (1).mov",
        start_sec=85.0,
        end_sec=155.0,
        target_osd="12:06:59",
        station_focus="hygiene_and_chiller",
        rule_ids=("A1", "A2", "A3", "C1"),
        note="R10 (holdout): 12:06:59 (clip 119s) Partner handles mop and touches food ingredients in chiller without handwashing",
    ),
    ProbeWindowSpec(
        probe_id="P09_R16_ice_c_f9",
        item_ids=("R16",),
        role="holdout_miss",
        video_file_id="1hVp87SjOdfbAhnsSLLxQVbVGF-jlGFdI",
        video_filename="Footage9-20260910123928821_GD2902390_hcDownloadP_C 8_8_video.mov",
        start_sec=180.0,
        end_sec=245.0,
        target_osd="13:23:33",
        station_focus="handwashing_sink",
        rule_ids=("A1", "A2", "A3", "A4"),
        note="R16 (holdout): 13:23:33 (clip 214s) Not rinse hand before applying handwashing gel",
    ),
    ProbeWindowSpec(
        probe_id="P10_R17_ice_c_f10",
        item_ids=("R17",),
        role="holdout_miss",
        video_file_id="1ilmpdq_GOxy4_m_LjctbkhsPQoXqJHZS",
        video_filename="Footage10-20260910133855518_GD2902390_hcDownloadP_C 8_8_video.mov",
        start_sec=165.0,
        end_sec=235.0,
        target_osd="13:28:20",
        station_focus="ice_maker_sanitising",
        rule_ids=("B0", "B1-1", "B1-2", "B2", "B3", "B4"),
        note="R17 (holdout): 13:28:20 (clip 199s) After spray sanitiser, do not wait 5 min before cleaning and installing back",
    ),
    # --- 2 Positive Controls (100% hit rate across all 5 full runs) ---
    ProbeWindowSpec(
        probe_id="P11_CTRL_R08_hw_b_f2",
        item_ids=("R08",),
        role="positive_control",
        video_file_id="1fS6tKNxpfacwaph35bUSBncEYSLM1bJG",
        video_filename="Footage2-20260910171233496_GE7411586_hcDownloadP_C 4_4_video (1).mov",
        start_sec=0.0,
        end_sec=65.0,
        target_osd="08:05:30",
        station_focus="handwashing_sink",
        rule_ids=("A1", "A2", "A3", "A4"),
        note="R08 (positive control): 08:05:30 (clip 30s) Soaping time is less than 20s",
    ),
    ProbeWindowSpec(
        probe_id="P12_CTRL_R19_ice_c_f13",
        item_ids=("R19",),
        role="positive_control",
        video_file_id="1jCGFpG8UHhQmr9GUp8Ii0bUPr50lTn0F",
        video_filename="Footage13-20260910124058250_GD2902390_hcDownloadP_C 8_8_video.mov",
        start_sec=120.0,
        end_sec=190.0,
        target_osd="13:42:35",
        station_focus="ice_maker_sanitising",
        rule_ids=("B0", "B1-1", "B1-2", "B2", "B3", "B4"),
        note="R19 (positive control): 13:42:35 (clip 155s) After spray sanitiser at interior of ice maker door, do not wait 5 min",
    ),
)

NEUTRAL_DIARY_SYSTEM_INSTRUCTION = """你是连锁茶饮门店监控画面的客观视觉观察员。
你的任务是：**不预设任何违规结论**，把这段约 1 分钟的监控短视频里**每一名员工**的物理动作按时间顺序逐秒记成客观视觉流水账。

观察要求：
1. 先校准画面角落（左上角或右上角）的 OSD 水印时钟（HH:MM:SS），每条记录都同时写出「片段相对时间 MM:SS」与「OSD 时间 HH:MM:SS」。
2. 如果画面中有多名员工，按人物分别记录，切勿因为某一人动作显眼（如看手机、站立）而漏记同一时刻另一人在水槽、吧台、制冰机、泡茶机或货架旁的动作。
3. 针对以下 4 类工位动作，必须写出精细到秒的物理细节（看得清就写具体事实，被遮挡或看不清就写「被遮挡/看不清」）：
   - **水槽动作**：员工何时走到水槽、双手是先伸到水龙头流水下冲湿还是干手直接先按压皂液器、双手对搓泡沫从第几秒到第几秒（净时长几秒）、何时冲水、冲完水离开前有没有抽取擦手纸把双手擦干。
   - **手部接触**：双手有没有触碰头发、眼镜、脸部/口罩、吧台透明防飞沫挡板（spit guard）、地面、拖把（mop）或清洁工具，触碰后接着去碰了什么设备/食材/冰柜/制冰机。
   - **制冰机与清洁工具/容器**：员工手里拿的喷壶/瓶子/量杯/桶是什么颜色和形状、往哪个制冰机部件（白色外盖门板 white cover、布水管 water distribution tube、挡水帘 water curtain、机身内腔）喷洒或涂刷、喷洒后隔了多久就开始用毛巾/刷子擦拭或装回、毛巾是从哪里拿起的。
   - **泡茶机与茶汤容器**：泡茶机何时出茶/滴滤、茶汤倒入哪个容器、之后多少秒内有没有人拿长勺/搅拌棒伸进容器搅拌。"""

NEUTRAL_DIARY_USER_PROMPT = (
    "请逐秒观察本段监控短视频，按时间顺序完整列出画面中所有员工的双手、水槽、吧台、制冰机、泡茶机与工具接触动作明细（含 OSD 时间戳）。"
)

DIARY_JUDGE_TEMPLATE = """你是茶饮门店视频感知评测裁判。任务：判断下面这份「AI 逐秒客观视觉流水账」是否肉眼观察到了人工标注所描述的物理动作。

【人工标注的物理事件】
{reference}

【AI 对该 60 秒裁剪片段的逐秒视觉流水账（非引导式观察）】
{response}

评分标准（只看流水账里有没有观察到对应的物理动作事实，不要求流水账使用 SOP 条款编号）：
- 1 分（CLEARLY_OBSERVED）：流水账明确观察并写出了人工标注对应的关键物理动作（例如：明确写出未湿手先按皂液器、洗完手未用纸巾擦干直接离开、摸头发/眼镜后碰制冰机、碰吧台挡板后继续作业、拿拖把后碰冷柜食材、搓洗秒数不足20秒、喷洒后未等待即擦拭/装回、使用黄瓶/非标清洁剂或工具刷洗制冰机部件、出茶后未在30秒内搅拌）。
- 0.5 分（PARTIALLY_OBSERVED）：流水账在对应时间看到了该员工在该工位的相关动作（例如看到了那次洗手、看到了在制冰机或吧台操作），但没有看清或漏记了最关键的那个细节（如没写先湿手还是先按皂、没写擦没擦干、把清洁制冰机误认成粉刷墙面等）。
- 0 分（NOT_OBSERVED）：流水账完全没提到该员工在该时刻的这一动作，或看成了完全无关的事。

只返回 JSON，不要任何其他文字：
{{"score": <0 或 0.5 或 1>, "explanation": "OBSERVED=<CLEARLY_OBSERVED 或 PARTIALLY_OBSERVED 或 NOT_OBSERVED>; <一句中文理由，引用流水账里的关键原话>"}}"""


_SOURCE_DOWNLOAD_LOCKS: dict[str, asyncio.Lock] = {}


async def cut_and_upload_subclip(
    *,
    source_gcs_uri: str,
    start_sec: float,
    end_sec: float,
    probe_id: str,
    probe_run_id: str,
    bucket_name: str,
    project_id: str,
    work_dir: Path,
) -> tuple[Path, str]:
    """Downloads cached 5-min slice (once per file_id, race-free), cuts `[start_sec, end_sec]` via FFmpeg, and uploads to GCS."""
    from google.cloud import storage

    target_blob_name = f"eval/visibility_probe/{probe_run_id}/clips/{probe_id}.mp4"
    target_gcs_uri = f"gs://{bucket_name}/{target_blob_name}"
    local_subclip = work_dir / "subclips" / f"{probe_id}.mp4"
    local_subclip.parent.mkdir(parents=True, exist_ok=True)

    # Download source 5-min MP4 once per source_gcs_uri under a per-source lock with atomic rename
    # so concurrent windows sharing the same video (e.g. P03 and P04 on Footage4) never read a
    # partially-written MP4 ("moov atom not found").
    src_name = source_gcs_uri.removeprefix(f"gs://{bucket_name}/")
    safe_src_key = src_name.replace("/", "__")
    local_src = work_dir / "source_slices" / safe_src_key
    local_src.parent.mkdir(parents=True, exist_ok=True)

    dl_lock = _SOURCE_DOWNLOAD_LOCKS.setdefault(f"{work_dir}:{safe_src_key}", asyncio.Lock())
    async with dl_lock:
        if not local_src.exists() or local_src.stat().st_size == 0:
            tmp_dl = local_src.with_name(f".{local_src.name}.{probe_id}.part")

            def _dl() -> None:
                client = storage.Client(project=project_id)
                client.bucket(bucket_name).blob(src_name).download_to_filename(str(tmp_dl))
                tmp_dl.replace(local_src)

            await asyncio.to_thread(_dl)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg binary is required to cut visibility probe sub-clips.")

    duration = max(1.0, float(end_sec) - float(start_sec))
    cmd = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-ss",
        f"{start_sec:.2f}",
        "-t",
        f"{duration:.2f}",
        "-i",
        str(local_src),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-movflags",
        "+faststart",
        str(local_subclip),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    if proc.returncode != 0 or not local_subclip.exists():
        raise RuntimeError(
            f"FFmpeg subclip cut failed for {probe_id}: {stderr.decode('utf-8', 'replace')[:300]}"
        )

    def _ul() -> None:
        client = storage.Client(project=project_id)
        client.bucket(bucket_name).blob(target_blob_name).upload_from_filename(
            str(local_subclip), content_type="video/mp4"
        )

    await asyncio.to_thread(_ul)
    return local_subclip, target_gcs_uri


def build_focused_prompt_config(
    *,
    rounds_dir: Path,
    rule_ids: Sequence[str],
    model_version: str,
    probe_id: str,
) -> PromptModelConfig:
    """Builds a station-focused PromptModelConfig using r00 Layer 1 + only `rule_ids` from r00 Layer 2."""
    r00_dir = rounds_dir / "r00"
    layer1 = (r00_dir / "layer1_template.md").read_text(encoding="utf-8")
    all_rules = deserialize_rules_json(
        json.loads((r00_dir / "layer2_rules.json").read_text(encoding="utf-8"))
    )
    wanted = set(rule_ids)
    selected = [r for r in all_rules if r.rule_id in wanted]
    if not selected:
        raise ValueError(f"No matching rules for {rule_ids} in r00/layer2_rules.json")
    prompt_ver = f"probe_{probe_id}"
    sys_inst = render_v25_system_instruction(
        prompt_version=prompt_ver,
        rules=selected,
        layer1_template=layer1,
    )
    return PromptModelConfig(
        active_prompt_version=prompt_ver,
        active_model_version=model_version,
        fallback_model_version=model_version,
        rules=selected,
        system_instruction=sys_inst,
    )


async def run_neutral_diary_with_retry(
    *,
    segment: VideoSliceSegment,
    model_version: str,
    label: str,
) -> str:
    """Runs the non-leading second-by-second visual diary query on `segment` with bounded retry."""
    video_part = build_video_part(segment, model_version)
    gen_config = types.GenerateContentConfig(
        system_instruction=NEUTRAL_DIARY_SYSTEM_INSTRUCTION,
        temperature=0.0,
        max_output_tokens=16384,
    )
    for attempt in range(1, CLIP_MAX_ATTEMPTS + 1):
        try:
            resp = await generate_content_with_retry(
                model=model_version,
                contents=[video_part, NEUTRAL_DIARY_USER_PROMPT],
                gen_config=gen_config,
            )
            return (resp.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            if attempt == CLIP_MAX_ATTEMPTS:
                logger.error("[%s] neutral diary failed after %d attempts: %r", label, attempt, exc)
                raise
            delay = CLIP_RETRY_BASE_SEC * attempt
            logger.warning(
                "[%s] neutral diary attempt %d/%d failed (%r); retrying in %.0fs",
                label,
                attempt,
                CLIP_MAX_ATTEMPTS,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def classify_item_visibility(
    *,
    flash_sop_score: float,
    flash_diary_score: float,
    pro_sop_score: float,
    pro_diary_score: float,
) -> tuple[str, str]:
    """Maps the 4 probe scores into a concrete engineering diagnosis and plain-Chinese action."""
    flash_best = max(flash_sop_score, flash_diary_score)
    pro_best = max(pro_sop_score, pro_diary_score)
    if flash_best >= 0.5:
        return (
            "A1_FLASH_CROPPED_RECOVERABLE",
            "看得见（5分钟长片注意力稀释）：裁剪到约60秒后 Flash 即可识别，适合用「两段式先定位时间点、再剪短片段放大复核」架构救回",
        )
    if pro_best >= 0.5:
        return (
            "A2_PRO_ESCALATION_RECOVERABLE",
            "需 Pro 模型才能看清：裁剪到约60秒后 Flash 仍漏，但 Pro 能识别，适合用「Flash 粗定位 + Pro 对关键短窗复核」救回",
        )
    return (
        "B_VISUAL_CEILING",
        "画面本身看不清或标注时刻无可见动作：裁剪到60秒后 Flash 与 Pro 在规则模式与纯视觉流水账下均无法识别，属机位/遮挡/分辨率上限，建议与客户对齐预期",
    )


def score_focused_sop_findings(
    *,
    golden_items: list[dict[str, Any]],
    spec: ProbeWindowSpec,
    findings_json: list[dict[str, Any]],
    judge_fn: Callable[[list[dict[str, str]]], list[tuple[float | None, str]]],
) -> dict[str, dict[str, Any]]:
    """Scores `focused_sop` findings for `spec.item_ids` using `eval/score_run.py`."""
    fake_job = {
        "job_id": spec.probe_id,
        "completed_segments": {
            f"{spec.video_file_id}:0": {
                "file_id": spec.video_file_id,
                "filename": spec.video_filename,
                "segment_index": 0,
                "findings": findings_json,
            }
        },
    }
    flat = sr.flatten_findings([fake_job])
    # For R20 Footage 3 window (17:44:58-17:46:03), golden_v1 only lists 17:43:40 in osd_times
    # even though video_filenames includes Footage 2 & 3. Add target_osd so Footage 3 findings
    # within the cropped window are not dropped by the +-60s prefilter around 17:43:40.
    sub_items: list[dict[str, Any]] = []
    for it in golden_items:
        if it["item_id"] not in spec.item_ids:
            continue
        it_copy = json.loads(json.dumps(it))
        for p in it_copy["parts"]:
            if spec.target_osd and spec.target_osd not in p["osd_times"]:
                p["osd_times"] = list(p["osd_times"]) + [spec.target_osd]
        sub_items.append(it_copy)

    rep = sr.score(sub_items, flat, judge_fn)
    return {it["item_id"]: it for it in rep["items"]}


def judge_diary_cases(
    cases: list[dict[str, str]],
    *,
    project: str,
    judge_model: str,
    judge_passes: int = 3,
    credentials: Any = None,
    diary_judge_fn: Callable[[list[dict[str, str]]], list[tuple[float | None, str]]] | None = None,
) -> list[tuple[float, str]]:
    """Grades `neutral_diary` outputs against golden items using Vertex AI GenAI Eval SDK."""
    if not cases:
        return []
    if diary_judge_fn is not None:
        raw = diary_judge_fn(cases)
        return [(float(s or 0.0), exp) for s, exp in raw]

    import agentplatform
    from agentplatform import types as at
    from google.genai import types as gt

    client = agentplatform.Client(project=project, location="global", credentials=credentials)
    metric = at.LLMMetric(
        name="diary_visibility",
        prompt_template=DIARY_JUDGE_TEMPLATE,
        judge_model=f"projects/{project}/locations/global/publishers/google/models/{judge_model}",
    )
    ds = at.EvaluationDataset(
        eval_cases=[
            at.EvalCase(
                prompt=gt.UserContent(c["case_id"]),
                responses=[at.ResponseCandidate(response=gt.ModelContent(c["response"] or "（空输出）"))],
                reference=at.ResponseCandidate(response=gt.ModelContent(c["reference"])),
            )
            for c in cases
        ]
    )
    passes: list[list[tuple[float | None, str]]] = []
    for _ in range(judge_passes):
        result = client.evals.evaluate(dataset=ds, metrics=[metric])
        cur: list[tuple[float | None, str]] = []
        for case_res in result.eval_case_results:
            mr = case_res.response_candidate_results[0].metric_results["diary_visibility"]
            err = getattr(mr, "error_message", None)
            sc = mr.score if mr.score in (0.0, 0.5, 1.0) else 0.0
            cur.append((sc, mr.explanation or (f"JUDGE_ERROR: {err}" if err else "")))
        passes.append(cur)
    voted = sr.vote(passes)
    return [(float(s or 0.0), exp) for s, exp in voted]


def render_probe_markdown(report: dict[str, Any]) -> str:
    """Renders a plain-Chinese diagnostic table and summary for the visibility probe."""
    lines = [
        f"# 「能不能看见」短片段感知探针报告 (`{report['probe_run_id']}`)",
        "",
        f"- **评测时间**: `{report['created_at']}`",
        f"- **Flash 模型（线上同款）**: `{report['flash_model']}`",
        f"- **Pro 模型（动态最新）**: `{report['pro_model']}`",
        f"- **裁判模型**: `{report['judge_model']}`",
        "",
        "## 一、总体分类统计",
        "",
    ]
    counts = report["summary_counts"]
    lines.extend(
        [
            f"- **A1 · 剪短后 Flash 即可看清（注意力稀释，可靠两段式定位+放大救回）**: `{counts.get('A1_FLASH_CROPPED_RECOVERABLE', 0)}` 项",
            f"- **A2 · 剪短后需 Pro 才能看清（可靠粗定位 + Pro 复核救回）**: `{counts.get('A2_PRO_ESCALATION_RECOVERABLE', 0)}` 项",
            f"- **B · 剪短后 Flash 与 Pro 均看不清（画面/机位/遮挡物理上限）**: `{counts.get('B_VISUAL_CEILING', 0)}` 项",
            "",
            "## 二、逐项测试结果明细",
            "",
            "| 编号 | 角色 | 客户标注 | 裁剪窗口 | Flash 专项规则 | Flash 纯视觉流水账 | Pro 专项规则 | Pro 纯视觉流水账 | 诊断结论 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for row in report["item_diagnostics"]:
        lines.append(
            f"| `{row['item_id']}` | `{row['role']}` | {row['finding_verbatim'].replace('|', '/')} | "
            f"`{row['windows_str']}` | {row['flash_sop_score']:.2g} | {row['flash_diary_score']:.2g} | "
            f"{row['pro_sop_score']:.2g} | {row['pro_diary_score']:.2g} | **{row['diagnosis_code']}**：{row['diagnosis_zh']} |"
        )
    lines.extend(["", "## 三、各裁剪窗口原始观察与裁判理由", ""])
    for w in report["window_results"]:
        lines.append(f"### `{w['probe_id']}` — {w['note']} (`{w['start_sec']:.0f}s..{w['end_sec']:.0f}s`, 目标 OSD `{w['target_osd']}`)")
        for m_key in ("flash", "pro"):
            m_res = w["models"][m_key]
            lines.append(f"- **`{m_key}` (`{m_res['model_version']}`)**:")
            lines.append(f"  - 专项规则 (`focused_sop`) 条目数 `{len(m_res['findings'])}`：{json.dumps(m_res['sop_scores'], ensure_ascii=False)}")
            lines.append(f"  - 纯视觉流水账 (`neutral_diary`) 判分：{json.dumps(m_res['diary_scores'], ensure_ascii=False)}")
            diary_preview = (m_res["diary_text"] or "").replace("\n", " ")[:360]
            lines.append(f"  - 流水账摘要：{diary_preview}")
        lines.append("")
    return "\n".join(lines) + "\n"


async def run_probe_async(
    args: argparse.Namespace,
    *,
    subclip_cutter: Callable[..., Any] = cut_and_upload_subclip,
    sop_judge_fn: Callable[[list[dict[str, str]]], list[tuple[float | None, str]]] | None = None,
    diary_judge_fn: Callable[[list[dict[str, str]]], list[tuple[float | None, str]]] | None = None,
) -> dict[str, Any]:
    """Executes the full visibility probe across `PROBE_SPECS` and returns the structured report."""
    _apply_env_aliases()
    cfg = AuditConfig()
    if not cfg.gcp_project:
        raise RuntimeError("GCP_PROJECT is not set; refusing to run against placeholder default.")
    if config.gcp_project != cfg.gcp_project or config.staging_bucket != cfg.staging_bucket:
        raise RuntimeError(
            f"cctv_audit.config was built with ({config.gcp_project}, {config.staging_bucket}) "
            f"!= runtime ({cfg.gcp_project}, {cfg.staging_bucket})."
        )

    probe_run_id = args.probe_run_id or f"vprobe_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    golden_items = [
        json.loads(line)
        for line in Path(args.golden).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    golden_by_id = {it["item_id"]: it for it in golden_items}

    # Resolve active Flash model from Tab 0 and newest Pro model live.
    sheet_client = GoogleSheetsConfigClient()
    pm = PromptManager(sheet_client=sheet_client)
    active_cfg = await pm.load_active_config(cfg.master_prompt_sheet_id)
    flash_model = args.flash_model or active_cfg.active_model_version
    pro_model = args.pro_model or await asyncio.to_thread(sr.resolve_judge_model, cfg.gcp_project)
    judge_model = args.judge_model or pro_model

    logger.info(
        "Starting visibility probe %s: flash=%s, pro=%s, judge=%s",
        probe_run_id,
        flash_model,
        pro_model,
        judge_model,
    )

    # Map file_id -> (video_dict, candidate_job_ids) from frozen folder specs
    folder_specs = load_folder_specs(args.golden)
    video_lookup: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for fspec in folder_specs:
        for vdict in fspec["videos"]:
            video_lookup[str(vdict["file_id"])] = (vdict, fspec["candidate_job_ids"])

    ckpt = ClipCheckpointStore(
        run_id=probe_run_id,
        bucket_name=cfg.staging_bucket,
        project=cfg.gcp_project,
        local_dir=args.ckpt_local_dir,
    )
    auditor = AgenticAuditor(gemini_concurrency=args.concurrency)
    sem = asyncio.Semaphore(args.concurrency)
    work_dir = Path(tempfile.mkdtemp(prefix=f"{probe_run_id}_"))

    selected_specs = [
        s for s in PROBE_SPECS if not args.probe_ids or s.probe_id in set(args.probe_ids)
    ]

    async def _eval_one_window(spec: ProbeWindowSpec) -> dict[str, Any]:
        async with sem:
            saved_flash = await asyncio.to_thread(ckpt.get, spec.probe_id, "flash")
            saved_pro = await asyncio.to_thread(ckpt.get, spec.probe_id, "pro")
            if saved_flash is not None and saved_pro is not None:
                logger.info("[%s] Resuming both flash and pro from checkpoint (skipping video cut)", spec.probe_id)
                return {
                    "probe_id": spec.probe_id,
                    "item_ids": list(spec.item_ids),
                    "role": spec.role,
                    "video_file_id": spec.video_file_id,
                    "video_filename": spec.video_filename,
                    "start_sec": spec.start_sec,
                    "end_sec": spec.end_sec,
                    "target_osd": spec.target_osd,
                    "station_focus": spec.station_focus,
                    "rule_ids": list(spec.rule_ids),
                    "note": spec.note,
                    "subclip_gcs_uri": f"gs://{cfg.staging_bucket}/eval/visibility_probe/{probe_run_id}/clips/{spec.probe_id}.mp4",
                    "models": {"flash": saved_flash, "pro": saved_pro},
                }

            vdict, cand_jids = video_lookup[spec.video_file_id]
            full_seg = await resolve_or_ingest_video_slice(
                cfg=cfg,
                bucket_name=cfg.staging_bucket,
                video_dict=vdict,
                candidate_job_ids=cand_jids,
                ingestor=None,
            )
            local_subclip, subclip_gcs_uri = await subclip_cutter(
                source_gcs_uri=full_seg.gcs_uri,
                start_sec=spec.start_sec,
                end_sec=spec.end_sec,
                probe_id=spec.probe_id,
                probe_run_id=probe_run_id,
                bucket_name=cfg.staging_bucket,
                project_id=cfg.gcp_project,
                work_dir=work_dir,
            )
            duration = float(spec.end_sec) - float(spec.start_sec)
            # start_offset_sec=0.0 ensures Finding.with_segment_context keeps global_offset_sec
            # relative to the cropped sub-clip so score_run.finding_osd_times computes the
            # sub-clip's true start OSD.
            sub_seg = VideoSliceSegment(
                source_file_id=spec.video_file_id,
                source_filename=spec.video_filename,
                segment_index=0,
                start_offset_sec=0.0,
                end_offset_sec=duration,
                local_path=local_subclip,
                gcs_uri=subclip_gcs_uri,
                width=full_seg.width,
                height=full_seg.height,
            )

            models_out: dict[str, Any] = {}
            for model_tag, model_ver, pre_saved in (
                ("flash", flash_model, saved_flash),
                ("pro", pro_model, saved_pro),
            ):
                if pre_saved is not None:
                    logger.info("[%s:%s] Resuming from checkpoint", spec.probe_id, model_tag)
                    models_out[model_tag] = pre_saved
                    continue

                prompt_cfg = build_focused_prompt_config(
                    rounds_dir=args.rounds_dir,
                    rule_ids=spec.rule_ids,
                    model_version=model_ver,
                    probe_id=f"{spec.probe_id}_{model_tag}",
                )
                w_res, ledger_row = await analyze_clip_with_retry(
                    auditor,
                    label=f"{spec.probe_id}:{model_tag}:sop",
                    audit_id=f"{probe_run_id}_{spec.probe_id}_{model_tag}",
                    folder_id=spec.station_focus,
                    segment=sub_seg,
                    prompt_cfg=prompt_cfg,
                    prior_carryover_summary="",
                    evidence_dir=None,
                )
                diary_text = await run_neutral_diary_with_retry(
                    segment=sub_seg,
                    model_version=model_ver,
                    label=f"{spec.probe_id}:{model_tag}:diary",
                )
                entry = {
                    "model_tag": model_tag,
                    "model_version": model_ver,
                    "findings": [f.model_dump(mode="json") for f in w_res.findings],
                    "ledger_row": ledger_row.model_dump(mode="json"),
                    "diary_text": diary_text,
                }
                await asyncio.to_thread(ckpt.put, spec.probe_id, model_tag, entry)
                models_out[model_tag] = entry

            return {
                "probe_id": spec.probe_id,
                "item_ids": list(spec.item_ids),
                "role": spec.role,
                "video_file_id": spec.video_file_id,
                "video_filename": spec.video_filename,
                "start_sec": spec.start_sec,
                "end_sec": spec.end_sec,
                "target_osd": spec.target_osd,
                "station_focus": spec.station_focus,
                "rule_ids": list(spec.rule_ids),
                "note": spec.note,
                "subclip_gcs_uri": subclip_gcs_uri,
                "models": models_out,
            }

    try:
        window_results = list(await asyncio.gather(*(_eval_one_window(s) for s in selected_specs)))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # Build SOP judge function (live SDK judge or injected fake for tests)
    effective_sop_judge = sop_judge_fn
    if effective_sop_judge is None:
        def _live_sop_judge(cases: list[dict[str, str]]) -> list[tuple[float | None, str]]:
            passes = []
            for _ in range(args.judge_passes):
                verdicts, _ = sr.sdk_judge(cases, cfg.gcp_project, judge_model)
                passes.append(verdicts)
            return sr.vote(passes)

        effective_sop_judge = _live_sop_judge

    # Score all window_results (both focused_sop and neutral_diary)
    spec_by_id = {s.probe_id: s for s in selected_specs}
    for w in window_results:
        spec = spec_by_id[w["probe_id"]]
        for model_tag in ("flash", "pro"):
            m_entry = w["models"][model_tag]
            sop_scored = await asyncio.to_thread(
                score_focused_sop_findings,
                golden_items=golden_items,
                spec=spec,
                findings_json=m_entry["findings"],
                judge_fn=effective_sop_judge,
            )
            m_entry["sop_scores"] = {
                iid: {
                    "score": float(item_rep["score"]),
                    "explanations": [p["explanation"] for p in item_rep["parts"]],
                }
                for iid, item_rep in sop_scored.items()
            }

            diary_cases: list[dict[str, str]] = []
            for iid in spec.item_ids:
                g_item = golden_by_id[iid]
                for p in g_item["parts"]:
                    diary_cases.append(
                        {
                            "case_id": f"{w['probe_id']}_{model_tag}_{p['part_id']}",
                            "item_id": iid,
                            "part_id": p["part_id"],
                            "reference": sr.render_reference(g_item, p),
                            "response": m_entry["diary_text"],
                        }
                    )
            diary_verdicts = await asyncio.to_thread(
                judge_diary_cases,
                diary_cases,
                project=cfg.gcp_project,
                judge_model=judge_model,
                judge_passes=args.judge_passes,
                diary_judge_fn=diary_judge_fn,
            )
            by_item_diary: dict[str, list[tuple[float, str]]] = {}
            for c_meta, (sc, exp) in zip(diary_cases, diary_verdicts):
                by_item_diary.setdefault(c_meta["item_id"], []).append((sc, exp))
            m_entry["diary_scores"] = {
                iid: {
                    "score": round(sum(x[0] for x in pairs) / len(pairs), 4),
                    "explanations": [x[1] for x in pairs],
                }
                for iid, pairs in by_item_diary.items()
            }

    # Aggregate per golden item (an item like R20 across 2 windows takes max score across its windows)
    item_windows: dict[str, list[dict[str, Any]]] = {}
    for w in window_results:
        for iid in w["item_ids"]:
            item_windows.setdefault(iid, []).append(w)

    item_diagnostics: list[dict[str, Any]] = []
    summary_counts: dict[str, int] = {
        "A1_FLASH_CROPPED_RECOVERABLE": 0,
        "A2_PRO_ESCALATION_RECOVERABLE": 0,
        "B_VISUAL_CEILING": 0,
    }
    for iid, wins in item_windows.items():
        g_item = golden_by_id[iid]
        role = wins[0]["role"]
        flash_sop = max(w["models"]["flash"]["sop_scores"][iid]["score"] for w in wins)
        flash_diary = max(w["models"]["flash"]["diary_scores"][iid]["score"] for w in wins)
        pro_sop = max(w["models"]["pro"]["sop_scores"][iid]["score"] for w in wins)
        pro_diary = max(w["models"]["pro"]["diary_scores"][iid]["score"] for w in wins)
        diag_code, diag_zh = classify_item_visibility(
            flash_sop_score=flash_sop,
            flash_diary_score=flash_diary,
            pro_sop_score=pro_sop,
            pro_diary_score=pro_diary,
        )
        if role != "positive_control":
            summary_counts[diag_code] = summary_counts.get(diag_code, 0) + 1
        item_diagnostics.append(
            {
                "item_id": iid,
                "role": role,
                "split": g_item["split"],
                "finding_verbatim": g_item["finding_verbatim"],
                "windows_str": ", ".join(
                    f"{w['probe_id']}({w['start_sec']:.0f}-{w['end_sec']:.0f}s)" for w in wins
                ),
                "flash_sop_score": flash_sop,
                "flash_diary_score": flash_diary,
                "pro_sop_score": pro_sop,
                "pro_diary_score": pro_diary,
                "diagnosis_code": diag_code,
                "diagnosis_zh": diag_zh,
            }
        )

    out_dir = Path(args.out_dir) / probe_run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "probe_run_id": probe_run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "flash_model": flash_model,
        "pro_model": pro_model,
        "judge_model": judge_model,
        "summary_counts": summary_counts,
        "item_diagnostics": item_diagnostics,
        "window_results": window_results,
    }
    (out_dir / "probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_text = render_probe_markdown(report)
    (out_dir / "probe_report.md").write_text(md_text, encoding="utf-8")

    if not args.skip_gcs_sync and cfg.staging_bucket:
        await asyncio.to_thread(
            upload_directory_to_gcs,
            cfg.staging_bucket,
            cfg.gcp_project,
            out_dir,
            f"eval/visibility_probe/{probe_run_id}/results",
        )

    print(md_text)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-run-id", default="", help="Unique ID for this probe run (enables resume)")
    parser.add_argument("--rounds-dir", type=Path, default=DEFAULT_ROUNDS_DIR)
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_PROBE_OUT_DIR)
    parser.add_argument("--flash-model", default="", help="Override Flash model (default: Tab 0 active)")
    parser.add_argument("--pro-model", default="", help="Override Pro model (default: newest Pro live)")
    parser.add_argument("--judge-model", default="", help="Override Judge model (default: newest Pro live)")
    parser.add_argument("--judge-passes", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--probe-id", dest="probe_ids", action="append", default=None)
    parser.add_argument("--ckpt-local-dir", type=Path, default=None)
    parser.add_argument("--skip-gcs-sync", action="store_true")
    args = parser.parse_args(argv)
    asyncio.run(run_probe_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
