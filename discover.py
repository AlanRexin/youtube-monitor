#!/usr/bin/env python3
"""一次性频道发现脚本（在 GitHub Actions runner 上跑）：
1) 探测候选 handle/user 是否存在（复用 collect.py 的解析逻辑）
2) 抓 YouTube 搜索结果页（频道过滤）提取官方频道候选
结果写 data/discovery.json 并 commit。"""
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "discovery.json"

CANDIDATES = {
    "Beamex": ["Beamex", "BeamexOy", "Beamexfi", "beamex"],
    "WIKA": ["wikagroup", "WIKAGroup", "WIKAGlobal", "wika", "WIKAGermany"],
    "AMETEK Calibration (JOFRA)": ["AMETEKCalibration", "JOFRAcalibration",
                                   "JOFRA", "ametekcalibration"],
    "Druck": ["druck", "DruckLtd", "GEDruck", "drucksensors"],
}

SEARCH_QUERIES = ["beamex", "wika", "ametek jofra calibration", "druck baker hughes"]

INITIAL_RE = re.compile(r"var ytInitialData\s*=\s*(\{.*?\});", re.S)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "ignore")


def walk_channel_ids(obj, out):
    """递归抽 ytInitialData 里 channelRenderer / lockupViewModel 的频道信息。"""
    if isinstance(obj, dict):
        cid = obj.get("channelId") or (obj.get("contentId") if isinstance(obj.get("contentId"), str) else None)
        if isinstance(cid, str) and cid.startswith("UC") and len(cid) == 24:
            title = None
            t = obj.get("title")
            if isinstance(t, dict):
                title = (t.get("simpleText")
                         or (t.get("runs") or [{}])[0].get("text"))
            tv = obj.get("metadata")
            if title is None and isinstance(tv, dict):
                m = tv.get("channelMetadataRenderer")
                if isinstance(m, dict):
                    title = m.get("title")
            if cid not in [o["channel_id"] for o in out]:
                out.append({"channel_id": cid, "title": title})
        for v in obj.values():
            walk_channel_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_channel_ids(v, out)


def main():
    result = {"probes": [], "search": {}}
    for brand, cands in CANDIDATES.items():
        for h in cands:
            entry = {"brand": brand, "handle": h}
            try:
                html = fetch_html("https://www.youtube.com/@{}".format(h))
                m = re.search(r'"(?:externalId|channelId)":"(UC[A-Za-z0-9_-]{22})"', html)
                tm = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                if m:
                    entry.update(channel_id=m.group(1),
                                 page_title=tm.group(1) if tm else None,
                                 exists=True)
                else:
                    entry.update(exists=False, note="页面存在但无 channel_id")
            except Exception as exc:
                entry.update(exists=False, error="{}: {}".format(type(exc).__name__, exc))
            result["probes"].append(entry)
        # 老式 user 名也试一遍
        try:
            xml = collect.http_get(
                "https://www.youtube.com/feeds/videos.xml?user={}".format(
                    urllib.parse.quote(brand.split()[0].lower()))
            )
            root = collect.ET.fromstring(xml)
            title = root.findtext("{}title".format(collect.ATOM))
            result["probes"].append({"brand": brand, "user": brand.split()[0].lower(),
                                     "exists": True, "page_title": title})
        except Exception:
            pass

    for q in SEARCH_QUERIES:
        try:
            html = fetch_html("https://www.youtube.com/results?search_query={}&sp=EgIQAg%3D%3D".format(
                urllib.parse.quote(q)))
            m = INITIAL_RE.search(html)
            found = []
            if m:
                try:
                    data = json.loads(m.group(1))
                    walk_channel_ids(data, found)
                except Exception as exc:
                    found.append({"channel_id": None, "title": "JSON 解析失败: {}".format(exc)})
            else:
                found.append({"channel_id": None, "title": "未找到 ytInitialData（可能被反爬）"})
            result["search"][q] = found[:15]
        except Exception as exc:
            result["search"][q] = [{"channel_id": None,
                                    "title": "抓取失败: {}: {}".format(type(exc).__name__, exc)}]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        collect.git_commit_all("discover: channel candidates")
    except Exception as exc:
        print("git 提交失败: {}".format(exc), file=sys.stderr)
    print("done: probes={}, queries={}".format(len(result["probes"]), len(result["search"])))


if __name__ == "__main__":
    main()
