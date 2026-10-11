"""Higgsfield video generation via the official `higgsfield` CLI.

Routes to Higgsfield's video lineup (Seedance 2.0 default, plus Kling, Veo, Wan,
Grok, Minimax, and Cinema Studio) through one wrapper. The CLI handles auth,
auto-uploads local reference images/audio, polls to completion, and returns the
result URL, which we download to the project's asset path.

Layer 3 knowledge: `.agents/skills/higgsfield-generate/` (model catalog, media
inputs) and `.agents/skills/seedance-2-0/` (Seedance prompt craft).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tools._higgsfield_cli import INSTALL_INSTRUCTIONS, is_installed, run_generate
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


# Single source of truth for the default model. Referenced by both the input
# schema and every code path that reads `model`, so estimate_cost / estimate_runtime
# / execute can never silently diverge from the advertised default again.
_DEFAULT_MODEL = "seedance_2_0"

# Display model -> CLI job_set_type. Defaults favor Seedance 2.0 per the skill.
# On Higgsfield, Seedance 2.0 "fast" is --mode fast on seedance_2_0 (not a separate JST).
MODELS = {
    "seedance_2_0": {"name": "Seedance 2.0", "jst": "seedance_2_0", "i2v": True, "audio_ref": True, "cost5": 0.80, "supports_mode": True},
    "seedance_2_0_fast": {"name": "Seedance 2.0 Fast", "jst": "seedance_2_0", "i2v": True, "audio_ref": True, "cost5": 0.50, "supports_mode": True, "default_mode": "fast"},
    "kling3_0": {"name": "Kling 3.0", "i2v": True, "audio_ref": False, "cost5": 0.20},
    "kling3_0_turbo": {"name": "Kling 3.0 Turbo", "i2v": True, "audio_ref": False, "cost5": 0.12},
    "seedance_1_5_pro": {"name": "Seedance 1.5 Pro", "i2v": True, "audio_ref": False, "cost5": 0.30},
    "veo3_1": {"name": "Google Veo 3.1", "i2v": True, "audio_ref": False, "cost5": 0.50},
    "minimax_hailuo": {"name": "Minimax Hailuo", "i2v": True, "audio_ref": False, "cost5": 0.10},
    "wan2_7": {"name": "Wan 2.7", "i2v": True, "audio_ref": False, "cost5": 0.12},
    "grok_video_v15": {"name": "Grok Video 1.5", "i2v": True, "audio_ref": False, "cost5": 0.20},
    "cinema_studio_video_3_0": {"name": "Cinema Studio Video 3.0", "i2v": True, "audio_ref": False, "cost5": 0.60},
}


class HiggsFieldVideo(BaseTool):
    name = "higgsfield_video"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "higgsfield"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["cmd:higgsfield"]
    install_instructions = INSTALL_INSTRUCTIONS
    agent_skills = ["higgsfield-generate", "seedance-2-0", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "start_frame": True,
        "end_frame": True,
        "audio_reference": True,
        "character_consistency": True,
        "native_audio": True,
        "cinematic_quality": True,
        "camera_direction": True,
        "multi_shot": True,
    }
    best_for = [
        "premium video gen on Higgsfield (Seedance 2.0 default — SOTA multi-shot motion)",
        "cinematic trailers, teasers, and high-fidelity clips with native synchronized audio",
        "character-consistent video (Soul ID + Seedance 2.0 identity consistency)",
        "image-to-video with start/end frame control",
        "single-API access to Seedance, Kling, Veo, Wan, Grok, Minimax, Cinema Studio",
    ]
    not_good_for = ["offline generation", "branded ads (use higgsfield_marketing)", "budget projects without a subscription"]
    fallback_tools = ["seedance_video", "seedance_replicate", "kling_video", "veo_video", "minimax_video"]
    quality_score = 0.9

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video"],
                "default": "text_to_video",
            },
            "model": {
                "type": "string",
                "enum": list(MODELS.keys()),
                "default": _DEFAULT_MODEL,
                "description": "CLI job_set_type. Defaults to Seedance 2.0 (preferred premium).",
            },
            "duration": {
                "type": "integer",
                "default": 5,
                "description": "Seconds. Seedance 2.0 accepts 4-15; ranges vary by model.",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
                "default": "16:9",
            },
            "resolution": {"type": "string", "description": "e.g. 480p/720p/1080p (model-specific)."},
            "mode": {
                "type": "string",
                "enum": ["std", "fast"],
                "description": "Seedance 2.0 only: std (default) or fast (cheaper iteration).",
            },
            "start_image": {"type": "string", "description": "Local path or UUID — first frame for image-to-video."},
            "end_image": {"type": "string", "description": "Local path or UUID — last frame for transitions."},
            "image": {"type": "string", "description": "Local path or UUID — generic reference image."},
            "audio": {"type": "string", "description": "Local path or UUID — reference audio (Seedance 2.0 lipsync/soundtrack)."},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model", "operation", "duration", "aspect_ratio"]
    side_effects = ["writes video file to output_path", "calls the Higgsfield CLI"]
    user_visible_verification = ["Watch generated clip for motion coherence and visual quality"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if is_installed() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        model = inputs.get("model", _DEFAULT_MODEL)
        duration = int(inputs.get("duration", 5) or 5)
        base = MODELS.get(model, {}).get("cost5", 0.20)
        return round(base * (duration / 5), 2)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 120.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not is_installed():
            return ToolResult(success=False, error="Higgsfield CLI not found. " + INSTALL_INSTRUCTIONS)

        model = inputs.get("model", _DEFAULT_MODEL)
        operation = inputs.get("operation", "text_to_video")
        model_meta = MODELS.get(model, {})
        jst = model_meta.get("jst", model)

        # Media flags. For image-to-video prefer start_image; fall back to generic image.
        media: list[tuple[str, str]] = []
        if inputs.get("start_image"):
            media.append(("--start-image", inputs["start_image"]))
        elif operation == "image_to_video" and inputs.get("image"):
            media.append(("--start-image", inputs["image"]))
        elif inputs.get("image"):
            media.append(("--image", inputs["image"]))
        if inputs.get("end_image"):
            media.append(("--end-image", inputs["end_image"]))
        if inputs.get("audio") and model_meta.get("audio_ref"):
            media.append(("--audio", inputs["audio"]))

        if operation == "image_to_video" and not media:
            return ToolResult(
                success=False,
                error="image_to_video requires a start_image (or image) reference.",
            )

        params: dict[str, Any] = {}
        if inputs.get("duration"):
            params["duration"] = int(inputs["duration"])
        if inputs.get("aspect_ratio"):
            params["aspect_ratio"] = inputs["aspect_ratio"]
        if inputs.get("resolution"):
            params["resolution"] = inputs["resolution"]
        if model_meta.get("supports_mode"):
            params["mode"] = inputs.get("mode") or model_meta.get("default_mode", "std")
            # Seedance clips for compose are muted; skip native audio generation.
            params["generate_audio"] = False

        output_path = inputs.get("output_path", "higgsfield_video.mp4")
        start = time.time()
        result = run_generate(
            jst,
            prompt=inputs["prompt"],
            media=media or None,
            params=params,
            output_path=output_path,
            output_ext=".mp4",
            wait_timeout="30m",
        )

        if not result.success:
            return ToolResult(success=False, error=result.error)

        from tools.video._shared import probe_output

        out = Path(result.output_path or output_path)
        probed = probe_output(out) if out.exists() else {}
        return ToolResult(
            success=True,
            data={
                "provider": "higgsfield",
                "model": result.model or model,
                "prompt": inputs["prompt"],
                "operation": operation,
                "aspect_ratio": inputs.get("aspect_ratio", "16:9"),
                "job_id": result.job_id,
                "result_url": result.result_url,
                "adjustments": result.adjustments,
                "output": str(out),
                "output_path": str(out),
                "format": "mp4",
                **probed,
            },
            artifacts=[str(out)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=result.model or model,
        )
