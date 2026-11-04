#!/usr/bin/env python3
"""YouTube 可达性远程诊断（在 GitHub Actions 境外 runner 内执行）。

背景：2026-10-30 起 collect.py 在 runner 上对 youtube.com 的所有请求
（handle 页面解析 + RSS feed）均返回 404/500，本脚本用多种姿势探测，
定位是哪种请求方式还能用。结果写 data/diagnose.json 并 commit，
沙箱侧经 GitHub API 读取。
"""
import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "diagnose.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

UC = "UCwQKBOxKye-nDL_dycFjq-Q"  # Beamex 官方频道
WEBKEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"  # YouTube Web 客户端公开 key


def probe_get(url, headers=None, label=""):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            return {"label": label, "url": url, "status": r.status,
                    "final_url": r.url, "len": len(body),
                    "snippet": body[:300].decode("utf-8", "ignore")}
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
            snip = body[:300].decode("utf-8", "ignore")
        except Exception:
            snip = ""
        return {"label": label, "url": url, "status": e.code,
                "error": str(e), "snippet": snip}
    except Exception as e:
        return {"label": label, "url": url,
                "error": "{}: {}".format(type(e).__name__, e)}


def probe_post(url, payload, label=""):
    h = {"Content-Type": "application/json", "User-Agent": UA}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            parsed = {}
            try:
                j = json.loads(body)
                try:
                    tabs = [t.get("tabRenderer", {}).get("title")
                            for t in j["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]]
                    parsed["tabs"] = tabs
                except Exception:
                    pass
                parsed["videoRenderer_count"] = body.count(b'"videoRenderer"')
                parsed["richItemRenderer_count"] = body.count(b'"richItemRenderer"')
                parsed["channelTitle"] = (
                    j.get("metadata", {})
                    .get("channelMetadataRenderer", {})
                    .get("title"))
            except Exception:
                parsed["snippet"] = body[:300].decode("utf-8", "ignore")
            return {"label": label, "url": url, "status": r.status,
                    "parsed": parsed}
    except urllib.error.HTTPError as e:
        try:
            snip = e.read()[:300].decode("utf-8", "ignore")
        except Exception:
            snip = ""
        return {"label": label, "url": url, "status": e.code,
                "snippet": snip}
    except Exception as e:
        return {"label": label, "url": url,
                "error": "{}: {}".format(type(e).__name__, e)}


def git(*args):
    return subprocess.run(["git", *args], cwd=BASE, capture_output=True,
                          text=True)


def main():
    results = []
    try:
        ip = urllib.request.urlopen(
            "https://api.ipify.org", timeout=10).read().decode("utf-8")
    except Exception as e:
        ip = "unknown {}".format(e)
    results.append({"label": "runner_ip", "value": ip})

    rss = "https://www.youtube.com/feeds/videos.xml?channel_id=" + UC
    results.append(probe_get("https://www.youtube.com/", label="yt_root"))
    results.append(probe_get("https://www.youtube.com/@Beamex",
                             label="handle_page"))
    results.append(probe_get("https://www.youtube.com/channel/" + UC,
                             label="channel_page"))
    results.append(probe_get(rss, label="rss_plain"))
    results.append(probe_get(rss, headers={
        "Cookie": "CONSENT=YES+cb.20210328-17-p0.en+FX+419; SOCS=CAISNUnDbg"},
        label="rss_consent_cookie"))
    results.append(probe_get(rss, headers={"User-Agent": "curl/8.5.0"},
                             label="rss_curl_ua"))
    results.append(probe_get(
        "https://www.youtube.com/feeds/videos.xml?user=Beamex",
        label="rss_user_param"))
    results.append(probe_post(
        "https://www.youtube.com/youtubei/v1/browse?key=" + WEBKEY,
        {"context": {"client": {"clientName": "WEB",
                                "clientVersion": "2.20250101.01.00",
                                "hl": "en", "gl": "US"}},
         "browseId": UC},
        label="innertube_web"))
    results.append(probe_post(
        "https://www.youtube.com/youtubei/v1/browse?key=" + WEBKEY,
        {"context": {"client": {"clientName": "ANDROID",
                                "clientVersion": "19.09.37",
                                "androidSdkVersion": 34,
                                "hl": "en", "gl": "US"}},
         "browseId": UC},
        label="innertube_android"))

    out = {"run_time": datetime.now(timezone.utc).isoformat(),
           "results": results}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))

    git("config", "user.name", "youtube-monitor-bot")
    git("config", "user.email", "actions@github.com")
    git("add", "-A", "data")
    if git("diff", "--cached", "--quiet").returncode != 0:
        r = git("commit", "-m", "diag: youtube reachability probe")
        if r.returncode != 0:
            print("commit failed: {}".format(r.stderr.strip()))
            return 1
        r = git("push")
        if r.returncode != 0:
            print("push failed: {}".format(r.stderr.strip()))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
