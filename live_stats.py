"""Live YouTube numbers for the dashboard.

Runs every ~20 minutes in the public dashboard repo (GitHub Actions minutes are free for public
repos), all day. Reads data.json for our videos and writes live.json, which the page lays over
data.json. Sources, best first:

1. The public Shorts page: the exact view and like counts YouTube shows people. (For Shorts the
   Data API reports the older "engaged views", which is lower, and RSS lags by hours.)
2. The YouTube Data API, hourly: comments and subscriber counts, and views/likes for any video
   whose page could not be read. It shares the 10,000-unit daily quota with uploads (1,600 + 50
   per Short), so it only spends what the upload cap leaves free (10,000 - 6 x 1,650 = 100 units
   a day; one call costs about 2).
3. The public RSS feed (free, but hours behind): only ever raises a number, never lowers it.

This public repo holds no secrets, so step 2 normally doesn't run here: the private YTAutomation
tick fetches comments/subscribers hourly with the channel login. (Set YT_API_KEY, or
YT_CLIENT_ID/YT_CLIENT_SECRET/YT_REFRESH_TOKEN, in the environment to enable it.)
"""

import datetime as dt
import html
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://www.googleapis.com/youtube/v3/"
UPLOAD_UNITS, DAILY_UNITS = 1650, 10_000
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
HERE = Path(__file__).resolve().parent


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(title):
    return re.sub(r"[^a-z0-9]+", "", html.unescape(title or "").lower())


def fetch(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0 (live stats)", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def auth():
    """(headers, extra params) for the Data API, or None when no login/key is set."""
    if os.environ.get("YT_API_KEY"):
        return {}, {"key": os.environ["YT_API_KEY"]}
    cid, sec, rt = (os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))
    if not (cid and sec and rt):
        return None
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": sec, "refresh_token": rt,
                                   "grant_type": "refresh_token"}).encode()
    tok = json.loads(fetch("https://oauth2.googleapis.com/token", data=body))["access_token"]
    return {"Authorization": f"Bearer {tok}"}, {}


def api(path, creds, **params):
    headers, extra = creds
    return json.loads(fetch(API + path + "?" + urllib.parse.urlencode(params | extra), headers=headers))


PAGE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/124.0 Safari/537.36")


def page(vid):
    """(views, likes) shown on the public Shorts page, or None."""
    h = fetch(f"https://www.youtube.com/shorts/{vid}", headers={"User-Agent": PAGE_UA, "Accept-Language": "en-US,en;q=0.9"})
    views = re.search(r'"viewCount":"(\d+)"', h)
    if not views:
        return None
    likes = re.search(r'along with ([\d,]+) other', h) or re.search(r'"likeCount":"?(\d+)', h)
    return int(views.group(1)), int(likes.group(1).replace(",", "")) if likes else None


