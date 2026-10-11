"""YouTube channel analytics (YouTube Data API v3 + YouTube Analytics API v2).

Read-only performance data for channels you own: channel totals, per-video
stats, daily views, traffic sources and audience-retention curves.

Auth is OAuth for an installed app, one token per channel, stored OUTSIDE the
repo (default ``~/.config/openmontage/youtube/``, override with
``OPENMONTAGE_YOUTUBE_DIR``). One-time setup per machine and channel:

    python -m tools.analysis.youtube_analytics auth --channel <name>

See ``SETUP`` below for the Google Cloud steps that precede it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import requests

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolStability,
    ToolStatus,
    ToolTier,
)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
DATA_API = "https://www.googleapis.com/youtube/v3"
ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"
DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

VIDEO_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "likes,comments,shares,subscribersGained"
)
CHANNEL_METRICS = "views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost"
# Thumbnail impressions/CTR are not exposed for every channel; queried best-effort.
IMPRESSION_METRICS = "videoThumbnailImpressions,videoThumbnailImpressionsClickRate"

SETUP = """\
One-time Google Cloud setup (per machine):
  1. https://console.cloud.google.com -> create a project (e.g. "openmontage-analytics").
  2. APIs & Services -> Library -> enable "YouTube Data API v3" and "YouTube Analytics API".
  3. OAuth consent screen -> External -> add your Google account as a test user.
     (Testing-mode refresh tokens expire after 7 days; publish the app to
     "In production" to keep them. Google shows an "unverified app" warning
     you can accept for your own use.)
  4. Credentials -> Create credentials -> OAuth client ID -> "Desktop app" ->
     download the JSON to {client_secret}
  5. python -m tools.analysis.youtube_analytics auth --channel <name>
     Sign in with the Google account (or Brand Account) that owns the channel.
"""


def config_dir() -> Path:
    return Path(os.environ.get("OPENMONTAGE_YOUTUBE_DIR", Path.home() / ".config" / "openmontage" / "youtube"))


def client_secret_path() -> Path:
    return config_dir() / "client_secret.json"


def token_path(channel: str) -> Path:
    return config_dir() / "tokens" / f"{channel}.json"


def _load_client() -> dict[str, str]:
    path = client_secret_path()
    if not path.exists():
        raise RuntimeError("YouTube OAuth client not found.\n" + SETUP.format(client_secret=path))
    raw = json.loads(path.read_text())
    client = raw.get("installed") or raw.get("web") or raw
    return {
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "auth_uri": client.get("auth_uri", DEFAULT_AUTH_URI),
        "token_uri": client.get("token_uri", DEFAULT_TOKEN_URI),
    }


def _save_token(channel: str, token: dict[str, Any]) -> None:
    path = token_path(channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, indent=2))
    path.chmod(0o600)


def _http_post(url: str, data: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(url, data=data, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"POST {url} failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _http_get(url: str, params: dict[str, Any], access_token: str) -> dict[str, Any]:
    resp = requests.get(url, params=params, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"GET {url} failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def authorize(channel: str, open_browser: bool = True) -> Path:
    """Run the installed-app OAuth flow (loopback + PKCE) and store the token."""
    import webbrowser

    client = _load_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>Authorized. You can close this tab.</h3>")

        def log_message(self, *args):  # silence the default stderr logging
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    auth_url = client["auth_uri"] + "?" + urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    print(f"Open this URL to authorize channel '{channel}':\n{auth_url}\n", flush=True)
    if open_browser:
        webbrowser.open(auth_url)
    server.timeout = 300
    server.handle_request()
    server.server_close()

    if result.get("state") != state:
        raise RuntimeError("OAuth state mismatch; aborting.")
    if "code" not in result:
        raise RuntimeError(f"Authorization failed: {result.get('error', 'no code returned')}")

    token = _http_post(
        client["token_uri"],
        {
            "code": result["code"],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    if "refresh_token" not in token:
        raise RuntimeError("Google did not return a refresh token; revoke the app's access and retry.")
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    _save_token(channel, token)
    return token_path(channel)


def access_token(channel: str) -> str:
    """Return a valid access token for the channel, refreshing it if needed."""
    path = token_path(channel)
    if not path.exists():
        raise RuntimeError(
            f"No YouTube token for channel '{channel}'. Run: "
            f"python -m tools.analysis.youtube_analytics auth --channel {channel}"
        )
    token = json.loads(path.read_text())
    if token.get("access_token") and token.get("expires_at", 0) > time.time() + 60:
        return token["access_token"]
    client = _load_client()
    fresh = _http_post(
        client["token_uri"],
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        },
    )
    token["access_token"] = fresh["access_token"]
    token["expires_at"] = time.time() + int(fresh.get("expires_in", 3600))
    _save_token(channel, token)
    return token["access_token"]


def _rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn an Analytics API report into a list of dicts keyed by column name."""
    names = [h["name"] for h in report.get("columnHeaders", [])]
    return [dict(zip(names, row)) for row in report.get("rows", []) or []]


