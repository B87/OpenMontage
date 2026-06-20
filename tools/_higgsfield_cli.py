"""Shared client for the Higgsfield CLI.

All Higgsfield provider tools (video, image, music, SFX, 3D, marketing, virality)
delegate to this helper instead of talking to an HTTP API directly. The CLI is the
authoritative Higgsfield surface documented in `.agents/skills/higgsfield-*`: it
handles auth/session, auto-uploads local file paths, discovers model params, polls
to completion (`--wait`), and emits structured output (`--json`).

This module centralizes:
  - the `cmd:higgsfield` dependency + install/auth guidance,
  - command assembly for `higgsfield generate create <jst> ... --wait --json`,
  - JSON result parsing (job id, model, result URL, server-side adjustments),
  - downloading the result asset to a project path,
  - CLI error mapping into clean tool errors.

Keeping it in one place means a CLI shape change is fixed once, not in eight tools.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


BINARY = "higgsfield"

INSTALL_INSTRUCTIONS = (
    "Install the Higgsfield CLI and authenticate:\n"
    "  curl -fsSL https://raw.githubusercontent.com/higgsfield-ai/cli/main/install.sh | sh\n"
    "  higgsfield auth login   (interactive — opens a browser)\n"
    "Verify with: higgsfield account status"
)

# Media flag names the CLI exposes. Each accepts a local path (auto-uploaded) or a UUID.
MEDIA_FLAGS = {
    "image": "--image",
    "start_image": "--start-image",
    "end_image": "--end-image",
    "video": "--video",
    "audio": "--audio",
}

# Extensions worth treating as a downloadable result, by capability output kind.
_MEDIA_EXT_PATTERN = re.compile(
    r"\.(mp4|mov|webm|png|jpe?g|webp|gif|mp3|wav|m4a|ogg|glb|gltf|obj|fbx|usdz)(\?|$)",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


@dataclass
class HiggsfieldResult:
    """Normalized outcome of a `generate create` invocation."""

    success: bool
    job_id: Optional[str] = None
    model: Optional[str] = None
    result_url: Optional[str] = None
    output_path: Optional[str] = None
    adjustments: dict[str, Any] = field(default_factory=dict)
    all_urls: list[str] = field(default_factory=list)
    raw: Any = None
    stdout: str = ""
    error: Optional[str] = None


def is_installed() -> bool:
    """True if the `higgsfield` binary is on PATH."""
    return shutil.which(BINARY) is not None


def is_authenticated(timeout: int = 20) -> bool:
    """Best-effort auth probe via `higgsfield account status` (non-zero == not logged in)."""
    if not is_installed():
        return False
    try:
        proc = subprocess.run(
            [BINARY, "account", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return False
    return proc.returncode == 0


def _map_error(stdout: str, stderr: str, returncode: int) -> str:
    """Turn raw CLI stderr/stdout into a single actionable message."""
    blob = f"{stderr}\n{stdout}".strip()
    low = blob.lower()
    if "session expired" in low or "not authenticated" in low or "auth login" in low:
        return "Higgsfield session expired. Run `higgsfield auth login` to re-authenticate."
    if "missing required params" in low:
        return f"Higgsfield rejected the request — {blob.splitlines()[0] if blob else 'missing required params'}"
    if "unknown model" in low:
        return f"Unknown Higgsfield model. Run `higgsfield model list` to see valid ids. ({blob.splitlines()[0]})"
    if "unknown params" in low or "invalid values" in low:
        return f"Higgsfield schema mismatch: {blob.splitlines()[0] if blob else blob}"
    first = blob.splitlines()[0] if blob else f"exit code {returncode}"
    return f"Higgsfield CLI failed: {first}"


def _parse_json(stdout: str) -> Any:
    """Parse `--json` output, tolerating leading log lines before the JSON token."""
    stdout = stdout.strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    # Locate the first JSON token and try from there.
    for token in ("[", "{"):
        idx = stdout.find(token)
        if idx != -1:
            try:
                return json.loads(stdout[idx:])
            except json.JSONDecodeError:
                continue
    return None


def _first_job(parsed: Any) -> dict[str, Any]:
    """`--wait --json` returns a job-object array; normalize to the first object."""
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                return item
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _walk_urls(obj: Any) -> list[str]:
    """Recursively collect every http(s) URL string found in a parsed JSON blob."""
    found: list[str] = []
    if isinstance(obj, str):
        found.extend(_URL_PATTERN.findall(obj))
    elif isinstance(obj, dict):
        for value in obj.values():
            found.extend(_walk_urls(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_urls(item))
    # De-dup, preserve order.
    seen: set[str] = set()
    ordered: list[str] = []
    for url in found:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _pick_result_url(urls: list[str], output_ext: str) -> Optional[str]:
    """Prefer a URL whose extension matches the expected output, else the first media URL."""
    ext = output_ext.lstrip(".").lower()
    # Exact-extension match first.
    for url in urls:
        m = _MEDIA_EXT_PATTERN.search(url)
        if m and m.group(1).lower().rstrip("?") == ext:
            return url
    # Any media-looking URL.
    for url in urls:
        if _MEDIA_EXT_PATTERN.search(url):
            return url
    return None


def _find_job_id(job: dict[str, Any]) -> Optional[str]:
    for key in ("id", "job_id", "jobId", "job_set_id", "jobSetId"):
        val = job.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _find_model(job: dict[str, Any], fallback: str) -> str:
    for key in ("job_set_type", "jobSetType", "model", "type"):
        val = job.get(key)
        if isinstance(val, str) and val:
            return val
    return fallback


def build_command(
    job_set_type: str,
    *,
    prompt: Optional[str] = None,
    media: Optional[list[tuple[str, str]]] = None,
    params: Optional[dict[str, Any]] = None,
    extra_flags: Optional[list[str]] = None,
    wait_timeout: str = "20m",
    wait_interval: str = "5s",
) -> list[str]:
    """Assemble the `higgsfield generate create ... --wait --json` argv."""
    cmd: list[str] = [BINARY, "generate", "create", job_set_type]
    if prompt:
        cmd += ["--prompt", prompt]
    for flag, value in media or []:
        cmd += [flag, str(value)]
    for key, value in (params or {}).items():
        if value is None:
            continue
        flag = key if key.startswith("-") else f"--{key}"
        if isinstance(value, bool):
            cmd += [flag, "true" if value else "false"]
        else:
            cmd += [flag, str(value)]
    if extra_flags:
        cmd += list(extra_flags)
    cmd += ["--wait", "--wait-timeout", wait_timeout, "--wait-interval", wait_interval, "--json"]
    return cmd


def run_generate(
    job_set_type: str,
    *,
    prompt: Optional[str] = None,
    media: Optional[list[tuple[str, str]]] = None,
    params: Optional[dict[str, Any]] = None,
    extra_flags: Optional[list[str]] = None,
    output_path: Optional[str | Path] = None,
    output_ext: str = ".mp4",
    download: bool = True,
    wait_timeout: str = "20m",
    wait_interval: str = "5s",
    timeout_seconds: int = 1800,
) -> HiggsfieldResult:
    """Submit a Higgsfield generation, block until done, and (optionally) download the asset.

    `media` is a list of (flag, value) pairs, e.g. [("--start-image", "ref.png")].
    `params` maps schema flags to values (bools rendered as true/false). `download`
    pulls the result URL into `output_path`; set False for text-out jobs like virality.
    """
    if not is_installed():
        return HiggsfieldResult(
            success=False,
            error="Higgsfield CLI not found on PATH. " + INSTALL_INSTRUCTIONS,
        )

    cmd = build_command(
        job_set_type,
        prompt=prompt,
        media=media,
        params=params,
        extra_flags=extra_flags,
        wait_timeout=wait_timeout,
        wait_interval=wait_interval,
    )

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return HiggsfieldResult(
            success=False,
            error=f"Higgsfield CLI timed out after {timeout_seconds}s for {job_set_type}.",
        )
    except Exception as exc:  # pragma: no cover - defensive
        return HiggsfieldResult(success=False, error=f"Failed to invoke Higgsfield CLI: {exc}")

    if proc.returncode != 0:
        return HiggsfieldResult(
            success=False,
            stdout=proc.stdout,
            error=_map_error(proc.stdout, proc.stderr, proc.returncode),
        )

    parsed = _parse_json(proc.stdout)
    job = _first_job(parsed)
    urls = _walk_urls(parsed if parsed is not None else proc.stdout)
    adjustments = job.get("adjustments") if isinstance(job.get("adjustments"), dict) else {}

    result = HiggsfieldResult(
        success=True,
        job_id=_find_job_id(job),
        model=_find_model(job, job_set_type),
        all_urls=urls,
        adjustments=adjustments or {},
        raw=parsed,
        stdout=proc.stdout,
    )
    result.result_url = _pick_result_url(urls, output_ext)

    if download and result.result_url and output_path is not None:
        try:
            result.output_path = _download(result.result_url, output_path)
        except Exception as exc:
            return HiggsfieldResult(
                success=False,
                job_id=result.job_id,
                model=result.model,
                result_url=result.result_url,
                all_urls=urls,
                raw=parsed,
                stdout=proc.stdout,
                error=f"Generation succeeded but download failed: {exc}",
            )
    elif download and output_path is not None and not result.result_url:
        return HiggsfieldResult(
            success=False,
            job_id=result.job_id,
            model=result.model,
            all_urls=urls,
            raw=parsed,
            stdout=proc.stdout,
            error="Higgsfield job completed but no downloadable result URL was found in the response.",
        )

    return result


def _download(url: str, output_path: str | Path) -> str:
    """Download a result URL to output_path, creating parent dirs. Returns the path string."""
    import requests

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=300)
    response.raise_for_status()
    out.write_bytes(response.content)
    return str(out)
