#!/usr/bin/env python3
"""YouTube 同行频道监控采集器 v2（GitHub Actions 版）

2026-10-30 ~ 11-04 连续 6 天采集全失败的复盘结论：
1) YouTube 对部分 GitHub runner 的公网 IP 段**直接秒回 404**（不是超时），
   定时跑落在被封 IP 段时全部频道 RSS 抓取失败，而工作流仍是绿灯；
2) YouTube 已不再向数据中心 IP 返回 @handle 页面（404），handle 解析必须改走
   innertube resolve_url；
3) v1 的全局 last_success 会被失败覆盖，窗口一直只保留 25 小时，故障期间漏采
   无法追平。

v2 改动：
- 抓取（本文件）与发布（merge.py）分离：多 runner 并行抓取、各自产出独立结果
  文件，merge 汇总取并集——任一 runner 的 IP 可用即可拿到完整数据；
- 逐级兜底通道：RSS → r.jina.ai 镜像 RSS → innertube 视频列表，逐级验证内容有效；
- handle 解析改用 innertube resolve_url；
- 每频道独立记录 last_ok，失败不推进窗口，漏跑可自动追平；
- 频道身份复核：抓 channel 页 og:title 与 handle 解析结果比对，标记映射漂移；
- 只用标准库。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CHANNELS_FILE = BASE / "channels.json"
DATA_DIR = BASE / "data"
STATE_FILE = DATA_DIR / "state.json"
LATEST_FILE = DATA_DIR / "latest.json"

TZ_SH = timezone(timedelta(hours=8))  # 北京时间
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HTTP_TIMEOUT = 20
MAX_REPORTED = 5000  # 去重集合上限，超过按最早日期裁剪

# YouTube Web 客户端公开 key（公开网页端自带，非用户密钥），用于 innertube 兜底
WEB_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
WEB_CTX = {"clientName": "WEB", "clientVersion": "2.20250101.01.00",
           "hl": "en", "gl": "US"}
VIDEOS_TAB_PARAMS = "EgZ2aWRlb3PyBgQKAjoA"  # Videos 分页参数

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
YTNS = "{http://www.youtube.com/xml/schemas/2015}"

UC_RE = re.compile(r'"(?:externalId|channelId)":"(UC[A-Za-z0-9_-]{22})"')
UC_RE2 = re.compile(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})")
OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')

SOURCE_RANK = {"rss": 0, "rss_proxy": 1, "innertube": 2}


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_sh(dt):
    return dt.astimezone(TZ_SH).strftime("%Y-%m-%d %H:%M")


def load_json(path, default):
    if Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def http_get(url, headers=None, timeout=HTTP_TIMEOUT, data=None):
    """返回 (status, body, url)。HTTP 错误不抛异常，status 为 None 表示网络异常。"""
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.url
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, url
    except Exception as e:
        return None, ("{}: {}".format(type(e).__name__, e)).encode("utf-8"), url


# ---------------- 频道 ID 解析 ----------------

def channel_page_url(entry):
    if entry.get("handle"):
        return "https://www.youtube.com/@{}".format(
            str(entry["handle"]).lstrip("@"))
    if entry.get("user"):
        return "https://www.youtube.com/user/{}".format(entry["user"])
    return None


def yt_post(endpoint, payload, timeout=HTTP_TIMEOUT):
    url = "https://www.youtube.com/youtubei/v1/{}?key={}".format(
        endpoint, WEB_KEY)
    st, body, _ = http_get(
        url, headers={"Content-Type": "application/json"},
        timeout=timeout, data=json.dumps(payload).encode("utf-8"))
    if st != 200:
        return None, "{}: HTTP {}".format(endpoint, st)
    try:
        return json.loads(body), None
    except Exception as e:
        return None, "{}: JSON 解析失败 {}".format(endpoint, e)


def resolve_channel_id_by_innertube(key):
    """把 @handle 或 /user/xxx 解析成 UC channel_id（@handle 页面已对数据中心失效）。"""
    if not key:
        return None, "缺少 handle/user"
    if str(key).startswith("user/"):
        url = "https://www.youtube.com/{}".format(key)
    else:
        url = "https://www.youtube.com/@{}".format(str(key).lstrip("@"))
    j, err = yt_post("navigation/resolve_url",
                     {"context": {"client": WEB_CTX}, "url": url})
    if err:
        return None, err
    bid = ((j or {}).get("endpoint") or {}).get(
        "browseEndpoint", {}).get("browseId")
    if not bid:
        return None, "resolve_url 未返回 browseId"
    return bid, None


def resolve_channel_id(entry):
    """优先用已知 channel_id；否则解析 handle/user 拿 UC id（innertube 优先，页面兜底）。"""
    if entry.get("channel_id"):
        return entry["channel_id"], None
    key = entry.get("handle") or entry.get("user")
    bid, err = resolve_channel_id_by_innertube(key)
    if bid:
        return bid, None
    # 兜底：频道页正则（目前对数据中心 IP 基本失效，留作以后兼容）
    url = channel_page_url(entry)
    if not url:
        return None, "缺少 channel_id/handle/user，无法定位频道"
    st, body, _ = http_get(url)
    if st != 200:
        return None, "频道页 HTTP {}".format(st)
    m = UC_RE.search(body.decode("utf-8", "ignore")) or UC_RE2.search(
        body.decode("utf-8", "ignore"))
    if not m:
        return None, "页面未解析出 channel_id（handle 可能不存在或被反爬）"
    return m.group(1), None


def fetch_channel_title(channel_id):
    """取频道页 og:title，用于复核频道身份是否漂移。"""
    st, body, _ = http_get(
        "https://www.youtube.com/channel/{}".format(channel_id))
    if st != 200:
        return None
    m = OG_TITLE_RE.search(body.decode("utf-8", "ignore"))
    return m.group(1) if m else None


# ---------------- RSS 解析与抓取 ----------------

def parse_feed(xml_bytes):
    """解析 YouTube RSS（Atom）为视频列表，按原序（最新在前）。"""
    root = ET.fromstring(xml_bytes)
    videos = []
    for e in root.findall("{}entry".format(ATOM)):
        vid = (e.findtext("{}videoId".format(YTNS)) or "").strip()
        title = (e.findtext("{}title".format(ATOM)) or "").strip()
        link = ""
        le = e.find("{}link".format(ATOM))
        if le is not None:
            link = le.get("href", "")
        published = (e.findtext("{}published".format(ATOM)) or "").strip()
        views = None
        st = e.find(".//{}statistics".format(MEDIA))
        if st is not None:
            views = st.get("views")
        if vid and published:
            videos.append({
                "video_id": vid,
                "title": title,
                "url": link or "https://www.youtube.com/watch?v={}".format(vid),
                "published": published,
                "views": views,
            })
    return videos


def rss_url(channel_id):
    return "https://www.youtube.com/feeds/videos.xml?channel_id={}".format(
        channel_id)


def fetch_rss(channel_id, attempts=2):
    url = rss_url(channel_id)
    last = "未知错误"
    for i in range(attempts):
        st, body, _ = http_get(url)
        if st == 200:
            try:
                vids = parse_feed(body)
                if vids:
                    return {"source": "rss", "videos": vids, "error": None}
                last = "RSS 无条目"
            except Exception as e:
                last = "RSS 解析失败: {}".format(e)
        else:
            last = "HTTP {}".format(st)
        if i + 1 < attempts:
            time.sleep(3)
    return {"source": "rss", "videos": [], "error": last}


def fetch_rss_proxy(channel_id):
    """镜像兜底：r.jina.ai 代理 RSS（出口 IP 与 runner 不同，绕开 IP 段封锁）。"""
    url = "https://r.jina.ai/" + rss_url(channel_id)
    st, body, _ = http_get(url, headers={"X-Return-Format": "text",
                                         "X-Timeout": "25"}, timeout=30)
    if st != 200:
        return {"source": "rss_proxy", "videos": [],
                "error": "镜像 HTTP {}".format(st)}
    idx = body.find(b"<?xml")
    if idx >= 0:
        body = body[idx:]
    try:
        vids = parse_feed(body)
    except Exception as e:
        return {"source": "rss_proxy", "videos": [],
                "error": "镜像内容非 RSS XML: {}".format(e)}
    if not vids:
        return {"source": "rss_proxy", "videos": [], "error": "镜像无条目"}
    return {"source": "rss_proxy", "videos": vids, "error": None}


def _text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return node["simpleText"]
        runs = node.get("runs")
        if isinstance(runs, list):
            return "".join(r.get("text", "") for r in runs
                           if isinstance(r, dict))
    return ""


def _collect_video_items(node, out):
    """递归提取 innertube 返回里的视频条目。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, dict) and isinstance(v.get("videoId"), str):
                out.append({
                    "video_id": v["videoId"],
                    "title": _text(v.get("title")),
                    "published_text": _text(v.get("publishedTimeText")),
                    "views": _text(v.get("viewCountText")),
                    "shorts": k == "reelItemRenderer",
                })
            _collect_video_items(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_video_items(v, out)


_REL_UNITS = {"minute": 1.0 / 1440, "hour": 1.0 / 24, "day": 1.0,
              "week": 7.0, "month": 30.0, "year": 365.0}


def parse_relative_published(text, now=None):
    """把 '3 days ago' 这类相对发布时间换算成 UTC 时间点（近似）。"""
    if not text:
        return None
    m = re.search(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago",
                  text, re.I)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=n * _REL_UNITS[unit])


