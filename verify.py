#!/usr/bin/env python3
"""一次性频道验证脚本（runner 上跑）：对候选 UC id 抓频道页标题/描述/订阅数 + RSS 最近视频。"""
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "verify.json"

CANDIDATES = {
    "UCRSJ_cRVj4XvHERiHdkIXoA": "Omega Engineering Inc.(搜索)",
    "UC9bRLkDgDpxM9JdluFlW3Eg": "DwyerOmega(搜索，OMEGA 已并入)",
    "UCeDLFe-m9BFua9h7mVUmj8Q": "Omega Engineering Development(搜索)",
    "UCwY6FJ_pUKasM-5tFOtCmjw": "Testo USA(搜索)",
}


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": collect.UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "ignore")


def main():
    out = {}
    for cid, label in CANDIDATES.items():
        info = {"label": label}
        try:
            html = fetch_text("https://www.youtube.com/channel/{}".format(cid))
            t = re.search(r'<meta property="og:title" content="([^"]*)"', html)
            d = re.search(r'<meta property="og:description" content="([^"]*)"', html)
            subs = re.search(r'"subscriberCountText":\{"simpleText":"([^"]*)"', html)
            if not subs:
                subs = re.search(r'([\d.,KM]+) subscribers', html)
            info.update(
                og_title=t.group(1) if t else None,
                description=(d.group(1)[:200] if d else None),
                subscribers=subs.group(1) if subs else None,
            )
        except Exception as exc:
            info["page_error"] = "{}: {}".format(type(exc).__name__, exc)
        try:
            vids = collect.fetch_feed_videos(cid)
            info["feed_title_recent"] = [
                {"title": v["title"][:80], "published": v["published"]}
                for v in vids[:3]]
            info["video_count_in_feed"] = len(vids)
        except Exception as exc:
            info["feed_error"] = "{}: {}".format(type(exc).__name__, exc)
        out[cid] = info

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        collect.git_commit_all("verify: channel candidates detail")
    except Exception as exc:
        print("git 提交失败: {}".format(exc), file=sys.stderr)
    print("done: {} candidates".format(len(out)))


if __name__ == "__main__":
    main()
