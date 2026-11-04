#!/usr/bin/env python3
"""一次性探针：在境外 runner 上核清频道身份（AMETEK / Mensor / Fluke）。

只做只读探测，结果写 data/probe.json。
"""
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
CHANNEL_FILTER = "EgIQAg=="   # search params: 仅频道
OUT = Path("data/probe.json")


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


def resolve_url(key):
    """@handle / /user/xxx -> UC id"""
    payload = {"context": {"client": WEB_CTX},
               "url": "https://www.youtube.com/" + key.lstrip("@")}
    d = yt_post("navigation/resolve_url", payload)
    if not d:
        return None
    m = re.search(r'"(?:externalId|channelId)":"(UC[A-Za-z0-9_-]{22})"', json.dumps(d))
    return m.group(1) if m else None


def text_of(node):
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return node["simpleText"]
        if "runs" in node:
            return "".join(r.get("text", "") for r in node["runs"])
    return ""


def channel_meta(channel_id):
    """频道页取 og:title / 描述 / 订阅数文本。"""
    st, body, _ = http_get("https://www.youtube.com/channel/" + channel_id)
    out = {"channel_id": channel_id, "http": st}
    if st != 200:
        return out
    m = re.search(r'<meta property="og:title" content="([^"]*)"', body)
    out["og_title"] = m.group(1) if m else None
    m = re.search(r'<meta property="og:description" content="([^"]*)"', body)
    out["og_description"] = (m.group(1)[:400] if m else None)
    m = re.search(r'"subscriberCountText":\{"simpleText":"([^"]+)"', body)
    out["subscribers"] = m.group(1) if m else None
    m = re.search(r'"videosCountText":\{"runs":\[\{"text":"([^"]+)"', body)
    out["videos_count"] = m.group(1) if m else None
    m = re.search(r'"vanityChannelUrl":"([^"]+)"', body)
    out["vanity"] = m.group(1) if m else None
    m = re.search(r'"canonicalBaseUrl":"(/@[^"]+)"', body)
    out["canonical"] = m.group(1) if m else None
    return out


def rss_head(channel_id, n=3):
    st, body, _ = http_get("https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id)
    if st != 200:
        return {"http": st}
    items = re.findall(r"<entry>(.*?)</entry>", body, re.S)[:n]
    out = []
    for it in items:
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        p = re.search(r"<published>(.*?)</published>", it)
        out.append({"title": t.group(1) if t else None,
                    "published": p.group(1) if p else None})
    return {"http": st, "videos": out}


def search_channels(query):
    """innertube 搜索，仅取频道结果。"""
    payload = {"context": {"client": WEB_CTX}, "query": query,
               "params": CHANNEL_FILTER}
    d = yt_post("search", payload)
    if not d:
        return []
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "channelRenderer" in node:
                cr = node["channelRenderer"]
                found.append({
                    "channel_id": cr.get("channelId"),
                    "title": text_of(cr.get("title")),
                    "subs": text_of(cr.get("subscriberCountText")),
                    "videos": text_of(cr.get("videoCountText")),
                    "desc": text_of(cr.get("descriptionSnippet"))[:300],
                    "handle": (cr.get("subscriberCountText") is not None) and
                              ((cr.get("navigationEndpoint", {})
                                .get("browseEndpoint", {}).get("canonicalBaseUrl")) or None),
                })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(d)
    seen, uniq = set(), []
    for f in found:
        if f["channel_id"] and f["channel_id"] not in seen:
            seen.add(f["channel_id"])
            uniq.append(f)
    return uniq[:8]


def site_youtube_links(url):
    st, body, _ = http_get(url)
    links = sorted(set(re.findall(r'https?://(?:www\.)?youtube\.com/[^\s"\'<>\\]+', body or "")))
    return {"http": st, "links": links[:20]}


def main():
    result = {"probe_time": "run", "meta": {}, "search": {}, "sites": {}}

    # 1) 已知/候选 channel_id 的身份核验
    ids = [
        ("ametek_handle_target", "UCJ57iRcRhWjubFCZr64Fvxg"),
        ("ametek_current", "UCDTISRIDBLatulPDz9bQXpA"),
        ("mensor_current", "UC97blA8S6lVir-gdGFX9r-Q"),
        ("fluke_main", "UCR7ecClA4Ul6SDWFySBBLlQ"),
        ("fluke_cal", "UCDJWIiQoSFIyU2wBTS7Vn6Q"),
        ("fluke_es", "UCynGTWqOzeTecEdrB4DUgWSQ"),
    ]
    for name, cid in ids:
        info = channel_meta(cid)
        info["rss"] = rss_head(cid)
        result["meta"][name] = info

    # 2) handle 解析
    for name, key in [("handle_AMETEKInc", "@AMETEKInc"),
                      ("handle_Mensor", "@Mensor"),
                      ("handle_FlukeCalibration", "@FlukeCalibration"),
                      ("handle_FlukeCorporation", "@FlukeCorporation"),
                      ("handle_MensorGmbH", "@MensorGmbH"),
                      ("handle_MensorInstruments", "@MensorInstruments")]:
        cid = resolve_url(key)
        entry = {"key": key, "channel_id": cid}
        if cid:
            entry["meta"] = channel_meta(cid)
        result["meta"][name] = entry

    # 3) innertube 搜索候选
    for q in ["Mensor", "Mensor Instruments", "Mensor GmbH", "AMETEK"]:
        result["search"][q] = search_channels(q)

    # 4) 官网取官方 YouTube 链接
    for url in ["https://www.mensor.com/", "https://www.mensor.com/en-us/",
                "https://www.ametek.com/", "https://www.mensor.com/en-us/contact-us"]:
        result["sites"][url] = site_youtube_links(url)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("probe written:", OUT, "keys:", list(result["meta"].keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
