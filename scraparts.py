#!/usr/bin/env python3
"""scraparts - fast local search for reliableaftermarketparts.com (Shopify).

The storefront is slow, so we pull its public catalogue once (`sync`), keep it in
memory and answer searches from there. Every search is a dict lookup, not a
page load.

    python scraparts.py sync                    # download / refresh the catalogue
    python scraparts.py serve                   # http://127.0.0.1:8765  (search UI + JSON API)
    python scraparts.py window                  # same UI in a native window (needs pywebview)
    python scraparts.py lookup SKU1 SKU2        # check codes from the terminal
    python scraparts.py lookup --xml feed.xml   # check every sku / product-id in an XML file

JSON API (CORS open, so app.raply.space can call it or iframe the page):
    GET  /api/status
    GET  /api/search?q=&limit=
    GET  /api/product/<id|handle>[?live=1]
    GET  /api/live-search?q=                   # asks the real site, for items newer than the last sync
    POST /api/lookup                           # body: XML, or one code per line / comma separated
    POST /api/sync
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import sys
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import requests

try:  # readable output in the Windows console
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

SITE = os.environ.get("SCRAPARTS_SITE", "https://reliableaftermarketparts.com").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("SCRAPARTS_DATA", os.path.join(HERE, "data"))
CATALOG_FILE = os.path.join(DATA_DIR, "catalog.json")
INDEX_FILE = os.path.join(HERE, "index.html")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 scraparts/1.0"
)
MAX_BODY = 5 * 1024 * 1024
MAX_LIMIT = 60


# --- helpers -----------------------------------------------------------------


def norm(s) -> str:
    """Lower-case alphanumerics only, so 'AB-123 / x' matches 'ab123x'."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def tokens(s) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(s or "").lower())


def _strip_html(s, limit=1200) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s).strip()
    return s[:limit]


def _abs(url: str) -> str:
    return "https:" + url if url.startswith("//") else url


