#!/usr/bin/env python3
"""anidap.lol Flask API — catalog search + m3u8 stream resolver.

Endpoints (mirrors the anidb-scraper backend shape so the app treats it identically):
  GET /api/health
  GET /api/search?q=<query>          -> {"results":[{id,slug,title,poster,type,...}]}
  GET /api/sources?slug=<slug>&ep=<n>&provider=<opt>
                                    -> {"m3u8":..., "referer":..., "providers":[...], ...}

Internal scraping logic is reused from anidap_stream.py (pure HTTP, no browser).
"""
import json
import re
import urllib.parse
import urllib.request
from flask import Flask, request, jsonify

from anidap_stream import (
    BASE, CDN, UA, REFERER,
    api_get, _results_from, cdn_get, resolve_stream, normalize,
)

app = Flask(__name__)


def extract_slug_from_watch(anime_id):
    """anidap's JSON API omits the slug; the watch page embeds it. Extract best-effort."""
    try:
        req = urllib.request.Request(
            f"{BASE}/watch?id={anime_id}&ep=1",
            headers={"User-Agent": UA, "Referer": REFERER})
        h = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        toks = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+){2,}-e[a-z0-9]{3,5}", h)
        for t in toks:
            if t.count("-") >= 3 and re.search(r"-e[a-z0-9]{3,5}$", t):
                return t
        return toks[0] if toks else None
    except Exception:
        return None



@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "backend": "anidap", "site": BASE})


@app.route("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "missing q"}), 400
    try:
        d = api_get(f"anime/search?q={urllib.parse.quote(q)}&page=1")
        rows, _ = _results_from(d)
        out = []
        seen = set()
        for x in rows:
            r = normalize(x)
            key = (r["id"], r["anime"])
            if key in seen:
                continue
            seen.add(key)
            # anidap search API omits the slug; resolve it best-effort from the
            # watch page so the client can call /api/sources?slug=... directly.
            slug = r["slug"]
            if not slug and r["id"]:
                slug = extract_slug_from_watch(r["id"]) or ""
            out.append({
                "id": r["id"],
                "slug": slug,
                "title": r["anime"],
                "poster": r["cover"],
                "type": r["type"],
                "genres": r["genres"],
                "url": r["url"],
            })
        return jsonify({"results": out})
    except Exception as e:
        return jsonify({"error": "search failed", "details": str(e)}), 500


@app.route("/api/sources")
def sources():
    slug = request.args.get("slug", "").strip()
    anime_id = request.args.get("id", "").strip()
    ep = request.args.get("ep", "1").strip()
    provider = request.args.get("provider", "").strip() or None
    if not slug and not anime_id:
        return jsonify({"error": "missing slug or id"}), 400
    try:
        if not slug:
            slug = slug_for_id(anime_id)
        res = resolve_stream(slug, ep, provider)
        m3u8 = res.get("m3u8")
        if not m3u8:
            return jsonify({
                "error": "no m3u8 resolved",
                "source": "anidap",
                "slug": res.get("slug"),
                "episode": res.get("episode"),
                "providers": res.get("providers"),
                "details": "anidap returned no playable source for this slug/episode",
                "raw": res,
            }), 404
        return jsonify({
            "source": "anidap",
            "slug": res.get("slug"),
            "episode": res.get("episode"),
            "providers": res.get("providers"),
            "chosen_provider": res.get("chosen_provider"),
            "m3u8": m3u8,
            "referer": (res.get("required_headers") or {}).get("Referer"),
            "quality": res.get("quality"),
            "type": res.get("type"),
            "raw": res,
        })
    except Exception as e:
        return jsonify({"error": "sources failed", "details": str(e)}), 500


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port)