class YouTubeAnalytics(BaseTool):
    name = "youtube_analytics"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "analysis"
    provider = "youtube"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC

    dependencies = ["python:requests"]
    install_instructions = SETUP.format(client_secret="~/.config/openmontage/youtube/client_secret.json")
    agent_skills = []

    capabilities = ["channel_analytics", "video_analytics", "audience_retention", "traffic_sources"]
    best_for = [
        "performance review of published videos on channels you own",
        "retention curves to find where viewers drop off",
        "traffic sources (search vs. browse vs. suggested vs. Shorts feed)",
    ]
    not_good_for = ["channels you don't own (use public stats via video_downloader metadata)"]

    input_schema = {
        "type": "object",
        "required": ["channel", "operation"],
        "properties": {
            "channel": {"type": "string", "description": "Channel key, e.g. the folder name under channels/."},
            "operation": {
                "type": "string",
                "enum": ["channel_overview", "videos", "video_report", "custom"],
                "description": (
                    "channel_overview: channel stats + totals for the period. "
                    "videos: every upload with public stats + period analytics. "
                    "video_report: one video's totals, daily views, traffic sources and retention. "
                    "custom: raw Analytics API query (metrics/dimensions/filters/sort)."
                ),
            },
            "video_id": {"type": "string", "description": "Required for video_report."},
            "start_date": {"type": "string", "description": "YYYY-MM-DD. Default: 28 days ago."},
            "end_date": {"type": "string", "description": "YYYY-MM-DD. Default: today."},
            "metrics": {"type": "string", "description": "custom only: comma-separated metrics."},
            "dimensions": {"type": "string", "description": "custom only."},
            "filters": {"type": "string", "description": "custom only, e.g. video==abc123."},
            "sort": {"type": "string", "description": "custom only, e.g. -views."},
            "max_results": {"type": "integer", "default": 50},
            "output_path": {"type": "string", "description": "Optional JSON file to write the result to."},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=5, network_required=True)
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["timeout", "rate_limit"])
    idempotency_key_fields = ["channel", "operation", "video_id", "start_date", "end_date"]
    side_effects = ["reads YouTube Data/Analytics APIs", "writes JSON to output_path if given"]
    user_visible_verification = ["Spot-check numbers against YouTube Studio"]

    def get_status(self) -> ToolStatus:
        if not client_secret_path().exists():
            return ToolStatus.UNAVAILABLE
        tokens = config_dir() / "tokens"
        return ToolStatus.AVAILABLE if tokens.exists() and any(tokens.glob("*.json")) else ToolStatus.DEGRADED

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    # ---- operations -------------------------------------------------------

    def _report(self, token: str, start: str, end: str, metrics: str, **extra: Any) -> list[dict[str, Any]]:
        params = {"ids": "channel==MINE", "startDate": start, "endDate": end, "metrics": metrics}
        params.update({k: v for k, v in extra.items() if v})
        return _rows(_http_get(ANALYTICS_API, params, token))

    def _try_report(self, *args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
        try:
            return self._report(*args, **kwargs), None
        except RuntimeError as exc:
            return None, str(exc)

    def _channel(self, token: str) -> dict[str, Any]:
        items = _http_get(f"{DATA_API}/channels", {"part": "snippet,statistics,contentDetails", "mine": "true"}, token)
        if not items.get("items"):
            raise RuntimeError("The authorized account has no YouTube channel.")
        return items["items"][0]

    def _uploads(self, token: str, channel: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        playlist = channel["contentDetails"]["relatedPlaylists"]["uploads"]
        ids: list[str] = []
        page = None
        while len(ids) < limit:
            params = {"part": "contentDetails", "playlistId": playlist, "maxResults": 50}
            if page:
                params["pageToken"] = page
            data = _http_get(f"{DATA_API}/playlistItems", params, token)
            ids += [it["contentDetails"]["videoId"] for it in data.get("items", [])]
            page = data.get("nextPageToken")
            if not page:
                break
        videos: list[dict[str, Any]] = []
        for i in range(0, min(len(ids), limit), 50):
            data = _http_get(
                f"{DATA_API}/videos",
                {"part": "snippet,statistics,contentDetails", "id": ",".join(ids[i : i + 50])},
                token,
            )
            for v in data.get("items", []):
                videos.append(
                    {
                        "video_id": v["id"],
                        "title": v["snippet"]["title"],
                        "published_at": v["snippet"]["publishedAt"],
                        "duration": v["contentDetails"]["duration"],
                        "lifetime": {k: int(n) for k, n in v.get("statistics", {}).items()},
                    }
                )
        return videos

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start_time = time.time()
        channel_key = inputs["channel"]
        op = inputs["operation"]
        end = inputs.get("end_date") or date.today().isoformat()
        start = inputs.get("start_date") or (date.fromisoformat(end) - timedelta(days=28)).isoformat()
        limit = int(inputs.get("max_results", 50))

        try:
            token = access_token(channel_key)
            notes: list[str] = []

            if op == "channel_overview":
                ch = self._channel(token)
                totals = self._report(token, start, end, CHANNEL_METRICS)
                data = {
                    "channel_id": ch["id"],
                    "title": ch["snippet"]["title"],
                    "lifetime": {k: int(v) for k, v in ch["statistics"].items() if v.isdigit()},
                    "period_totals": totals[0] if totals else {},
                }

            elif op == "videos":
                ch = self._channel(token)
                videos = self._uploads(token, ch, limit)
                per_video = self._report(
                    token, start, end, VIDEO_METRICS, dimensions="video", sort="-views", maxResults=200
                )
                by_id = {r.pop("video"): r for r in per_video}
                impressions, err = self._try_report(token, start, end, IMPRESSION_METRICS, dimensions="video", maxResults=200)
                imp_by_id = {r.pop("video"): r for r in impressions or []}
                if err:
                    notes.append(f"Impressions/CTR unavailable via API: {err[:160]}")
                for v in videos:
                    v["period"] = by_id.get(v["video_id"], {})
                    v["period"].update(imp_by_id.get(v["video_id"], {}))
                data = {"channel_id": ch["id"], "videos": videos}

            elif op == "video_report":
                vid = inputs.get("video_id")
                if not vid:
                    return ToolResult(success=False, error="video_report requires video_id")
                flt = f"video=={vid}"
                totals = self._report(token, start, end, VIDEO_METRICS, filters=flt)
                daily = self._report(token, start, end, "views,estimatedMinutesWatched", dimensions="day", filters=flt, sort="day")
                traffic = self._report(
                    token, start, end, "views,estimatedMinutesWatched",
                    dimensions="insightTrafficSourceType", filters=flt, sort="-views",
                )
                retention, err = self._try_report(
                    token, start, end, "audienceWatchRatio,relativeRetentionPerformance",
                    dimensions="elapsedVideoTimeRatio", filters=f"{flt};audienceType==ORGANIC",
                )
                if err:
                    notes.append(f"Retention unavailable: {err[:160]}")
                impressions, err = self._try_report(token, start, end, IMPRESSION_METRICS, filters=flt)
                if err:
                    notes.append(f"Impressions/CTR unavailable via API: {err[:160]}")
                data = {
                    "video_id": vid,
                    "totals": (totals[0] if totals else {}) | ((impressions or [{}])[0] if impressions else {}),
                    "daily": daily,
                    "traffic_sources": traffic,
                    "retention": retention or [],
                }

            elif op == "custom":
                if not inputs.get("metrics"):
                    return ToolResult(success=False, error="custom requires metrics")
                data = {
                    "rows": self._report(
                        token, start, end, inputs["metrics"],
                        dimensions=inputs.get("dimensions"), filters=inputs.get("filters"),
                        sort=inputs.get("sort"), maxResults=inputs.get("max_results"),
                    )
                }
            else:
                return ToolResult(success=False, error=f"Unknown operation: {op}")
        except RuntimeError as exc:
            return ToolResult(success=False, error=str(exc))

        data.update({"channel": channel_key, "operation": op, "start_date": start, "end_date": end})
        if notes:
            data["notes"] = notes
        artifacts: list[str] = []
        if inputs.get("output_path"):
            out = Path(inputs["output_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, indent=2))
            artifacts.append(str(out))
        return ToolResult(success=True, data=data, artifacts=artifacts, duration_seconds=round(time.time() - start_time, 2))


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="YouTube analytics for OpenMontage channels",
        epilog=SETUP.format(client_secret=client_secret_path()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    auth = sub.add_parser("auth", help="authorize a channel (opens a browser)")
    auth.add_argument("--channel", required=True)
    auth.add_argument("--no-browser", action="store_true")
    rep = sub.add_parser("report", help="run an operation and print JSON")
    rep.add_argument("--channel", required=True)
    rep.add_argument("--operation", default="channel_overview")
    rep.add_argument("--video-id")
    rep.add_argument("--start-date")
    rep.add_argument("--end-date")
    args = parser.parse_args()

    if args.cmd == "auth":
        print(f"Token saved to {authorize(args.channel, open_browser=not args.no_browser)}")
        return
    result = YouTubeAnalytics().execute(
        {k: v for k, v in {
            "channel": args.channel, "operation": args.operation, "video_id": args.video_id,
            "start_date": args.start_date, "end_date": args.end_date,
        }.items() if v}
    )
    print(json.dumps(result.data if result.success else {"error": result.error}, indent=2))


if __name__ == "__main__":
    _cli()
