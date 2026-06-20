"""Higgsfield music generation (Sonilo) via the official `higgsfield` CLI.

Text-to-music: backing tracks, instrumental beds, jingles, and musical moods.
Prompt-only — no media inputs. Auto-grouped under `capability="music_generation"`.

Layer 3 knowledge: `.agents/skills/higgsfield-generate/` (audio section).
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


class HiggsFieldMusic(BaseTool):
    name = "higgsfield_music"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "music_generation"
    provider = "higgsfield"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["cmd:higgsfield"]
    install_instructions = INSTALL_INSTRUCTIONS
    agent_skills = ["higgsfield-generate"]

    capabilities = ["generate_background_music", "generate_instrumental", "text_to_music"]
    supports = {"instrumental": True, "style_control": True, "duration_control": True}
    best_for = [
        "instrumental background beds and musical moods from a text prompt",
        "jingles and short branded music cues",
        "soundtrack generation when no library track fits",
    ]
    not_good_for = [
        "non-speech sound effects (use higgsfield_sfx / Mirelo)",
        "speech / narration (use a TTS tool)",
        "offline generation",
    ]
    fallback_tools = ["suno_music", "music_gen"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "description": "Description of the desired music."},
            "duration": {"type": "integer", "default": 12, "description": "Track length in seconds."},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=100, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "duration"]
    side_effects = ["writes audio file to output_path", "calls the Higgsfield CLI"]
    user_visible_verification = ["Listen for mood, genre accuracy, and loop quality"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if is_installed() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.05

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 60.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not is_installed():
            return ToolResult(success=False, error="Higgsfield CLI not found. " + INSTALL_INSTRUCTIONS)

        output_path = inputs.get("output_path", "higgsfield_music.mp3")
        start = time.time()
        result = run_generate(
            "sonilo_music",
            prompt=inputs["prompt"],
            params={"duration": int(inputs.get("duration", 12) or 12)},
            output_path=output_path,
            output_ext=".mp3",
            wait_timeout="10m",
        )

        if not result.success:
            return ToolResult(success=False, error=result.error)

        out = Path(result.output_path or output_path)
        return ToolResult(
            success=True,
            data={
                "provider": "higgsfield",
                "model": "sonilo_music",
                "prompt": inputs["prompt"],
                "duration_requested": int(inputs.get("duration", 12) or 12),
                "job_id": result.job_id,
                "result_url": result.result_url,
                "output": str(out),
                "output_path": str(out),
                "format": out.suffix.lstrip(".") or "mp3",
            },
            artifacts=[str(out)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model="sonilo_music",
        )
