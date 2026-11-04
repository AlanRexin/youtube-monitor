#!/usr/bin/env python3
"""YouTube 同行频道监控 · 合并发布阶段（GitHub Actions merge job）

输入：data/runs/run-*.json（各 runner 抓取阶段的独立结果文件）
输出：data/latest.json（日报消费）、data/state.json（去重集合）、
      data/last_ok.json（最近一次成功采集，供全失败时回退）、channels.json 回写

要点：
- 多 runner 结果按频道取并集：优先无失败 > 来源可靠度(rss > rss_proxy > innertube)
  > 条目更多；
- 每频道独立窗口：last_ok 只在该频道抓取成功时推进，失败频道窗口自动拉宽追平；
- 全部频道失败时：latest.json 回退到最近一次成功结果并标注 status=failed，
  同时以非 0 退出（工作流转红 → 仓库所有者收到失败通知），避免静默漏采；
- 抓取阶段与本阶段分离，因此并发 runner 互不冲突。
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402

BASE = Path(__file__).resolve().parent
CHANNELS_FILE = BASE / "channels.json"
DATA_DIR = BASE / "data"
STATE_FILE = DATA_DIR / "state.json"
LATEST_FILE = DATA_DIR / "latest.json"
LAST_OK_FILE = DATA_DIR / "last_ok.json"
RUNS_DIR = DATA_DIR / "runs"

SOURCE_RANK = {"rss": 0, "rss_proxy": 1, "innertube": 2}


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_sh(dt):
    return dt.astimezone(collect.TZ_SH).strftime("%Y-%m-%d %H:%M")


def load_json(path, default):
    if Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def channel_key(ch):
    return ch.get("channel_id") or ch.get("handle") or ch.get("user")


def better(rec, cur):
    """a 是否比 b 更值得采用。"""
    if bool(rec.get("error")) != bool(cur.get("error")):
        return not bool(rec.get("error"))
    r_rank = SOURCE_RANK.get(rec.get("source"), 9)
    c_rank = SOURCE_RANK.get(cur.get("source"), 9)
    if r_rank != c_rank:
        return r_rank < c_rank
    return len(rec.get("videos") or []) > len(cur.get("videos") or [])


def git(*args):
    return subprocess.run(["git", *args], cwd=BASE, capture_output=True,
                          text=True)


def commit_and_push(message):
    git("config", "user.name", "youtube-monitor-bot")
    git("config", "user.email", "actions@github.com")
    git("add", "-A", "data", "channels.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return False
    r = git("commit", "-m", message)
    if r.returncode != 0:
        raise RuntimeError("git commit 失败: {}".format(r.stderr.strip()))
    for attempt in range(3):
        r = git("push")
        if r.returncode == 0:
            return True
        time.sleep(3)
        git("pull", "--rebase", "origin", "main")
    raise RuntimeError("git push 失败: {}".format(r.stderr.strip()))


def main():
    t_now = now_utc()
    channels = load_json(CHANNELS_FILE, [])
    state = load_json(STATE_FILE, {"reported": {}, "last_success": None})
    reported = state.get("reported", {})
    catchup_raw = os.environ.get("CATCHUP_FROM", "").strip()
    catchup_from = parse_iso(catchup_raw) if catchup_raw else None

    runs = []
    if RUNS_DIR.exists():
        for p in sorted(RUNS_DIR.glob("run-*.json")):
            try:
                runs.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception as e:
                print("读取 {} 失败: {}".format(p.name, e))
    if not runs:
        print("没有任何抓取结果文件，合并中止")
        return 2

    best = {}
    for run in runs:
        for rec in run.get("results", []):
            key = rec.get("key")
            cur = best.get(key)
            if cur is None or better(rec, cur):
                best[key] = rec

    new_videos, failures, sources = [], [], {}
    channels_ok = 0
    windows = []
    for ch in channels:
        if not ch.get("enabled", True):
            continue
        key = channel_key(ch)
        rec = best.get(key)
        if rec is None:
            failures.append({"brand": ch.get("brand", "?"), "key": key,
                             "error": "所有 runner 均未产出该频道结果"})
            continue

        last_ok = ch.get("last_ok")
        if last_ok:
            ws = min(parse_iso(last_ok) - timedelta(hours=1),
                     t_now - timedelta(hours=25))
        else:
            ws = t_now - timedelta(hours=25)
            if catchup_from:
                ws = min(ws, catchup_from)
        windows.append(ws)

        if rec.get("error"):
            failures.append({"brand": ch.get("brand", "?"), "key": key,
                             "error": rec["error"]})
            continue

        channels_ok += 1
        source = rec.get("source") or "rss"
        sources[source] = sources.get(source, 0) + 1
        vids = rec.get("videos") or []
        for v in vids:
            vid = v.get("video_id")
            if not vid or vid in reported:
                continue
            pub = v.get("published")
            if pub:
                try:
                    pub_dt = parse_iso(pub)
                except Exception:
                    pub_dt = None
                if pub_dt and pub_dt < ws:
                    continue
            item = {
                "video_id": vid,
                "brand": ch.get("brand", ""),
                "channel": ch.get("channel_name") or ch.get("handle") or key,
                "title": v.get("title") or "",
                "url": v.get("url") or "https://www.youtube.com/watch?v={}".format(vid),
                "views": v.get("views"),
                "source": source,
                "published_sh": fmt_sh(pub_dt) if pub_dt else "",
                "published_approx": bool(v.get("published_approx")),
            }
            new_videos.append(item)
            reported[vid] = t_now.date().isoformat()

        # 回写频道状态：只有抓取成功才推进 last_ok
        ch["last_ok"] = t_now.isoformat()
        ch["last_ok_source"] = source
        if vids and vids[0].get("published"):
            ch["last_video_published"] = vids[0]["published"]
        if rec.get("resolved_channel_id"):
            ch["channel_id"] = rec["resolved_channel_id"]
            ch["channel_id_resolved_at"] = t_now.isoformat()
        if rec.get("title_now"):
            ch["title_now"] = rec["title_now"]
            ch["title_checked_at"] = t_now.isoformat()
        hr = rec.get("handle_resolved_id")
        if ch.get("handle") and hr and hr != ch.get("channel_id"):
            ch["handle_maps_to"] = hr
            ch["handle_drift_at"] = t_now.isoformat()

    new_videos.sort(key=lambda v: (v.get("published_sh") or ""), reverse=True)

    status = "ok"
    note = None
    fallback = None
    if channels_ok == 0:
        status = "failed"
        fallback = load_json(LAST_OK_FILE, None)
        note = ("本轮全部频道抓取失败（YouTube 对部分 runner IP 段返回 404），"
                "已回退到最近一次成功采集结果"
                + ("（{}）".format(fallback.get("run_time_sh"))
                   if fallback else "（无历史成功记录）"))
        if fallback:
            new_videos = fallback.get("videos") or []
    elif failures:
        status = "degraded"
        note = "{} 个频道抓取失败，其余正常".format(len(failures))

    catchup = any((t_now - w) > timedelta(hours=26) for w in windows) if windows else False

    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()
    webhook_result = {"configured": bool(webhook), "posted": False,
                      "response": None, "note": None}
    if status == "failed":
        webhook_result["note"] = "本轮采集失败，未推送"
    elif not new_videos:
        webhook_result["note"] = "无新视频，未推送"
    elif not webhook:
        webhook_result["note"] = "未配置 FEISHU_WEBHOOK_URL，仅落盘"
    else:
        try:
            webhook_result["response"] = collect.post_feishu(
                webhook, secret, collect.build_feishu_text(new_videos))
            webhook_result["posted"] = True
        except Exception as e:
            webhook_result["note"] = "{}: {}".format(type(e).__name__, e)

    if len(reported) > collect.MAX_REPORTED:
        for k in sorted(reported, key=reported.get)[
                :len(reported) - collect.MAX_REPORTED]:
            reported.pop(k)
    state["reported"] = reported
    state["last_success"] = t_now.isoformat()
    state["last_full_success"] = (t_now.isoformat() if channels_ok
                                  else state.get("last_full_success"))

    latest = {
        "run_time": t_now.isoformat(),
        "run_time_sh": fmt_sh(t_now),
        "window_start_sh": fmt_sh(min(windows)) if windows else "",
        "channels_enabled": sum(1 for c in channels if c.get("enabled", True)),
        "channels_ok": channels_ok,
        "runner_count": len(runs),
        "new_count": len(new_videos),
        "videos": new_videos,
        "failures": failures,
        "status": status,
        "catchup": catchup,
        "sources": sources,
        "note": note,
        "webhook": webhook_result,
    }

    save_json(STATE_FILE, state)
    save_json(LATEST_FILE, latest)
    if channels_ok:
        save_json(LAST_OK_FILE, {"run_time": t_now.isoformat(),
                                 "run_time_sh": fmt_sh(t_now),
                                 "videos": new_videos,
                                 "channels_ok": channels_ok})
    save_json(CHANNELS_FILE, channels)

    try:
        committed = commit_and_push(
            "monitor: {} new, {} ok, {} failures ({})".format(
                len(new_videos), channels_ok, len(failures), status))
    except Exception as e:
        print("git 提交失败: {}".format(e))
        committed = False

    print(json.dumps({"status": status, "new": len(new_videos),
                      "channels_ok": channels_ok,
                      "failures": len(failures), "catchup": catchup,
                      "committed": committed}, ensure_ascii=False))
    if status == "failed":
        print("全部频道失败：工作流将以失败状态结束，便于及时发现")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
