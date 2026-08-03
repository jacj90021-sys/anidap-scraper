#!/usr/bin/env python3
"""
anidap.lol  —  index scraper + m3u8 stream resolver  (NO browser needed)
=========================================================================

Both layers are plain HTTP. No Playwright / no headless browser required.

1) CATALOG  (anidap.lol/api/anime/*)
   JSON REST API. It 403s with {"message":"YOUR GAY!"} unless you send a
   browser-like `Referer: https://anidap.lol/home` header. That header is the
   only requirement.
   Working endpoints:
     GET /api/anime/recents?page=N        -> data.data is a BARE list
     GET /api/anime/search?q=<q>&page=N   -> data.results list (paginated)
     GET /api/anime/trending?page=N       -> data.data.results list
   Each item: id (== anilist id), slug (text), title{...}, nextAiringEpisode.episode,
              genres, coverImage.large (or image).

2) STREAM RESOLVER  (chad.anidap.lol/rest/api/*)
   The player resolves the HLS source from a SEPARATE CDN. Plain HTTP works
   (even curl's default UA gets 200). The ONLY real gotcha:
     * the `id` param is the anidap SLUG, not the numeric id
       (numeric id -> {"error":"anime not found"} ; slug -> 200).
   Endpoints:
     GET /rest/api/servers?id=<SLUG>&epNum=<ep>
        -> {"subProviders":[{id,default,tip}],"dubProviders":[...]}
     GET /rest/api/sources?id=<SLUG>&epNum=<ep>&type=sub&providerId=<pid>
        -> {"sources":[{url,quality,type}],"headers":{Referer,User-Agent},"chapters":[...]}
   sources[].url is the .m3u8 master playlist; headers gives the exact
   Referer/UA the final CDN needs to fetch it.

If you only have a numeric id, resolve the slug first:
  slug_for_id() tries /api/anime/recents, /api/anime/search?q=<id>,
  then scrapes the SSR /info/<id> page.

USAGE
  index:
    python3 anidap_stream.py        # recents (default)
    python3 anidap_stream.py --search "frieren" --out frieren
    python3 anidap_stream.py --trending --pages 2

  stream (m3u8):
    python3 anidap_stream.py --stream --slug grand-blue-season-3-e14z3 --ep 5
    python3 anidap_stream.py --stream --id 199111 --ep 5
    python3 anidap_stream.py --stream --slug <slug> --ep 5 --provider yuki

COPYRIGHT: resolves the player's own stream URL (what the site's video player
fetches). Do NOT bolt on a downloader that writes m3u8 segments to disk for
redistribution — that facilitates copying unlicensed video.
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

BASE = "https://anidap.lol"
CDN = "https://chad.anidap.lol"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
REFERER = f"{BASE}/home"


# ---------- catalog ----------
def api_get(path, referer=REFERER):
    req = urllib.request.Request(f"{BASE}/api/{path}", headers={
        "User-Agent": UA, "Referer": referer, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _results_from(d):
    if isinstance(d.get("results"), list):
        return d["results"], d.get("hasNextPage", False)
    inner = d.get("data")
    if isinstance(inner, dict):
        if isinstance(inner.get("results"), list):
            return inner["results"], inner.get("hasNextPage", False)
        i2 = inner.get("data")
        if isinstance(i2, dict):
            if isinstance(i2.get("results"), list):
                return i2["results"], i2.get("hasNextPage", False)
            if isinstance(i2.get("data"), list):        # recents: data.data is a list
                return i2["data"], i2.get("hasNextPage", False)
        if isinstance(i2, list):
            return i2, inner.get("hasNextPage", False)
    return [], False


def slug_for_id(anime_id):
    for attempt in (lambda: api_get(f"anime/recents?page=1"),
                    lambda: api_get(f"anime/search?q={anime_id}&page=1")):
        try:
            for x in _results_from(attempt())[0]:
                if str(x.get("id")) == str(anime_id) and x.get("slug"):
                    return x["slug"]
        except Exception:
            pass
    # SSR info page fallback
    try:
        req = urllib.request.Request(f"{BASE}/info/{anime_id}", headers={
            "User-Agent": UA, "Referer": REFERER})
        h = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        m = re.search(r'([a-z0-9]+(?:-[a-z0-9]+){2,}-e[a-z0-9]{3,5})', h)
        if m:
            return m.group(1)
    except Exception:
        pass
    raise RuntimeError(f"could not resolve slug for id {anime_id}")


# ---------- stream (plain HTTP) ----------
def cdn_get(path):
    url = f"{CDN}/rest/api/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def resolve_stream(slug, ep, provider=None):
    servers = cdn_get(f"servers?id={slug}&epNum={ep}")
    subs = servers.get("subProviders", [])
    if not subs:
        return {"slug": slug, "episode": ep, "error": "no sub providers",
                "raw_servers": servers}
    chosen = next((s for s in subs if s["id"] == provider), None) or \
             next((s for s in subs if s.get("default")), subs[0])
    pid = chosen["id"]
    src = cdn_get(f"sources?id={slug}&epNum={ep}&type=sub&providerId={pid}")
    sources = src.get("sources", [])
    return {
        "slug": slug,
        "episode": ep,
        "providers": [s["id"] for s in subs],
        "chosen_provider": pid,
        "m3u8": sources[0]["url"] if sources else None,
        "quality": sources[0].get("quality") if sources else None,
        "type": sources[0].get("type") if sources else None,
        "required_headers": src.get("headers"),
        "chapters": src.get("chapters"),
    }


# ---------- index fetch ----------
def fetch_index(mode, query, pages):
    rows = []
    for p in range(1, pages + 1):
        if mode == "search":
            d = api_get(f"anime/search?q={urllib.parse.quote(query)}&page={p}")
        elif mode == "trending":
            d = api_get(f"anime/trending?page={p}")
        else:
            d = api_get(f"anime/recents?page={p}")
        data, nxt = _results_from(d)
        rows += data
        if not nxt:
            break
    return rows


def normalize(x):
    t = x.get("title") or {}
    title = (t.get("userPreferred") or t.get("english") or t.get("romaji") or "")
    return {
        "slug": x.get("slug") or "",
        "id": str(x.get("id") or x.get("anilistId") or ""),
        "anime": title,
        "episode": (x.get("nextAiringEpisode") or {}).get("episode", 0),
        "type": x.get("format") or x.get("type") or "",
        "genres": ", ".join(x.get("genres") or []),
        "cover": (x.get("coverImage") or {}).get("large") or x.get("image") or "",
        "url": f"{BASE}/watch?id={x.get('id')}"
               f"&ep={(x.get('nextAiringEpisode') or {}).get('episode', 1)}",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recents", action="store_true")
    ap.add_argument("--trending", action="store_true")
    ap.add_argument("--search")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--out", default="anidap_index")
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--slug")
    ap.add_argument("--id")
    ap.add_argument("--ep", default="1")
    ap.add_argument("--provider")
    args = ap.parse_args()

    if args.stream:
        slug = args.slug
        if not slug:
            if not args.id:
                print("ERROR: --slug or --id required", file=sys.stderr); sys.exit(1)
            slug = slug_for_id(args.id)
            print(f"resolved id {args.id} -> slug {slug}", file=sys.stderr)
        print(json.dumps(resolve_stream(slug, args.ep, args.provider),
                         indent=2, ensure_ascii=False))
        return

    mode = "search" if args.search else ("trending" if args.trending else "recents")
    rows = fetch_index(mode, args.search, args.pages)
    out = []
    seen = set()
    for x in rows:
        r = normalize(x)
        k = (r["id"], r["anime"])
        if k in seen:
            continue
        seen.add(k); out.append(r)
    json.dump(out, open(args.out + ".json", "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    print(f"[{mode}] extracted {len(out)} rows -> {args.out}.json")
    for r in out[:8]:
        print(f"  [{r['id']}] {r['anime']}")


if __name__ == "__main__":
    main()
