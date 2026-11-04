#!/usr/bin/env python3
"""merge.py 纯逻辑单测（不联网）：多 runner 汇总 / 优先级 / 去重 / 窗口 / 全失败回退。"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collect  # noqa: E402
import merge  # noqa: E402

passed = 0


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print("PASS  {}".format(name))
    else:
        print("FAIL  {}  {}".format(name, detail))
        sys.exit(1)


def video(vid, published, title="T"):
    return {"video_id": vid, "title": title,
            "url": "https://www.youtube.com/watch?v=" + vid,
            "published": published, "views": "10"}


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# 在临时目录里搭一套工作区，指向 merge.py 的模块级路径
tmp = Path(tempfile.mkdtemp())
orig = (merge.BASE, merge.CHANNELS_FILE, merge.DATA_DIR, merge.STATE_FILE,
        merge.LATEST_FILE, merge.LAST_OK_FILE, merge.RUNS_DIR,
        merge.PENDING_FILE)
merge.BASE = tmp
merge.CHANNELS_FILE = tmp / "channels.json"
merge.DATA_DIR = tmp / "data"
merge.STATE_FILE = tmp / "data" / "state.json"
merge.LATEST_FILE = tmp / "data" / "latest.json"
merge.LAST_OK_FILE = tmp / "data" / "last_ok.json"
merge.PENDING_FILE = tmp / "data" / "pending.json"
merge.RUNS_DIR = tmp / "data" / "runs"

T0 = "2026-11-04T02:00:00+00:00"   # 模拟 run 时刻

# --- 场景 1：两个 runner，一成功一失败，取并集 ---
write(merge.CHANNELS_FILE, [
    {"brand": "A", "channel_name": "A", "channel_id": "UCa", "enabled": True},
    {"brand": "B", "channel_name": "B", "channel_id": "UCb", "enabled": True},
])
write(merge.RUNS_DIR / "run-ubuntu-latest.json", {
    "runner": "ubuntu-latest", "run_time": T0,
    "results": [
        {"key": "UCa", "brand": "A", "source": "rss", "error": None,
         "videos": [video("v1", "2026-11-04T01:00:00+00:00")]},
        {"key": "UCb", "brand": "B", "source": "rss",
         "error": "HTTP 404", "videos": []},
    ]})
write(merge.RUNS_DIR / "run-ubuntu-24.04.json", {
    "runner": "ubuntu-24.04", "run_time": T0,
    "results": [
        {"key": "UCa", "brand": "A", "source": "rss", "error": None,
         "videos": [video("v1", "2026-11-04T01:00:00+00:00")]},
        {"key": "UCb", "brand": "B", "source": "rss", "error": None,
         "videos": [video("v2", "2026-11-04T00:30:00+00:00")]},
    ]})
os.environ.pop("CATCHUP_FROM", None)
rc = merge.main()
latest = json.loads(merge.LATEST_FILE.read_text(encoding="utf-8"))
check("多 runner 汇总：B 频道由另一个 runner 补齐", latest["channels_ok"] == 2,
      str(latest["channels_ok"]))
check("多 runner 汇总：状态 degraded 变 ok（两频道都成功）",
      latest["status"] == "ok", latest["status"])
check("多 runner 汇总：两条新视频", latest["new_count"] == 2,
      str(latest["new_count"]))
ids = sorted(v["video_id"] for v in latest["videos"])
check("多 runner 汇总：视频 ID 正确", ids == ["v1", "v2"], str(ids))

# --- 场景 2：重复上报去重（条目进入待投递队列后仍对日报可见，但不再算新采） ---
rc = merge.main()
latest2 = json.loads(merge.LATEST_FILE.read_text(encoding="utf-8"))
check("去重：同一批视频不再作为新采上报", latest2["fresh_count"] == 0,
      str(latest2["fresh_count"]))
check("待投递：条目仍保留在 latest.json 供日报消费",
      latest2["new_count"] == 2 and latest2["carry_count"] == 2,
      "{} / {}".format(latest2["new_count"], latest2["carry_count"]))
check("待投递：pending.json 已落盘",
      merge.PENDING_FILE.exists() and
      len(json.loads(merge.PENDING_FILE.read_text(encoding="utf-8"))["items"]) == 2)
chans = json.loads(merge.CHANNELS_FILE.read_text(encoding="utf-8"))
check("回写：成功频道推进 last_ok", all(c.get("last_ok") for c in chans),
      str(chans))

# --- 场景 3：全部失败 → status=failed 且回退到上次成功结果 ---
last_ok_before = {c.get("brand"): c.get("last_ok") for c in json.loads(
    merge.CHANNELS_FILE.read_text(encoding="utf-8"))}
write(merge.RUNS_DIR / "run-ubuntu-latest.json", {
    "runner": "ubuntu-latest", "run_time": T0,
    "results": [{"key": "UCa", "brand": "A", "source": "rss",
                 "error": "HTTP 404", "videos": []},
                {"key": "UCb", "brand": "B", "source": "rss",
                 "error": "HTTP 404", "videos": []}]})
write(merge.RUNS_DIR / "run-ubuntu-24.04.json", {
    "runner": "ubuntu-24.04", "run_time": T0,
    "results": [{"key": "UCa", "brand": "A", "source": "rss",
                 "error": "HTTP 404", "videos": []},
                {"key": "UCb", "brand": "B", "source": "rss",
                 "error": "HTTP 404", "videos": []}]})
# 清掉 last_ok，模拟"从未成功过"的极端情况
write(merge.LAST_OK_FILE, {})
rc = merge.main()
latest3 = json.loads(merge.LATEST_FILE.read_text(encoding="utf-8"))
check("全失败：status=failed", latest3["status"] == "failed",
      latest3["status"])
check("全失败：退出码非 0（工作流转红）", rc == 1, str(rc))
check("全失败：note 说明回退", "回退" in (latest3.get("note") or ""),
      str(latest3.get("note")))
chans = json.loads(merge.CHANNELS_FILE.read_text(encoding="utf-8"))
last_ok_after = {c.get("brand"): c.get("last_ok") for c in chans}
check("全失败：失败不推进也不清空 last_ok（窗口不被吃掉）",
      last_ok_after == last_ok_before,
      "{} -> {}".format(last_ok_before, last_ok_after))

# --- 场景 4：来源优先级 rss > rss_proxy > innertube ---
recs = [
    {"key": "k", "source": "innertube", "error": None,
     "videos": [video("i1", "2026-11-04T01:00:00+00:00")]},
    {"key": "k", "source": "rss", "error": None,
     "videos": [video("r1", "2026-11-04T01:00:00+00:00")]},
]
check("来源优先级：rss 胜过 innertube",
      merge.better(recs[1], recs[0]) and not merge.better(recs[0], recs[1]))
check("来源优先级：成功记录胜过失败记录",
      merge.better({"source": "innertube", "error": None, "videos": []},
                   {"source": "rss", "error": "HTTP 404", "videos": []}))

# --- 场景 5：CATCHUP_FROM 追平窗口 ---
write(merge.CHANNELS_FILE, [
    {"brand": "A", "channel_name": "A", "channel_id": "UCa", "enabled": True}])
write(merge.RUNS_DIR / "run-ubuntu-latest.json", {
    "runner": "ubuntu-latest", "run_time": T0,
    "results": [{"key": "UCa", "brand": "A", "source": "rss", "error": None,
                 "videos": [video("old1", "2026-10-31T01:00:00+00:00"),
                            video("new1", "2026-11-04T01:00:00+00:00")]}]})
os.remove(merge.RUNS_DIR / "run-ubuntu-24.04.json")
os.environ["CATCHUP_FROM"] = "2026-10-29T09:00:00+00:00"
rc = merge.main()
latest5 = json.loads(merge.LATEST_FILE.read_text(encoding="utf-8"))
ids5 = sorted(v["video_id"] for v in latest5["videos"])
check("追平：窗口回溯后旧视频被捞回", "old1" in ids5 and "new1" in ids5, str(ids5))
check("追平：catchup 标记为 true", latest5["catchup"] is True,
      str(latest5["catchup"]))
os.environ.pop("CATCHUP_FROM", None)

# --- 场景 6：待投递队列生命周期（回补条目跨轮次可见，日报消费后清理） ---
real_now = merge.now_utc
try:
    # 隔离本场景：清空待投递队列与已上报集合
    write(merge.PENDING_FILE, {"items": {}})
    write(merge.STATE_FILE, {"reported": {}, "last_success": None})
    write(merge.CHANNELS_FILE, [
        {"brand": "A", "channel_name": "A", "channel_id": "UCa",
         "enabled": True}])
    write(merge.RUNS_DIR / "run-ubuntu-latest.json", {
        "runner": "ubuntu-latest", "run_time": "2026-11-04T09:30:00+00:00",
        "results": [{"key": "UCa", "brand": "A", "source": "rss",
                     "error": None,
                     "videos": [video("vcarry", "2026-11-04T09:15:00+00:00")]}]})
    if (merge.RUNS_DIR / "run-ubuntu-24.04.json").exists():
        os.remove(merge.RUNS_DIR / "run-ubuntu-24.04.json")
    # T1 = 2026-11-04 17:30 北京（09:30 UTC）：采到 vcarry
    merge.now_utc = lambda: datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc)
    rc = merge.main()
    l1 = json.loads(merge.LATEST_FILE.read_text(encoding="utf-8"))
    check("待投递：首次采集计入新采", l1["fresh_count"] == 1 and l1["carry_count"] == 0,
          "{} / {}".format(l1["fresh_count"], l1["carry_count"]))
    # T2 = 2026-11-05 08:30 北京（00:30 UTC）：日报（09:00）还没到，条目须继续可见
    merge.now_utc = lambda: datetime(2026, 11, 5, 0, 30, tzinfo=timezone.utc)
    rc = merge.main()
    l2 = json.loads(merge.LATEST_FILE.read_text(encoding="utf-8"))
    check("待投递：日报消费前条目不丢失",
          l2["new_count"] == 1 and l2["carry_count"] == 1 and l2["fresh_count"] == 0,
          "{} / {} / {}".format(l2["new_count"], l2["carry_count"], l2["fresh_count"]))
    # T3 = 2026-11-05 11:30 北京（03:30 UTC）：09:00 日报 + 2 小时宽限已过 → 清理
    merge.now_utc = lambda: datetime(2026, 11, 5, 3, 30, tzinfo=timezone.utc)
    rc = merge.main()
    l3 = json.loads(merge.LATEST_FILE.read_text(encoding="utf-8"))
    check("待投递：日报消费后条目清理",
          l3["new_count"] == 0 and l3["carry_count"] == 0,
          "{} / {}".format(l3["new_count"], l3["carry_count"]))
    pend = json.loads(merge.PENDING_FILE.read_text(encoding="utf-8"))
    check("待投递：pending 队列已清空", len(pend["items"]) == 0, str(pend["items"]))
finally:
    merge.now_utc = real_now

# 还原模块路径
(merge.BASE, merge.CHANNELS_FILE, merge.DATA_DIR, merge.STATE_FILE,
 merge.LATEST_FILE, merge.LAST_OK_FILE, merge.RUNS_DIR,
 merge.PENDING_FILE) = orig
shutil.rmtree(tmp, ignore_errors=True)

print("\n{} 项全部通过".format(passed))
