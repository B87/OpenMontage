"""Higgsfield sound-effect generation (Mirelo) via the official `higgsfield` CLI.

Text-to-audio for non-speech: sound effects, foley, ambience, impacts, transitions,
and environmental sound. Prompt-only — no media inputs.

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


class HiggsFieldSFX(BaseTool):
    name = "higgsfield_sfx"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "sound_effects"
    provider = "higgsfield"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["cmd:higgsfield"]
    install_instructions = INSTALL_INSTRUCTIONS
    agent_skills = ["higgsfield-generate"]

    capabilities = ["generate_sound_effect", "generate_ambience", "text_to_audio"]
    supports = {"foley": True, "ambience": True, "impacts": True, "duration_control": True}
    best_for = [
        "one-off sound effects and foley from a text description",
        "ambient beds, impacts, whooshes, and transition stingers",
        "environmental audio textures for edits",
    ]
    not_good_for = [
        "music / instrumental beds (use higgsfield_music / Sonilo)",
        "speech / narration (use a TTS tool)",
        "offline generation",
    ]
    fallback_tools = ["music_gen"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "description": "Description of the sound, e.g. 'glass breaking in a large hall'."},
            "duration": {"type": "integer", "default": 4, "description": "Clip length in seconds."},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "duration"]
    side_effects = ["writes audio file to output_path", "calls the Higgsfield CLI"]
    user_visible_verification = ["Listen for accuracy to the described sound and clean tails"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if is_installed() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.03

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 40.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not is_installed():
            return ToolResult(success=False, error="Higgsfield CLI not found. " + INSTALL_INSTRUCTIONS)

        output_path = inputs.get("output_path", "higgsfield_sfx.mp3")
        start = time.time()
        result = run_generate(
            "mirelo_text_to_audio",
            prompt=inputs["prompt"],
            params={"duration": int(inputs.get("duration", 4) or 4)},
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
                "model": "mirelo_text_to_audio",
                "prompt": inputs["prompt"],
                "duration_requested": int(inputs.get("duration", 4) or 4),
                "job_id": result.job_id,
                "result_url": result.result_url,
                "output": str(out),
                "output_path": str(out),
                "format": out.suffix.lstrip(".") or "mp3",
            },
            artifacts=[str(out)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model="mirelo_text_to_audio",
        )
