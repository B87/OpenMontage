"""Higgsfield Virality Predictor (brain_activity) via the official `higgsfield` CLI.

Video-in / text-out: scores a finished clip's hook, attention, retention, and
virality potential. This is video *analysis*, not media generation — `capability=
"analysis"`. Returns the parsed report plus an Open-report URL; it never downloads
the raw `.glb`/`.bin` render artifacts the job carries.

Layer 3 knowledge: `.agents/skills/higgsfield-generate/` (Virality Predictor section).
"""

from __future__ import annotations

import time
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


# Keys we surface from the report when present (best-effort; schema is server-defined).
_SCORE_KEYS = (
    "overall_score", "score", "hook_score", "peak_second", "sustain", "sustain_score",
    "attention", "visual", "auditory", "language", "default_mode",
    "strongest_region", "weakest_region",
)


class HiggsFieldVirality(BaseTool):
    name = "higgsfield_virality"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "analysis"
    provider = "higgsfield"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["cmd:higgsfield"]
    install_instructions = INSTALL_INSTRUCTIONS
    agent_skills = ["higgsfield-generate"]

    capabilities = ["video_virality_scoring", "hook_analysis", "attention_analysis"]
    supports = {"hook_score": True, "retention": True, "attention_proxy": True, "report_url": True}
    best_for = [
        "scoring a finished ad/clip's hook, attention, retention, and virality potential",
        "objective creative testing before launch",
    ]
    not_good_for = [
        "generating media (this returns a text report)",
        "transcription or scene detection (use the analysis tools for that)",
    ]

    input_schema = {
        "type": "object",
        "required": ["video"],
        "properties": {
            "video": {"type": "string", "description": "Local video path or UUID to analyze."},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["video"]
    side_effects = ["calls the Higgsfield CLI (uploads the video for analysis)"]
    user_visible_verification = ["Open the report URL; sanity-check scores against the clip"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if is_installed() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.10

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 120.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not is_installed():
            return ToolResult(success=False, error="Higgsfield CLI not found. " + INSTALL_INSTRUCTIONS)

        if not inputs.get("video"):
            return ToolResult(success=False, error="brain_activity requires a --video (path or UUID).")

        start = time.time()
        result = run_generate(
            "brain_activity",
            media=[("--video", inputs["video"])],
            download=False,  # text-out: no media asset to download
            wait_timeout="20m",
        )

        if not result.success:
            return ToolResult(success=False, error=result.error)

        scores = self._extract_scores(result.raw)
        report_url = self._find_report_url(result.all_urls)

        return ToolResult(
            success=True,
            data={
                "provider": "higgsfield",
                "model": "brain_activity",
                "analysis": "virality_predictor",
                "job_id": result.job_id,
                "scores": scores,
                "report_url": report_url,
                "raw": result.raw,
            },
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model="brain_activity",
        )

    @staticmethod
    def _find_report_url(urls: list[str]) -> str | None:
        for url in urls:
            if "virality-predictor" in url or "resultJobId" in url:
                return url
        return None

    def _extract_scores(self, obj: Any, depth: int = 0) -> dict[str, Any]:
        """Best-effort pull of known score keys from the report JSON."""
        found: dict[str, Any] = {}
        if depth > 6:
            return found
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in _SCORE_KEYS and isinstance(value, (int, float, str)):
                    found.setdefault(key, value)
                else:
                    found.update({k: v for k, v in self._extract_scores(value, depth + 1).items() if k not in found})
        elif isinstance(obj, list):
            for item in obj:
                found.update({k: v for k, v in self._extract_scores(item, depth + 1).items() if k not in found})
        return found