# --- http --------------------------------------------------------------------


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/xml, */*"})
    return s


def get(session, url, *, params=None, tries=5):
    """GET with backoff on 429 / 5xx (Shopify throttles with Retry-After)."""
    last = None
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            last = e
            time.sleep(min(2**attempt, 15))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            try:
                wait = float(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = 2**attempt
            last = RuntimeError(f"HTTP {r.status_code} from {url}")
            time.sleep(min(wait, 30))
            continue
        return r
    raise RuntimeError(f"Could not reach {url}: {last}")


# --- product shape -----------------------------------------------------------


def compact(p: dict, site: str = SITE) -> dict:
    """Normalises either Shopify shape (products.json or /products/<handle>.js)
    into the one record we store and serve."""
    is_js = "body_html" not in p and "description" in p  # .js: prices in cents, images are URL strings

    def price(x):
        if x in (None, ""):
            return None
        return round(float(x) / 100, 2) if is_js else round(float(x), 2)

    variants = []
    for v in p.get("variants") or []:
        variants.append(
            {
                "id": v.get("id"),
                "title": "" if (v.get("title") or "") == "Default Title" else (v.get("title") or ""),
                "sku": (v.get("sku") or "").strip(),
                "barcode": (v.get("barcode") or "").strip(),
                "price": price(v.get("price")),
                "compare_at": price(v.get("compare_at_price")),
                "available": v.get("available"),
            }
        )

    images = []
    for i in p.get("images") or []:
        if isinstance(i, str):
            images.append({"src": _abs(i), "alt": ""})
        elif i.get("src"):
            images.append({"src": _abs(i["src"]), "alt": i.get("alt") or "", "w": i.get("width"), "h": i.get("height")})

    tags = p.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    prices = [v["price"] for v in variants if v["price"] is not None]
    avail = [v["available"] for v in variants if v["available"] is not None]
    handle = p.get("handle") or ""
    return {
        "id": p.get("id"),
        "handle": handle,
        "title": p.get("title") or "",
        "vendor": p.get("vendor") or "",
        "type": p.get("product_type") or p.get("type") or "",
        "tags": tags,
        "desc": _strip_html(p.get("body_html") or p.get("description") or ""),
        "url": f"{site}/products/{handle}",
        "images": images,
        "variants": variants,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "available": any(avail) if avail else None,
        "updated": p.get("updated_at") or "",
    }


def card(p: dict, matched=None) -> dict:
    """Light version of a product for result lists."""
    skus = [v["sku"] for v in p["variants"] if v["sku"]]
    return {
        "id": p["id"],
        "handle": p["handle"],
        "title": p["title"],
        "vendor": p["vendor"],
        "type": p["type"],
        "image": p["images"][0]["src"] if p["images"] else None,
        "n_images": len(p["images"]),
        "skus": skus[:6],
        "n_variants": len(p["variants"]),
        "price_min": p["price_min"],
        "price_max": p["price_max"],
        "available": p["available"],
        "url": p["url"],
        "matched": matched or [],
    }


# --- catalogue (in-memory index) ---------------------------------------------


class Catalog:
    def __init__(self, products=None, synced_at=None):
        self.products = products or []
        self.synced_at = synced_at
        self.by_id = {}
        self.by_handle = {}
        self.keys = {}  # norm(sku | barcode | variant id) -> [(product, variant, kind)]
        self.blobs = []  # (product, searchable lower-case text)
        self.lock = threading.RLock()  # live results are upserted while other requests search
        for p in self.products:
            self._index(p)

    def _index(self, p):
        self.by_id[str(p["id"])] = p
        self.by_handle[p["handle"].lower()] = p
        bits = [p["title"], p["vendor"], p["type"], p["handle"].replace("-", " "), " ".join(p["tags"])]
        for v in p["variants"]:
            bits += [v["sku"], v["title"], v["barcode"]]
            for kind, val in (("sku", v["sku"]), ("barcode", v["barcode"]), ("variant id", v["id"])):
                n = norm(val)
                if n:
                    self.keys.setdefault(n, []).append((p, v, kind))
        self.blobs.append((p, " ".join(b for b in bits if b).lower()))

    def upsert(self, p) -> bool:
        """Add a product, or swap in a fresher copy (live refresh). True if it was new."""
        with self.lock:
            old = self.by_id.get(str(p["id"]))
            if old is not None:
                self.products = [q for q in self.products if q is not old]
                self.blobs = [(q, t) for q, t in self.blobs if q is not old]
                for n in list(self.keys):
                    kept = [e for e in self.keys[n] if e[0] is not old]
                    if kept:
                        self.keys[n] = kept
                    else:
                        del self.keys[n]
            self.products.append(p)
            self._index(p)
            return old is None

    def get(self, ident) -> dict | None:
        ident = unquote(str(ident)).strip()
        return self.by_id.get(ident) or self.by_handle.get(ident.lower())

    def exact(self, code) -> list[dict]:
        """Every product whose SKU / barcode / variant id / product id / handle equals `code`."""
        code = str(code or "").strip()
        n = norm(code)
        out, seen = [], set()

        def add(p, v, kind):
            k = (p["id"], v["id"] if v else None, kind)
            if k not in seen:
                seen.add(k)
                out.append({"product": p, "variant": v, "kind": kind})

        with self.lock:
            for p, v, kind in self.keys.get(n, []):
                add(p, v, kind)
            if code in self.by_id:
                add(self.by_id[code], None, "product id")
            if code.lower() in self.by_handle:
                add(self.by_handle[code.lower()], None, "handle")
        return out

    def search(self, q, limit=24) -> list[dict]:
        with self.lock:
            return self._search(q, limit)

    def _search(self, q, limit):
        qn = norm(q)
        toks = tokens(q)
        if not qn:
            return []
        scores = {}  # product id -> [score, product, matched variant ids]

        def bump(p, score, vid=None):
            e = scores.setdefault(p["id"], [0, p, []])
            e[0] = max(e[0], score)
            if vid is not None and vid not in e[2]:
                e[2].append(vid)

        for n, entries in self.keys.items():
            if n == qn:
                score = 100
            elif len(qn) >= 2 and n.startswith(qn):
                score = 80
            elif len(qn) >= 3 and qn in n:
                score = 60
            else:
                continue
            for p, v, kind in entries:
                if kind != "variant id" or score == 100:
                    bump(p, score, v["id"])
        if str(q).strip() in self.by_id:
            bump(self.by_id[str(q).strip()], 100)
        if toks:
            for p, blob in self.blobs:
                if all(t in blob for t in toks):
                    title = p["title"].lower()
                    bump(p, 40 + 10 * sum(t in title for t in toks) // len(toks))

        ranked = sorted(scores.values(), key=lambda e: (-e[0], e[1]["title"]))
        return [dict(card(p, matched), score=s) for s, p, matched in ranked[: max(1, min(limit, MAX_LIMIT))]]


# --- XML / text code extraction ----------------------------------------------

ID_TAGS = {
    "sku", "productid", "product", "id", "itemid", "item", "mpn", "partnumber", "partno", "part",
    "variantid", "upc", "gtin", "ean", "barcode", "handle", "vendorsku", "modelnumber", "model",
}
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/#]{1,39}$")


def extract_codes(text: str) -> list[str]:
    """Pulls SKUs / product ids out of an XML document (feed, export, ...) or a
    plain list (one per line, or comma / semicolon / tab separated)."""
    text = (text or "").strip().lstrip("﻿")
    if not text:
        return []
    found: list[str] = []

    if text.startswith("<"):
        if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
            raise ValueError("XML with DOCTYPE / ENTITY declarations is not accepted.")
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            root = None
        if root is not None:
            leaves = []
            for el in root.iter():
                tag = norm(el.tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1])
                val = (el.text or "").strip()
                if tag in ID_TAGS and val and len(el) == 0:
                    found.append(val)
                for k, v in el.attrib.items():
                    if norm(k.rsplit("}", 1)[-1]) in ID_TAGS and v.strip():
                        found.append(v.strip())
                if val and len(el) == 0:
                    leaves.append(val)
            if not found:  # unknown layout: any short code-looking leaf value
                found = [v for v in leaves if CODE_RE.match(v)]
        else:  # malformed XML: pull values with a regex instead of failing
            pat = r"<(?:[\w.-]+:)?(" + "|".join(sorted(ID_TAGS)) + r"|product[-_]id|part[-_]number)\b[^>]*>([^<]+)<"
            found = [m.group(2).strip() for m in re.finditer(pat, text, re.I)]
    else:
        found = [p.strip().strip('"') for p in re.split(r"[\n\r,;\t]+", text) if p.strip()]

    out, seen = [], set()
    for c in found:
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


MAX_LIVE_CODES = 300  # per bulk check; each live miss costs about 0.4 s (8 in parallel)


def live_exact(catalog: Catalog, code: str, session=None) -> bool:
    """Ask the real site for `code` and cache what it returns. True if the catalogue
    now has an exact match (the store's search is fuzzy, so hits are re-checked)."""
    try:
        found = live_search(code, session=session, limit=5)
    except RuntimeError:
        return False
    for p in found:
        catalog.upsert(p)
    return bool(catalog.exact(code))


def lookup(catalog: Catalog, codes: list[str], live=False) -> dict:
    """Check each code against the catalogue. With live=True, codes the cache
    does not know are asked of the real site (the cache only holds the newest products)."""
    missing = [c for c in codes if not catalog.exact(c)] if live else []
    live_found = set()
    if missing:
        session = make_session()
        todo = missing[:MAX_LIVE_CODES]
        with ThreadPoolExecutor(max_workers=8) as pool:
            for code, ok in zip(todo, pool.map(lambda c: live_exact(catalog, c, session), todo)):
                if ok:
                    live_found.add(code)
    rows = []
    for code in codes:
        hits = catalog.exact(code)
        row = {
            "source": "live" if code in live_found else "cache",
            "query": code,
            "found": bool(hits),
            "matches": [
                dict(card(h["product"], [h["variant"]["id"]] if h["variant"] else []), kind=h["kind"], variant=h["variant"])
                for h in hits
            ],
        }
        if not hits:
            row["similar"] = catalog.search(code, limit=3)
        rows.append(row)
    return {
        "total": len(rows),
        "found": sum(r["found"] for r in rows),
        "live_checked": min(len(missing), MAX_LIVE_CODES),
        "live_skipped": max(0, len(missing) - MAX_LIVE_CODES),
        "rows": rows,
    }


# --- syncing the catalogue ---------------------------------------------------


def fetch_products_json(session, site, progress):
    """Fast path: /products.json, 250 per page, until a page adds nothing new."""
    out, seen = [], set()
    for page in range(1, 500):
        r = get(session, f"{site}/products.json", params={"limit": 250, "page": page})
        if r.status_code != 200:
            if page == 1:
                raise RuntimeError(f"/products.json returned HTTP {r.status_code}")
            break
        try:
            batch = r.json().get("products", [])
        except ValueError:
            if page == 1:
                raise RuntimeError("/products.json did not return JSON")
            break
        new = [p for p in batch if p["id"] not in seen]
        if not new:
            break
        seen.update(p["id"] for p in new)
        out += new
        progress(len(out), "products.json")
        time.sleep(0.25)
    return out


def fetch_via_sitemap(session, site, progress):
    """Fallback if /products.json is disabled: walk sitemap.xml, then fetch each
    product's /products/<handle>.js."""
    root = get(session, f"{site}/sitemap.xml")
    if root.status_code != 200:
        raise RuntimeError(f"/sitemap.xml returned HTTP {root.status_code}")
    sitemaps = [u for u in re.findall(r"<loc>([^<]+)</loc>", root.text) if "sitemap_products" in u]
    handles = []
    for sm in sitemaps:
        txt = get(session, html.unescape(sm)).text
        for u in re.findall(r"<loc>([^<]+)</loc>", txt):
            m = re.search(r"/products/([^/?#]+)", u)
            if m:
                handles.append(m.group(1))
    handles = list(dict.fromkeys(handles))
    if not handles:
        raise RuntimeError("sitemap.xml listed no products")

    out = []

    def one(h):
        r = get(session, f"{site}/products/{h}.js")
        return r.json() if r.status_code == 200 else None

    with ThreadPoolExecutor(max_workers=6) as pool:
        for p in pool.map(one, handles):
            if p:
                out.append(p)
                progress(len(out), "sitemap")
    return out


def sync(progress=lambda n, phase: None, site=SITE) -> Catalog:
    session = make_session()
    try:
        raw = fetch_products_json(session, site, progress)
    except RuntimeError as e:
        print(f"  products.json unavailable ({e}); falling back to the sitemap")
        raw = fetch_via_sitemap(session, site, progress)
    if not raw:
        raise RuntimeError("The site returned 0 products; keeping the previous catalogue.")
    products = [compact(p, site) for p in raw]
    # products.json only reaches the newest ~25k (Shopify caps page * limit), so keep
    # everything already cached (older products found live) and let fresh copies win.
    fresh = {p["id"] for p in products}
    products += [p for p in load_catalog().products if p["id"] not in fresh]
    cat = Catalog(products, time.time())
    save_catalog(cat, site)
    return cat


def save_catalog(cat: Catalog, site=SITE):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CATALOG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"site": site, "synced_at": cat.synced_at, "products": cat.products}, f, separators=(",", ":"))
    os.replace(tmp, CATALOG_FILE)


def load_catalog() -> Catalog:
    try:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return Catalog(d.get("products", []), d.get("synced_at"))
    except (OSError, ValueError):
        return Catalog()


# --- live queries (straight to the real site) --------------------------------


def fetch_live_product(handle, session=None, site=SITE) -> dict | None:
    r = get(session or make_session(), f"{site}/products/{handle}.js")
    return compact(r.json(), site) if r.status_code == 200 else None


def live_search(q, session=None, site=SITE, limit=10) -> list[dict]:
    """Shopify predictive search, then the full record of each hit."""
    session = session or make_session()
    r = get(
        session,
        f"{site}/search/suggest.json",
        params={"q": q, "resources[type]": "product", "resources[limit]": limit},
    )
    if r.status_code != 200:
        return []
    hits = r.json().get("resources", {}).get("results", {}).get("products", [])
    handles = [h["handle"] for h in hits if h.get("handle")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        return [p for p in pool.map(lambda h: fetch_live_product(h, session, site), handles) if p]


# --- server ------------------------------------------------------------------


class State:
    def __init__(self):
        self.catalog = load_catalog()
        self.lock = threading.Lock()
        self.saved_at, self.saving = 0.0, False
        self.sync = {"running": False, "count": 0, "phase": "", "error": None}

    def status(self):
        c = self.catalog
        return {
            "site": SITE,
            "products": len(c.products),
            "variants": sum(len(p["variants"]) for p in c.products),
            "synced_at": c.synced_at,
            "sync": dict(self.sync),
        }

    def persist_soon(self):
        """Save live finds to disk, at most every 30 s, off the request thread."""
        with self.lock:
            if self.saving or time.time() - self.saved_at < 30:
                return
            self.saving = True

        def work():
            try:
                save_catalog(self.catalog)
            finally:
                self.saved_at, self.saving = time.time(), False

        threading.Thread(target=work, daemon=True).start()

    def start_sync(self) -> bool:
        with self.lock:
            if self.sync["running"]:
                return False
            self.sync.update(running=True, count=0, phase="starting", error=None)

        def work():
            try:
                self.catalog = sync(lambda n, ph: self.sync.update(count=n, phase=ph))
            except Exception as e:  # surfaced to the page, never crashes the server
                self.sync["error"] = str(e)
            finally:
                self.sync["running"] = False

        threading.Thread(target=work, daemon=True).start()
        return True


STATE = State()


class Handler(BaseHTTPRequestHandler):
    server_version = "scraparts"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, status, body: bytes, ctype, extra=None):
        headers = {
            "Content-Type": ctype,
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Private-Network": "true",  # public page -> local server preflight
            "Cache-Control": "no-cache",
        }
        headers.update(extra or {})
        if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 5)
            headers["Content-Encoding"] = "gzip"
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json; charset=utf-8")

    def _err(self, status, msg):
        self._json({"error": msg}, status)

    def do_OPTIONS(self):
        self._send(HTTPStatus.NO_CONTENT, b"", "text/plain")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        try:
            if path in ("/", "/index.html"):
                with open(INDEX_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(STATE.status())
            if path == "/api/search":
                try:
                    limit = int(q.get("limit", 24))
                except ValueError:
                    limit = 24
                t0 = time.perf_counter()
                results = STATE.catalog.search(q.get("q", ""), limit)
                return self._json({"q": q.get("q", ""), "ms": round((time.perf_counter() - t0) * 1000, 2), "results": results})
            if path == "/api/live-search":
                found = live_search(q.get("q", ""))
                new = [STATE.catalog.upsert(p) for p in found]
                if any(new):
                    STATE.persist_soon()
                return self._json({"q": q.get("q", ""), "new": sum(new), "results": [card(p) for p in found]})
            if path.startswith("/api/product/"):
                ident = path[len("/api/product/"):]
                p = STATE.catalog.get(ident)
                if q.get("live") and (p or ident):
                    fresh = fetch_live_product(p["handle"] if p else unquote(ident))
                    if fresh:
                        STATE.catalog.upsert(fresh)
                        p = fresh
                if not p:
                    return self._err(404, "Product not found")
                return self._json(p)
            return self._err(404, "Not found")
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            return self._err(502, str(e))

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._err(413, "Body too large (5 MB max)")
        body = self.rfile.read(length).decode("utf-8", "replace")
        try:
            if path == "/api/lookup":
                if body.lstrip().startswith("{"):
                    try:
                        body = json.loads(body).get("text", "")
                    except ValueError:
                        pass
                before = len(STATE.catalog.products)
                res = lookup(STATE.catalog, extract_codes(body), live=True)
                if len(STATE.catalog.products) > before:
                    STATE.persist_soon()
                return self._json(res)
            if path == "/api/sync":
                return self._json({"started": STATE.start_sync()})
            return self._err(404, "Not found")
        except ValueError as e:
            return self._err(400, str(e))
        except Exception as e:
            return self._err(500, str(e))


def make_server(host="127.0.0.1", port=8765) -> ThreadingHTTPServer:
    ThreadingHTTPServer.daemon_threads = True
    return ThreadingHTTPServer((host, port), Handler)


# --- CLI ---------------------------------------------------------------------


def cmd_sync(_args):
    t0 = time.time()

    def progress(n, phase):
        print(f"\r  {n} products ({phase})", end="", flush=True)

    cat = sync(progress)
    print(f"\n  done: {len(cat.products)} products in {time.time() - t0:.1f}s -> {CATALOG_FILE}")


def cmd_serve(args):
    if not STATE.catalog.products:
        print("  no local catalogue yet - syncing in the background (the page shows progress)")
        STATE.start_sync()
    srv = make_server(args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"  scraparts  {url}   ({len(STATE.catalog.products)} products)   Ctrl+C to stop")
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


def cmd_window(args):
    try:
        import webview
    except ImportError:
        sys.exit("  pip install pywebview   (needed for window mode)")
    if not STATE.catalog.products:
        STATE.start_sync()
    srv = make_server("127.0.0.1", args.port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    webview.create_window("Scraparts", f"http://127.0.0.1:{args.port}/", width=1100, height=760, min_size=(420, 400))
    webview.start()


def cmd_lookup(args):
    cat = STATE.catalog
    if not cat.products:
        sys.exit("  no catalogue - run `python scraparts.py sync` first")
    codes = list(args.codes)
    if args.xml:
        with open(args.xml, encoding="utf-8-sig") as f:
            codes += extract_codes(f.read())
    if not codes:
        sys.exit("  give some codes, or --xml FILE")
    before = len(cat.products)
    res = lookup(cat, codes, live=True)
    if len(cat.products) > before:
        save_catalog(cat)
    for row in res["rows"]:
        if row["found"]:
            for m in row["matches"]:
                v = m.get("variant") or {}
                print(f"  OK    {row['query']:<22} {m['kind']:<10} {v.get('sku') or '-':<18} {m['title'][:60]}")
        else:
            sim = ", ".join(s["skus"][0] if s["skus"] else s["title"][:30] for s in row["similar"])
            print(f"  MISS  {row['query']:<22}" + (f" similar: {sim}" if sim else ""))
    print(f"\n  {res['found']}/{res['total']} found")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fast local search for reliableaftermarketparts.com")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="download / refresh the catalogue").set_defaults(fn=cmd_sync)
    s = sub.add_parser("serve", help="run the search page + JSON API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open the page in your browser")
    s.set_defaults(fn=cmd_serve)
    w = sub.add_parser("window", help="open the UI in a native window (pywebview)")
    w.add_argument("--port", type=int, default=8765)
    w.set_defaults(fn=cmd_window)
    l = sub.add_parser("lookup", help="check SKUs / product ids from the terminal")
    l.add_argument("codes", nargs="*")
    l.add_argument("--xml", help="file to pull SKUs / product ids from")
    l.set_defaults(fn=cmd_lookup)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
