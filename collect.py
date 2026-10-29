#!/usr/bin/env python3
"""YouTube 同行频道监控采集器（GitHub Actions 版）

设计（2026-10-29，与任鑫确认的路线）：
- GitHub Actions 境外 runner 定时触发，直连 YouTube，无需 API key（走 RSS）
- 逐频道拉最新视频 → 过滤时间窗内新视频（video_id 去重）
- 结果写 data/latest.json 并 git commit（沙箱侧日报 agent 经 GitHub API 读取合并进 09:00 日报）
- 如配置飞书群机器人 webhook（secrets.FEISHU_WEBHOOK_URL），有新视频时推文本消息
- channels.json 里没填 channel_id 的条目，首跑自动从 handle/user 页面解析并回写

仅用标准库。单频道失败不影响其他频道，失败明细写入 latest.json.failures。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CHANNELS_FILE = BASE / "channels.json"
DATA_DIR = BASE / "data"
LATEST_FILE = DATA_DIR / "latest.json"
STATE_FILE = DATA_DIR / "state.json"

TZ_SH = timezone(timedelta(hours=8))  # 北京时间
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HTTP_TIMEOUT = 20
MAX_REPORTED = 5000  # 去重集合上限，超过按最早日期裁剪

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
YTNS = "{http://www.youtube.com/xml/schemas/2015}"


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_sh(dt):
    return dt.astimezone(TZ_SH).strftime("%Y-%m-%d %H:%M")


def http_get(url, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_json(path, default):
    if Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 频道 ID 解析 ----------------

UC_RE = re.compile(r'"(?:externalId|channelId)":"(UC[A-Za-z0-9_-]{22})"')
UC_RE2 = re.compile(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})")


def channel_page_url(entry):
    if entry.get("handle"):
        return "https://www.youtube.com/@{}".format(
            str(entry["handle"]).lstrip("@"))
    if entry.get("user"):
        return "https://www.youtube.com/user/{}".format(entry["user"])
    return None


def resolve_channel_id(entry):
    """优先用已知 channel_id；否则解析 handle/user 页面拿 UC id。"""
    if entry.get("channel_id"):
        return entry["channel_id"]
    url = channel_page_url(entry)
    if not url:
        raise ValueError("缺少 channel_id/handle/user，无法定位频道")
    html = http_get(url).decode("utf-8", "ignore")
    m = UC_RE.search(html) or UC_RE2.search(html)
    if not m:
        raise ValueError("页面未解析出 channel_id（handle 可能不存在或被反爬）")
    return m.group(1)


# ---------------- RSS 解析 ----------------

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


def fetch_feed_videos(channel_id):
    url = ("https://www.youtube.com/feeds/videos.xml?channel_id={}"
           .format(channel_id))
    return parse_feed(http_get(url))


# ---------------- 时间窗与去重 ----------------

def compute_window_start(last_success_iso, t_now):
    """上次成功时间 -1h 与 now-25h 取较早者：
    正常日跑窗口约 25h；漏跑多日则自动拉宽追平；不会无限增长。"""
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
    # 飞书自定义机器人签名算法：HMAC-SHA256，key = timestamp + "\n" + secret，消息体为空，结果 base64
    string_to_sign = "{}\n{}".format(timestamp, secret)
    digest = hmac.new(string_to_sign.encode("utf-8"),
                      digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


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


# ---------------- git 提交 ----------------

def git(*args):
    return subprocess.run(["git", *args], cwd=BASE, capture_output=True,
                          text=True)


def git_commit_all(message):
    git("config", "user.name", "youtube-monitor-bot")
    git("config", "user.email", "actions@github.com")
    git("add", "-A", "data", "channels.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return False
    r = git("commit", "-m", message)
    if r.returncode != 0:
        raise RuntimeError("git commit 失败: {}".format(r.stderr.strip()))
    r = git("push")
    if r.returncode != 0:
        raise RuntimeError("git push 失败: {}".format(r.stderr.strip()))
    return True


# ---------------- 主流程 ----------------

def main():
    channels = load_json(CHANNELS_FILE, [])
    state = load_json(STATE_FILE, {"reported": {}, "last_success": None})
    reported = state.get("reported", {})
    t_now = now_utc()
    window_start = compute_window_start(state.get("last_success"), t_now)

    new_videos, failures = [], []
    for ch in channels:
        if not ch.get("enabled", True):
            continue
        try:
            cid = resolve_channel_id(ch)
            if cid != ch.get("channel_id"):
                ch["channel_id"] = cid
                ch["channel_id_resolved_at"] = t_now.isoformat()
            vids = fetch_feed_videos(cid)
            ch["last_video_published"] = (
                vids[0]["published"] if vids else None)
            for v in filter_new(vids, window_start, reported):
                v["brand"] = ch.get("brand", "")
                v["channel"] = ch.get("channel_name") or ch.get("handle") or cid
                new_videos.append(v)
        except Exception as exc:
            failures.append({
                "brand": ch.get("brand", "?"),
                "key": ch.get("handle") or ch.get("user") or ch.get("channel_id"),
                "error": "{}: {}".format(type(exc).__name__, exc),
            })
    new_videos.sort(key=lambda v: v["published_dt"], reverse=True)
    for v in new_videos:
        v.pop("published_dt", None)

    # 回写解析出的 channel_id，下次运行不再重复解析
    save_json(CHANNELS_FILE, channels)

    # 推飞书（无新视频或未配置 webhook 都跳过，不算错误）
    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()
    webhook_result = {
        "configured": bool(webhook),
        "posted": False,
        "response": None,
        "note": None,
    }
    if not new_videos:
        webhook_result["note"] = "无新视频，未推送"
    elif not webhook:
        webhook_result["note"] = "未配置 FEISHU_WEBHOOK_URL，仅落盘"
    else:
        try:
            webhook_result["response"] = post_feishu(
                webhook, secret, build_feishu_text(new_videos))
            webhook_result["posted"] = True
        except Exception as exc:
            webhook_result["note"] = "{}: {}".format(type(exc).__name__, exc)

    for v in new_videos:
        reported[v["video_id"]] = t_now.date().isoformat()
    if len(reported) > MAX_REPORTED:
        for k in sorted(reported, key=reported.get)[:len(reported) - MAX_REPORTED]:
            reported.pop(k)
    state["reported"] = reported
    state["last_success"] = t_now.isoformat()

    latest = {
        "run_time": t_now.isoformat(),
        "run_time_sh": fmt_sh(t_now),
        "window_start": window_start.isoformat(),
        "window_start_sh": fmt_sh(window_start),
        "channels_enabled": sum(1 for c in channels if c.get("enabled", True)),
        "new_count": len(new_videos),
        "videos": new_videos,
        "failures": failures,
        "webhook": webhook_result,
    }
    save_json(STATE_FILE, state)
    save_json(LATEST_FILE, latest)

    committed = False
    try:
        committed = git_commit_all(
            "monitor: {} new, {} failures".format(
                len(new_videos), len(failures)))
    except Exception as exc:
        print("git 提交失败: {}".format(exc), file=sys.stderr)

    print(json.dumps({
        "new": len(new_videos),
        "failures": len(failures),
        "posted": webhook_result["posted"],
        "committed": committed,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
