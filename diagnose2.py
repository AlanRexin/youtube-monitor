#!/usr/bin/env python3
"""YouTube 兜底通道探测（第二轮诊断，GitHub Actions runner 内执行）。

验证三件事：
1. innertube browse 带 Videos tab 参数时的返回结构（用于写解析器）
2. innertube resolve_url 能否把 @handle 解析成 UC channel_id
3. RSS 连续多次请求是否稳定 404（判断是否 IP 级封锁）+ 第三方镜像可用性
"""
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "diagnose2.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
WEBKEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
UC = "UCwQKBOxKye-nDL_dycFjq-Q"
WEB_CTX = {"clientName": "WEB", "clientVersion": "2.20250101.01.00",
           "hl": "en", "gl": "US"}


def http(url, data=None, headers=None, timeout=20):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    if data is not None:
        h.setdefault("Content-Type", "application/json")
        data = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.url
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(), url
        except Exception:
            return e.code, b"", url
    except Exception as e:
        return None, ("{}: {}".format(type(e).__name__, e)).encode(), url


def main():
    out = {"run_time": datetime.now(timezone.utc).isoformat(), "probes": {}}

    # 1. RSS 连打 3 次，间隔 4 秒
    rss = "https://www.youtube.com/feeds/videos.xml?channel_id=" + UC
    attempts = []
    for i in range(3):
        st, body, fin = http(rss)
        attempts.append({"try": i + 1, "status": st, "len": len(body),
                         "head": body[:80].decode("utf-8", "ignore")})
        if i < 2:
            time.sleep(4)
    out["probes"]["rss_repeat"] = attempts

    # 2. innertube Videos tab（params 为 Videos 分页参数）
    st, body, _ = http(
        "https://www.youtube.com/youtubei/v1/browse?key=" + WEBKEY,
        {"context": {"client": WEB_CTX}, "browseId": UC,
         "params": "EgZ2aWRlb3PyBgQKAjoA"})
    probe = {"status": st}
    if st == 200:
        try:
            j = json.loads(body)
            tabs = j["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
            items = None
            for t in tabs:
                tr = t.get("tabRenderer", {})
                if tr.get("title") == "Videos":
                    items = tr.get("content", {}).get(
                        "sectionListRenderer", {}).get("contents", [])
            probe["videos_tab_found"] = items is not None
            if items:
                sec = items[0].get("itemSectionRenderer", {})
                grid = sec.get("contents", [])[0].get(
                    "gridRenderer", {})
                probe["grid_item_count"] = len(grid.get("items", []))
                first = grid.get("items", [{}])[0]
                probe["first_item_keys"] = list(first.keys())
                probe["first_item_sample"] = json.dumps(
                    first, ensure_ascii=False)[:1800]
        except Exception as e:
            probe["parse_error"] = "{}: {}".format(type(e).__name__, e)
            probe["body_head"] = body[:300].decode("utf-8", "ignore")
    else:
        probe["body_head"] = body[:200].decode("utf-8", "ignore")
    out["probes"]["innertube_videos_tab"] = probe

    # 3. resolve_url 解析 handle
    resolve = []
    for handle in ["FlukeCalibration", "Additel", "Mensor", "AMETEKInc",
                   "Testolimited"]:
        st, body, _ = http(
            "https://www.youtube.com/youtubei/v1/navigation/resolve_url?key="
            + WEBKEY,
            {"context": {"client": WEB_CTX},
             "url": "https://www.youtube.com/@" + handle})
        item = {"handle": handle, "status": st}
        if st == 200:
            try:
                j = json.loads(body)
                item["browseId"] = j.get("endpoint", {}).get(
                    "browseEndpoint", {}).get("browseId")
                item["title"] = j.get("endpoint", {}).get(
                    "browseEndpoint", {}).get("canonicalBaseUrl")
                item["payload_keys"] = list(j.keys())
            except Exception as e:
                item["err"] = "{}: {}".format(type(e).__name__, e)
        else:
            item["body_head"] = body[:150].decode("utf-8", "ignore")
        resolve.append(item)
    out["probes"]["resolve_url"] = resolve

    # 4. 第三方镜像可用性（RSS 兜底通道候选）
    mirrors = {}
    enc = urllib.parse.quote(rss, safe="")
    for name, url in [
        ("allorigins", "https://api.allorigins.win/raw?url=" + enc),
        ("codetabs", "https://api.codetabs.com/v1/proxy?quest=" + enc),
        ("jina", "https://r.jina.ai/" + rss),
    ]:
        st, body, _ = http(url, timeout=25)
        head = body[:120].decode("utf-8", "ignore")
        mirrors[name] = {"status": st, "len": len(body), "head": head}
    out["probes"]["mirrors"] = mirrors

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2)[:4000])

    def git(*a):
        return subprocess.run(["git", *a], cwd=BASE, capture_output=True,
                              text=True)

    git("config", "user.name", "youtube-monitor-bot")
    git("config", "user.email", "actions@github.com")
    git("add", "-A", "data")
    if git("diff", "--cached", "--quiet").returncode != 0:
        r = git("commit", "-m", "diag2: fallback channel probe")
        if r.returncode == 0:
            git("push")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
