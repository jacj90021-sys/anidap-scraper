from flask import Flask, jsonify, request
import json
import re
import urllib.parse
import urllib.request

app = Flask(__name__)

BASE = "https://anidap.lol"
CDN = "https://chad.anidap.lol"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def http_get(url, headers=None):
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/html, */*"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "ignore"), r.status


def api_get(path, referer=None):
    url = f"{BASE}/api/{path}"
    hdrs = {"Referer": referer or f"{BASE}/home"} if referer else {}
    body, _ = http_get(url, hdrs or None)
    return json.loads(body)


def cdn_get(path, referer=None):
    """Fetch from the chad.anidap.lol rest API (servers/sources live here, NOT
    anidap.lol — anidap.lol/rest/api/* 404s; chad.anidap.lol/rest/api/* works)."""
    url = f"{CDN}/{path}"
    hdrs = {"Referer": referer or f"{BASE}/home", "Accept": "application/json"}
    body, _ = http_get(url, hdrs)
    return json.loads(body)


def slug_for_id(numeric_id):
    """Resolve numeric anilist id → anidap slug.

    anidap.lol/info/<id> embeds the slug in its page JSON as:
        ...,"requestedId","<id>","id","<slug>","anilistId",<id>,...
    Parse that directly (no dependency on a separate API host that may block
    the scraper's egress IP). Fall back to the /api/anime/<id> JSON endpoint.
    """
    try:
        body, _ = http_get(f"{BASE}/info/{numeric_id}", {"Referer": f"{BASE}/home"})
        # anidap embeds the page data as a DOUBLE-ESCAPED JSON string, so the
        # markers appear as  \"requestedId\",\"<id>\",\"id\",\"<slug>\"
        m = re.search(
            r'\\?"requestedId\\?",\\?"?%s\\"?\\?,\\?"id\\?",\\?"([A-Za-z0-9_-]+)\\?"' % re.escape(str(numeric_id)),
            body,
        )
        if m:
            return m.group(1)
        # looser: \"id\",\"<slug>\",\"anilistId\",<id>
        m2 = re.search(
            r'\\?"id\\?",\\?"([A-Za-z0-9_-]+)\\?",\\?"anilistId\\?",%s' % re.escape(str(numeric_id)),
            body,
        )
        if m2:
            return m2.group(1)
    except Exception:
        pass
    # fallback: dedicated anime API endpoint
    try:
        data, _ = http_get(f"{BASE}/api/anime/{numeric_id}", {"Referer": f"{BASE}/home"})
        j = json.loads(data)
        if j.get("success") and j.get("data", {}).get("id"):
            return j["data"]["id"]
    except Exception:
        pass
    return None


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/recents")
def recents():
    data = api_get("anime/recents", f"{BASE}/home")
    results = _extract_results(data)
    return jsonify({"count": len(results), "results": results})


@app.route("/api/trending")
def trending():
    data = api_get("anime/trending", f"{BASE}/home")
    results = _extract_results(data)
    return jsonify({"count": len(results), "results": results})


@app.route("/api/search")
def search():
    q = request.args.get("q", "")
    data = api_get(f"anime/search?q={urllib.parse.quote(q)}", f"{BASE}/home")
    results = _extract_results(data)
    return jsonify({"count": len(results), "query": q, "results": results})


@app.route("/api/info/<int:anilist_id>")
def info(anilist_id):
    slug = slug_for_id(str(anilist_id))
    if not slug:
        return jsonify({"error": "slug not found", "id": anilist_id}), 404
    # also try the API for details
    try:
        data = api_get(f"anime/{slug}", f"{BASE}/home")
    except Exception:
        data = {}
    return jsonify({"id": anilist_id, "slug": slug, "api_data": data})


@app.route("/api/stream")
def stream():
    slug = request.args.get("slug")
    ep = request.args.get("ep", "1")
    provider = request.args.get("provider")  # beep, mimi, yuki, sora, zoro
    stream_type = request.args.get("type", "sub")  # sub | dub
    anilist_id = request.args.get("id")       # numeric id fallback

    if not slug and anilist_id:
        slug = slug_for_id(anilist_id)
    if not slug:
        return jsonify({"error": "provide slug or id"}), 400

    # get available servers (lives on chad.anidap.lol, not anidap.lol)
    try:
        servers_data = cdn_get(
            f"rest/api/servers?id={slug}&epNum={ep}",
            f"{BASE}/home"
        )
    except Exception as e:
        return jsonify({"error": f"servers fetch failed: {e}"}), 502

    sub_providers = servers_data.get("subProviders", [])
    if not sub_providers:
        return jsonify({"error": "no servers found", "slug": slug, "ep": ep}), 404

    # select the provider pool for the requested audio type
    if stream_type == "dub":
        pool = servers_data.get("dubProviders", []) or sub_providers
    else:
        pool = sub_providers

    # build provider list
    all_providers = []
    for p in pool:
        all_providers.append({
            "id": p["id"],
            "default": p.get("default", False),
            "tip": p.get("tip", ""),
        })

    # pick provider: user-specified > default > first (from the chosen audio pool)
    chosen = None
    if provider:
        chosen = next((p for p in pool if p["id"] == provider), None)
    if not chosen:
        chosen = next((p for p in pool if p.get("default")), pool[0])

    pid = chosen["id"]

    # get sources (m3u8) from chad CDN — honor the requested audio type
    try:
        sources_data, status = http_get(
            f"{CDN}/rest/api/sources?id={slug}&epNum={ep}&type={stream_type}&providerId={pid}",
            {"Referer": f"{BASE}/home"}
        )
        sources = json.loads(sources_data)
    except Exception as e:
        return jsonify({"error": f"sources fetch failed: {e}", "slug": slug, "ep": ep, "provider": pid}), 502

    source_list = sources.get("sources", [])
    if not source_list:
        return jsonify({"error": "no sources returned", "slug": slug, "ep": ep, "provider": pid}), 404

    m3u8_url = source_list[0].get("url", "")
    quality = source_list[0].get("quality", "unknown")

    # determine the referer needed for the m3u8 CDN
    parsed = urllib.parse.urlparse(m3u8_url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"

    return jsonify({
        "slug": slug,
        "episode": ep,
        "provider": pid,
        "providers": [p["id"] for p in sub_providers],
        "quality": quality,
        "m3u8": m3u8_url,
        "referer": referer,
        "all_sources": [{"url": s.get("url"), "quality": s.get("quality"), "type": s.get("type")} for s in source_list],
    })


@app.route("/api/servers")
def servers():
    slug = request.args.get("slug")
    ep = request.args.get("ep", "1")
    if not slug:
        return jsonify({"error": "provide slug"}), 400
    try:
        data = cdn_get(f"rest/api/servers?id={slug}&epNum={ep}", f"{BASE}/home")
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(data)


def _extract_results(data):
    """Pull result list from various anidap API response shapes."""
    if isinstance(data.get("results"), list):
        return _normalize(data["results"])
    inner = data.get("data", data)
    if isinstance(inner, dict):
        if isinstance(inner.get("results"), list):
            return _normalize(inner["results"])
        inner2 = inner.get("data")
        if isinstance(inner2, list):
            return _normalize(inner2)
        if isinstance(inner2, dict) and isinstance(inner2.get("results"), list):
            return _normalize(inner2["results"])
    if isinstance(inner, list):
        return _normalize(inner)
    return []


def _normalize(results):
    out = []
    for r in results:
        slug = r.get("slug") or r.get("id") or ""
        title = r.get("title") or r.get("name") or r.get("anime") or ""
        if isinstance(title, dict):
            title = title.get("romaji") or title.get("english") or title.get("native") or ""
        out.append({
            "slug": slug,
            "id": r.get("id") or r.get("anilistId") or "",
            "anime": title,
            "episode": r.get("episode") or r.get("ep") or r.get("number") or "",
            "genres": r.get("genres") or r.get("categories") or [],
            "coverImage": r.get("coverImage") or r.get("image") or r.get("cover") or "",
        })
    return out


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(__import__("os").environ.get("PORT", 5000)))