def rss(channel_id):
    root = ET.fromstring(fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"))
    out = {}
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        st = e.find("media:group/media:community/media:statistics", NS)
        rt = e.find("media:group/media:community/media:starRating", NS)
        out[vid] = {"title": e.findtext("a:title", namespaces=NS),
                    "views": int(st.get("views", 0)) if st is not None else 0,
                    "likes": int(rt.get("count", 0)) if rt is not None else 0}
    return out


def main():
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    day = now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    data = json.loads((HERE / "data.json").read_text(encoding="utf-8"))
    path = HERE / "live.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    units = old.get("units") if (old.get("units") or {}).get("day") == day else {"day": day, "used": 0}

    chans = {k: c["channel_id"] for k, c in data["channels"].items() if c.get("enabled") and c.get("channel_id")}
    ids, titles, from_rss = set(), dict(old.get("titles", {})), {}
    for k in chans:
        for p in data["channels"][k].get("published", []):
            if p.get("youtube_id"):
                ids.add(p["youtube_id"])
    for k, cid in chans.items():  # free: finds brand-new uploads and gives fallback numbers
        try:
            for vid, v in rss(cid).items():
                ids.add(vid)
                titles[norm(v["title"])] = vid
                from_rss[vid] = v
        except Exception as exc:  # noqa: BLE001
            print(f"rss {k}: {exc}")

    ids = sorted(ids)
    videos = {vid: dict(v) for vid, v in old.get("videos", {}).items() if vid in ids}
    channels = dict(old.get("channels", {}))
    out = {"api_at": old.get("api_at")}

    # 1. public pages: newest videos every run, older ones in rotation (keeps each run ~1 minute)
    recent = {p["youtube_id"] for k in chans for p in data["channels"][k].get("published", [])
              if p.get("youtube_id") and (now - dt.datetime.fromisoformat(p["published_at"].replace("Z", "+00:00"))).days < 7}
    recent |= set(from_rss) - set(videos)
    older = sorted((v for v in ids if v not in recent), key=lambda v: videos.get(v, {}).get("page_at") or "")
    todo, failed = sorted(recent) + older[:20], 0

    def safe_page(vid):
        try:
            return page(vid)
        except Exception as exc:  # noqa: BLE001
            print(f"page {vid}: {exc}")
            return None

    probe = [safe_page(v) for v in todo[:3]]  # if the first 3 fail, pages are blocked here: skip
    if todo and not any(probe):
        print("public pages are not readable from here; using the API")
        results = []
    else:
        with ThreadPoolExecutor(6) as pool:
            results = list(zip(todo, probe + list(pool.map(safe_page, todo[3:]))))
    for vid, got in results:
        if not got:
            failed += 1
            continue
        v = videos.setdefault(vid, {})
        v.update(views=max(got[0], v.get("views") or 0), at=iso(now), page_at=iso(now), src="page")
        if got[1] is not None:
            v["likes"] = got[1]

    # 2. Data API: hourly (comments, subscribers), or now if pages failed
    cap = (data.get("budget", {}).get("api") or {}).get("cap", 0)
    allowance = int(os.environ.get("LIVE_UNITS_PER_DAY") or max(0, DAILY_UNITS - cap * UPLOAD_UNITS))
    last = out["api_at"]
    due = not last or (now - dt.datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() > 55 * 60
    stale = [v for v in ids if (videos.get(v) or {}).get("page_at") != iso(now)]
    cost = -(-len(ids) // 50) + 1
    if ids and (due or (stale and (failed or not results))) and units["used"] + cost <= allowance:
        try:
            creds = auth()
            if creds:
                for i in range(0, len(ids), 50):
                    for item in api("videos", creds, part="statistics", id=",".join(ids[i:i + 50]),
                                    maxResults=50).get("items", []):
                        st, v = item.get("statistics", {}), videos.setdefault(item["id"], {})
                        v["comments"] = int(st.get("commentCount", 0))
                        if v.get("page_at") != iso(now):  # page unread: API numbers (never lower one)
                            v.update(views=max(int(st.get("viewCount", 0)), v.get("views") or 0),
                                     likes=max(int(st.get("likeCount", 0)), v.get("likes") or 0), at=iso(now))
                            v.setdefault("src", "api")
                for c in api("channels", creds, part="statistics", id=",".join(chans.values())).get("items", []):
                    k = next(k for k, cid in chans.items() if cid == c["id"])
                    st = c["statistics"]
                    channels[k] = {"subscribers": int(st.get("subscriberCount", 0)),
                                   "views": int(st.get("viewCount", 0)), "videos": int(st.get("videoCount", 0))}
                units["used"] += cost
                out["api_at"] = iso(now)
        except Exception as exc:  # noqa: BLE001
            print(f"api failed: {exc}")

    # 3. RSS: only raises numbers (it lags)
    for vid, r in from_rss.items():
        v = videos.setdefault(vid, {"src": "rss", "at": iso(now)})
        v["views"] = max(r["views"], v.get("views") or 0)
        v["likes"] = max(r["likes"], v.get("likes") or 0)

    src = "page" if any(v.get("page_at") == iso(now) for v in videos.values()) else ("api" if out["api_at"] == iso(now) else "rss")
    out["source"] = src
    out |= {"at": iso(now), "units": units, "allowance": allowance, "videos": videos, "channels": channels,
            "titles": {t: v for t, v in titles.items() if v in ids}}
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"{len(videos)} videos via {out['source']}; units today {units['used']}/{allowance}")


if __name__ == "__main__":
    main()
