#!/usr/bin/env python3
"""读取 GitHub 仓库里的 YouTube 采集结果（data/latest.json），供每日日报消费。

仓库公开，走 api.github.com contents 接口即可，无需鉴权（不要用 raw.githubusercontent.com，沙箱内不稳定）。
用法：python3 fetch_latest.py            # 打印人类可读摘要
     python3 fetch_latest.py --json     # 打印原始 JSON
"""
import base64
import json
import sys
import urllib.request

API = "https://api.github.com/repos/AlanRexin/youtube-monitor/contents/data/latest.json"
UA = "Mozilla/5.0 (compatible; youtube-monitor-fetch/1.0)"


def fetch():
    req = urllib.request.Request(API, headers={"User-Agent": UA,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return json.loads(base64.b64decode(payload["content"]).decode("utf-8"))


def main():
    try:
        data = fetch()
    except Exception as exc:
        print("FETCH_FAILED: {}: {}".format(type(exc).__name__, exc))
        return 1

    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    vids = data.get("videos") or []
    fails = data.get("failures") or []
    print("run_time_sh: {}".format(data.get("run_time_sh")))
    print("window: {} ~ {}".format(data.get("window_start_sh"), data.get("run_time_sh")))
    print("channels_enabled: {}".format(data.get("channels_enabled")))
    print("new_count: {}".format(data.get("new_count")))
    print("failures: {}".format(len(fails)))
    for f in fails:
        print("  - {}".format(f))
    hook = data.get("webhook") or {}
    print("webhook_configured: {}".format(hook.get("configured")))
    print("--- videos ---")
    for v in vids:
        print("[{}] {} | {} | {} | views={} | {}".format(
            v.get("brand"), (v.get("title") or "")[:60], v.get("channel"),
            v.get("published_sh"), v.get("views"), v.get("url")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