def fetch_innertube_videos(channel_id):
    """innertube 兜底：拿频道 Videos 列表（发布时间为相对值，近似）。"""
    payload = {"context": {"client": WEB_CTX}, "browseId": channel_id,
               "params": VIDEOS_TAB_PARAMS}
    j, err = yt_post("browse", payload)
    if err:
        return {"source": "innertube", "videos": [], "error": err}
    items = []
    _collect_video_items(j, items)
    seen, videos = set(), []
    for it in items:
        vid = it.get("video_id")
        if not vid or vid in seen or it.get("shorts"):
            continue
        seen.add(vid)
        dt = parse_relative_published(it.get("published_text"))
        videos.append({
            "video_id": vid,
            "title": it.get("title") or "",
            "url": "https://www.youtube.com/watch?v={}".format(vid),
            "published": dt.isoformat() if dt else None,
            "published_approx": True,
            "views": it.get("views") or None,
        })
    if not videos:
        return {"source": "innertube", "videos": [], "error": "innertube 无条目"}
    return {"source": "innertube", "videos": videos, "error": None}


def fetch_channel_videos(channel_id):
    """三级兜底：RSS → 镜像 RSS → innertube。"""
    r = fetch_rss(channel_id)
    if r["videos"]:
        return r
    r2 = fetch_rss_proxy(channel_id)
    if r2["videos"]:
        return r2
    r3 = fetch_innertube_videos(channel_id)
    if r3["videos"]:
        return r3
    return {"source": "rss", "videos": [],
            "error": "RSS: {} | 镜像: {} | innertube: {}".format(
                r["error"], r2["error"], r3["error"])}


