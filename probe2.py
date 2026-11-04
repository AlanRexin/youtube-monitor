#!/usr/bin/env python3
"""探针 2：确认 Mensor 官方频道活跃度 + 官网嵌入视频归属 + 重试偶发 404 频道。"""
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
WEB_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
WEB_CTX = {"clientName": "WEB", "clientVersion": "2.20250101.01.00",
           "hl": "en", "gl": "US"}
OUT = Path("data/probe2.json")


def http_get(url, timeout=25, data=None, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace"), url
        except Exception:
            return e.code, "", url
    except Exception as e:
        return None, "ERR: %s" % e, url


def yt_post(endpoint, payload):
    url = "https://www.youtube.com/youtubei/v1/{}?key={}".format(endpoint, WEB_KEY)
    st, body, _ = http_get(url, data=json.dumps(payload).encode("utf-8"),
                           headers={"Content-Type": "application/json"})
    if st != 200:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def channel_meta(channel_id):
    st, body, _ = http_get("https://www.youtube.com/channel/" + channel_id)
    out = {"channel_id": channel_id, "http": st}
    if st != 200:
        return out
    for key, pat in [
        ("og_title", r'<meta property="og:title" content="([^"]*)"'),
        ("og_description", r'<meta property="og:description" content="([^"]*)"'),
        ("subscribers", r'"subscriberCountText":\{"simpleText":"([^"]+)"'),
        ("videos_count", r'"videosCountText":\{"runs":\[\{"text":"([^"]+)"'),
        ("canonical", r'"canonicalBaseUrl":"(/@[^"]+)"'),
        ("vanity", r'"vanityChannelUrl":"([^"]+)"'),
    ]:
        m = re.search(pat, body)
        v = m.group(1) if m else None
        if key == "og_description" and v:
            v = v[:500]
        out[key] = v
    return out


def rss_full(channel_id, n=6):
    st, body, _ = http_get("https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id)
    if st != 200:
        return {"http": st}
    items = re.findall(r"<entry>(.*?)</entry>", body, re.S)[:n]
    out = []
    for it in items:
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        p = re.search(r"<published>(.*?)</published>", it)
        a = re.search(r"<yt:videoId>(.*?)</yt:videoId>", it)
        out.append({"video_id": a.group(1) if a else None,
                    "title": t.group(1) if t else None,
                    "published": p.group(1) if p else None})
    return {"http": st, "videos": out}


def player_channel(video_id):
    payload = {"context": {"client": WEB_CTX}, "videoId": video_id}
    d = yt_post("player", payload)
    if not d:
        return None
    vd = d.get("videoDetails") or {}
    return {"title": vd.get("title"), "author": vd.get("author"),
            "channel_id": vd.get("channelId"),
            "view_count": vd.get("viewCount")}


def main():
    out = {"candidates": {}, "players": {}}
    for name, cid in [
        ("mensor_lp", "UCQe1k4TudcvW1MVoHDaVaxQ"),
        ("fluke_es_retry", "UCynGTWqOzeTecEdrB4DUgWSQ"),
        ("joemensor_retry", "UC97blA8S6lVir-gdGFX9r-Q"),
        ("ametek_inc", "UCJ57iRcRhWjubFCZr64Fvxg"),
        ("ametek_stc", "UCtSsF3FPbpTLyp_8OSPYaRA"),
        ("wika_group", "UCzm89EssQbaLCtxS1oXNwpA"),
    ]:
        info = channel_meta(cid)
        info["rss"] = rss_full(cid)
        out["candidates"][name] = info

    # mensor.com 首页嵌入的视频属于哪个频道
    for vid in ["42ayt5suUhQ"]:
        out["players"][vid] = player_channel(vid)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("probe2 written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
