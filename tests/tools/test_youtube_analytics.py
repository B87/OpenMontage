"""youtube_analytics: token handling and report shaping, with the Google APIs mocked."""

import json
import time

import pytest

import tools.analysis.youtube_analytics as yta
from tools.base_tool import ToolStatus


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_YOUTUBE_DIR", str(tmp_path))
    (tmp_path / "client_secret.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret"}})
    )
    return tmp_path


def _token(cfg, channel="demo", expires_in=3600):
    path = cfg / "tokens" / f"{channel}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"access_token": "old", "refresh_token": "r", "expires_at": time.time() + expires_in})
    )
    return path


def test_status_needs_client_and_token(tmp_path, monkeypatch, cfg):
    tool = yta.YouTubeAnalytics()
    assert tool.get_status() == ToolStatus.DEGRADED
    _token(cfg)
    assert tool.get_status() == ToolStatus.AVAILABLE
    monkeypatch.setenv("OPENMONTAGE_YOUTUBE_DIR", str(tmp_path / "empty"))
    assert tool.get_status() == ToolStatus.UNAVAILABLE


def test_missing_token_gives_auth_instructions(cfg):
    result = yta.YouTubeAnalytics().execute({"channel": "demo", "operation": "channel_overview"})
    assert not result.success
    assert "auth --channel demo" in result.error


def test_expired_token_is_refreshed_and_saved(cfg, monkeypatch):
    path = _token(cfg, expires_in=-10)
    calls = []

    def fake_post(url, data):
        calls.append(data)
        return {"access_token": "new", "expires_in": 3600}

    monkeypatch.setattr(yta, "_http_post", fake_post)
    assert yta.access_token("demo") == "new"
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "r"
    assert json.loads(path.read_text())["access_token"] == "new"


def test_valid_token_is_reused(cfg, monkeypatch):
    _token(cfg)
    monkeypatch.setattr(yta, "_http_post", lambda *a, **k: pytest.fail("should not refresh"))
    assert yta.access_token("demo") == "old"


def test_rows_maps_column_headers():
    report = {"columnHeaders": [{"name": "day"}, {"name": "views"}], "rows": [["2026-10-01", 5], ["2026-10-02", 7]]}
    assert yta._rows(report) == [{"day": "2026-10-01", "views": 5}, {"day": "2026-10-02", "views": 7}]
    assert yta._rows({"columnHeaders": [{"name": "views"}]}) == []


def test_video_report_shapes_sections_and_tolerates_missing_metrics(cfg, monkeypatch, tmp_path):
    _token(cfg)
    seen = []

    def fake_get(url, params, token):
        seen.append(params)
        metrics, dims = params.get("metrics", ""), params.get("dimensions")
        if metrics == yta.IMPRESSION_METRICS:
            raise RuntimeError("GET failed (400): Unknown identifier")
        if dims == "day":
            return {"columnHeaders": [{"name": "day"}, {"name": "views"}], "rows": [["2026-10-01", 12]]}
        if dims == "insightTrafficSourceType":
            return {"columnHeaders": [{"name": "insightTrafficSourceType"}, {"name": "views"}], "rows": [["YT_SEARCH", 9]]}
        if dims == "elapsedVideoTimeRatio":
            return {"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}], "rows": [[0.5, 0.4]]}
        return {"columnHeaders": [{"name": "views"}], "rows": [[19]]}

    monkeypatch.setattr(yta, "_http_get", fake_get)
    out = tmp_path / "report.json"
    result = yta.YouTubeAnalytics().execute(
        {"channel": "demo", "operation": "video_report", "video_id": "abc", "output_path": str(out)}
    )
    assert result.success, result.error
    data = result.data
    assert data["totals"] == {"views": 19}
    assert data["daily"] == [{"day": "2026-10-01", "views": 12}]
    assert data["traffic_sources"][0]["insightTrafficSourceType"] == "YT_SEARCH"
    assert data["retention"] == [{"elapsedVideoTimeRatio": 0.5, "audienceWatchRatio": 0.4}]
    assert any("Impressions/CTR unavailable" in n for n in data["notes"])
    assert all(p["ids"] == "channel==MINE" for p in seen)
    assert any(p.get("filters") == "video==abc" for p in seen)
    assert json.loads(out.read_text())["video_id"] == "abc"


def test_video_report_requires_video_id(cfg):
    _token(cfg)
    result = yta.YouTubeAnalytics().execute({"channel": "demo", "operation": "video_report"})
    assert not result.success


def test_authorize_round_trip_over_loopback(cfg, monkeypatch):
    """Simulate the browser: hit the loopback redirect with a code, then check the exchange."""
    import threading
    import urllib.parse
    import urllib.request

    exchanged = {}

    def fake_browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert q["access_type"] == "offline" and q["code_challenge_method"] == "S256"
        assert "yt-analytics.readonly" in q["scope"]
        redirect = f"{q['redirect_uri']}/?code=the-code&state={q['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(redirect, timeout=5).read()).start()

    def fake_post(url, data):
        exchanged.update(data)
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

    monkeypatch.setattr("webbrowser.open", fake_browser)
    monkeypatch.setattr(yta, "_http_post", fake_post)
    path = yta.authorize("demo")

    assert exchanged["code"] == "the-code"
    assert exchanged["grant_type"] == "authorization_code"
    assert exchanged["code_verifier"]
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "r"
    assert oct(path.stat().st_mode)[-3:] == "600"