# ---------------- 时间窗与去重（与 v1 保持一致，供单测覆盖） ----------------

def compute_window_start(last_success_iso, t_now):
    """上次成功时间 -1h 与 now-25h 取较早者：正常日跑窗口约 25h；漏跑多日则拉宽追平。"""
    fallback = t_now - timedelta(hours=25)
    if not last_success_iso:
        return fallback
    return min(parse_iso(last_success_iso) - timedelta(hours=1), fallback)


def filter_new(videos, window_start, reported):
    out = []
    for v in videos:
        try:
            pub = parse_iso(v["published"])
        except Exception:
            continue
        if pub >= window_start and v["video_id"] not in reported:
            v = dict(v)
            v["published_dt"] = pub
            v["published_sh"] = fmt_sh(pub)
            out.append(v)
    return out


# ---------------- 飞书群机器人 ----------------

def feishu_sign(secret, timestamp):
    string_to_sign = "{}\n{}".format(timestamp, secret)
    digest = __import__("hmac").new(
        string_to_sign.encode("utf-8"),
        digestmod=__import__("hashlib").sha256).digest()
    return __import__("base64").b64encode(digest).decode("utf-8")


def build_feishu_text(items, limit=15):
    lines = ["YouTube 同行动态：过去一天 {} 条新视频".format(len(items))]
    for v in items[:limit]:
        lines.append("【{}】{}（{}）\n{}".format(
            v["brand"], v["title"], v["published_sh"], v["url"]))
    if len(items) > limit:
        lines.append("……等共 {} 条".format(len(items)))
    return "\n".join(lines)


def post_feishu(webhook, secret, text):
    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = feishu_sign(secret, ts)
    req = urllib.request.Request(
        webhook, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------- 抓取阶段主流程 ----------------

def git(*args):
    return subprocess.run(["git", *args], cwd=BASE, capture_output=True,
                          text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runner", default="local")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    channels = load_json(CHANNELS_FILE, [])
    t_now = now_utc()
    results = []
    for ch in channels:
        if not ch.get("enabled", True):
            continue
        rec = {
            "key": ch.get("channel_id") or ch.get("handle") or ch.get("user"),
            "brand": ch.get("brand"),
            "channel_name": ch.get("channel_name"),
            "handle": ch.get("handle"),
            "user": ch.get("user"),
            "channel_id": ch.get("channel_id"),
            "resolved_channel_id": None,
            "handle_resolved_id": None,
            "title_now": None,
            "source": None,
            "videos": [],
            "error": None,
        }
        cid, err = resolve_channel_id(ch)
        if not cid:
            rec["error"] = err or "无法定位频道"
            results.append(rec)
            continue
        if not ch.get("channel_id"):
            rec["resolved_channel_id"] = cid
        if ch.get("handle"):
            bid, _ = resolve_channel_id_by_innertube(ch["handle"])
            rec["handle_resolved_id"] = bid
        rec["title_now"] = fetch_channel_title(cid)
        r = fetch_channel_videos(cid)
        rec["source"] = r["source"]
        rec["videos"] = r["videos"]
        rec["error"] = r["error"]
        if r["videos"]:
            rec["last_video_published"] = r["videos"][0].get("published")
        results.append(rec)

    out = {"runner": args.runner, "run_time": t_now.isoformat(),
           "results": results}
    out_path = Path(args.out) if args.out else DATA_DIR / "run-{}.json".format(
        args.runner)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    ok = sum(1 for r in results if not r["error"])
    print(json.dumps({"runner": args.runner, "channels": len(results),
                      "ok": ok,
                      "failed": [r["key"] for r in results if r["error"]],
                      "sources": sorted(set(r["source"] for r in results
                                            if r["source"]))},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
