#!/usr/bin/env python3
"""collect.py 纯逻辑单测（不联网）：RSS 解析 / 时间窗 / 去重 / 飞书签名 / 消息文本。"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collect  # noqa: E402

FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <link rel="self" href="https://www.youtube.com/feeds/videos.xml?channel_id=UCtest"/>
  <title>Test Channel</title>
  <entry>
    <id>yt:video:aaa111</id>
    <yt:videoId>aaa111</yt:videoId>
    <title>新压力校验仪发布</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=aaa111"/>
    <published>2026-10-29T02:00:00+00:00</published>
    <updated>2026-10-29T02:00:00+00:00</updated>
    <media:group>
      <media:title>新压力校验仪发布</media:title>
      <media:statistics views="1234"/>
    </media:group>
  </entry>
  <entry>
    <id>yt:video:bbb222</id>
    <yt:videoId>bbb222</yt:videoId>
    <title>三天前的旧视频</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=bbb222"/>
    <published>2026-10-26T02:00:00+00:00</published>
    <updated>2026-10-26T02:00:00+00:00</updated>
  </entry>
  <entry>
    <id>yt:video:ccc333</id>
    <yt:videoId>ccc333</yt:videoId>
    <title>昨天发布但已上报过</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=ccc333"/>
    <published>2026-10-28T10:00:00+00:00</published>
    <updated>2026-10-28T10:00:00+00:00</updated>
  </entry>
</feed>
"""

passed = 0


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print("PASS  {}".format(name))
    else:
        print("FAIL  {}  {}".format(name, detail))
        sys.exit(1)


# 1. RSS 解析
videos = collect.parse_feed(FIXTURE.encode("utf-8"))
check("RSS 解析出 3 条", len(videos) == 3, str(len(videos)))
check("解析字段完整", videos[0]["video_id"] == "aaa111"
      and videos[0]["title"] == "新压力校验仪发布"
      and videos[0]["url"].endswith("v=aaa111")
      and videos[0]["views"] == "1234"
      and videos[1]["views"] is None)

# 2. 时间窗 + 去重（以 2026-10-29 08:00 UTC 为“现在”）
t_now = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
ws = collect.compute_window_start("2026-10-28T08:00:00+00:00", t_now)
check("常规窗口 = now-25h", ws == t_now - timedelta(hours=25), str(ws))
ws_gap = collect.compute_window_start("2026-10-24T08:00:00+00:00", t_now)
check("漏跑多日窗口拉宽", ws_gap == datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc), str(ws_gap))
check("首次运行窗口 = now-25h",
      collect.compute_window_start(None, t_now) == t_now - timedelta(hours=25))

reported = {"ccc333": "2026-10-28"}
got = collect.filter_new(videos, ws, reported)
ids = [v["video_id"] for v in got]
check("过滤：只要窗口内未上报的", ids == ["aaa111"], str(ids))
check("补 published_sh 字段", got and got[0]["published_sh"] == "2026-10-29 10:00", got[0]["published_sh"] if got else "")

# 3. 去重集合：同一视频再次出现不再报
got2 = collect.filter_new(videos, ws, reported | {"aaa333": "", "aaa111": "2026-10-29"})
check("已上报视频被跳过", [v["video_id"] for v in got2] == [], str(got2))

# 4. 飞书签名格式（算法正确性以官方文档为准，这里验证可运行、输出为 base64）
import base64 as b64
sig = collect.feishu_sign("testsecret", "1766688000")
check("飞书签名输出合法 base64", bool(b64.b64decode(sig)))

# 5. 推送文本
items = [{"brand": "Fluke Calibration 福禄克校准", "title": "New 8270A video",
          "published_sh": "2026-10-29 09:00", "url": "https://www.youtube.com/watch?v=x"}]
txt = collect.build_feishu_text(items)
check("推送文本含品牌/标题/链接", "Fluke Calibration" in txt and "New 8270A video" in txt and "https://www.youtube.com/watch?v=x" in txt)

# 6. channels.json 合法且 15 个频道
chs = json.loads((Path(__file__).parent / "channels.json").read_text(encoding="utf-8"))
check("channels.json 共 15 个频道", len(chs) == 15, str(len(chs)))
check("启用频道有品牌和定位键", all(
    c.get("brand") and (c.get("handle") or c.get("user") or c.get("channel_id"))
    for c in chs if c.get("enabled", True)))
check("停用条目必须显式标注原因", all(
    c.get("note") for c in chs if not c.get("enabled", True)))

print("\n{} 项全部通过".format(passed))
