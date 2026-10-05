"""Runs scraparts against a local fake Shopify store. No internet needed.

    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scraparts  # noqa: E402

IMG = "https://cdn.shopify.com/s/files/1/0001/x/products/{}.jpg?v=1"


def product(pid, handle, title, skus, tags="", vendor="Reliable", avail=True, n_img=2):
    return {
        "id": pid,
        "title": title,
        "handle": handle,
        "body_html": f"<p>About <b>{title}</b></p><script>x()</script>",
        "vendor": vendor,
        "product_type": "Brakes",
        "tags": [t for t in tags.split(",") if t],
        "updated_at": "2026-01-01T00:00:00Z",
        "variants": [
            {"id": pid * 10 + i, "title": f"Opt {i}" if len(skus) > 1 else "Default Title", "sku": s,
             "barcode": f"0000{pid}{i}", "price": "19.50", "compare_at_price": None, "available": avail}
            for i, s in enumerate(skus)
        ],
        "images": [{"src": IMG.format(f"{handle}-{i}"), "width": 800, "height": 800, "alt": ""} for i in range(n_img)],
    }


PRODUCTS = [
    product(1001, "front-brake-pad-set", "Front Brake Pad Set Honda Civic", ["BP-1001-F"], "brakes,honda"),
    product(1002, "rear-rotor", "Rear Brake Rotor Toyota Camry", ["RR-2002", "RR-2002-X"], "brakes,toyota"),
    product(1003, "oil-filter", "Oil Filter Ford F150", ["OF 3003"], "filters,ford", avail=False),
    product(1004, "no-photo-thing", "Mystery Part", ["MP-4004"], n_img=0),
]
BY_HANDLE = {p["handle"]: p for p in PRODUCTS}


def as_js(p):
    """/products/<handle>.js shape: cents, URL-string images, 'description'."""
    return {
        "id": p["id"], "title": p["title"], "handle": p["handle"], "description": p["body_html"],
        "vendor": p["vendor"], "type": p["product_type"], "tags": p["tags"], "price": 1950,
        "images": [i["src"].replace("https:", "") for i in p["images"]],
        "variants": [dict(v, price=1950) for v in p["variants"]],
    }


class FakeShop(BaseHTTPRequestHandler):
    page_size = 2
    products_json_enabled = True
    hits = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FakeShop.hits.append(u.path)
        if u.path == "/products.json":
            if not FakeShop.products_json_enabled:
                return self._send(404, "nope", "text/plain")
            page = int(q.get("page", 1))
            n = FakeShop.page_size
            return self._send(200, {"products": PRODUCTS[(page - 1) * n: page * n]})
        if u.path == "/sitemap.xml":
            return self._send(200, f"<sitemapindex><sitemap><loc>{self.base}/sitemap_products_1.xml</loc></sitemap></sitemapindex>", "text/xml")
        if u.path == "/sitemap_products_1.xml":
            urls = "".join(f"<url><loc>{self.base}/products/{p['handle']}</loc></url>" for p in PRODUCTS)
            return self._send(200, f"<urlset>{urls}</urlset>", "text/xml")
        if u.path.startswith("/products/") and u.path.endswith(".js"):
            p = BY_HANDLE.get(u.path[len("/products/"):-3])
            return self._send(200, as_js(p)) if p else self._send(404, {"error": "x"})
        if u.path == "/search/suggest.json":
            term = q.get("q", "").lower()
            hits = [p for p in PRODUCTS if term and term in (p["title"] + " ".join(v["sku"] for v in p["variants"])).lower()]
            return self._send(200, {"resources": {"results": {"products": [{"handle": p["handle"]} for p in hits]}}})
        self._send(404, {"error": "nf"})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shop = ThreadingHTTPServer(("127.0.0.1", 0), FakeShop)
        FakeShop.base = f"http://127.0.0.1:{cls.shop.server_port}"
        threading.Thread(target=cls.shop.serve_forever, daemon=True).start()
        cls.site = FakeShop.base
        cls.tmp = tempfile.TemporaryDirectory()
        scraparts.DATA_DIR = cls.tmp.name
        scraparts.CATALOG_FILE = os.path.join(cls.tmp.name, "catalog.json")

    @classmethod
    def tearDownClass(cls):
        cls.shop.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        FakeShop.products_json_enabled = True
        FakeShop.hits = []


class TestSync(Base):
    def test_paginates_and_normalises(self):
        cat = scraparts.sync(site=self.site)
        self.assertEqual(len(cat.products), 4)
        self.assertGreaterEqual(FakeShop.hits.count("/products.json"), 3)  # 2 + 2 + empty page
        p = cat.get("rear-rotor")
        self.assertEqual([v["sku"] for v in p["variants"]], ["RR-2002", "RR-2002-X"])
        self.assertEqual(p["variants"][0]["price"], 19.5)
        self.assertEqual(p["desc"], "About Rear Brake Rotor Toyota Camry")  # tags + <script> stripped
        self.assertTrue(os.path.exists(scraparts.CATALOG_FILE))
        self.assertEqual(len(scraparts.load_catalog().products), 4)

    def test_sitemap_fallback(self):
        FakeShop.products_json_enabled = False
        cat = scraparts.sync(site=self.site)
        self.assertEqual(len(cat.products), 4)
        p = cat.get("front-brake-pad-set")
        self.assertEqual(p["variants"][0]["price"], 19.5)  # cents -> dollars
        self.assertTrue(p["images"][0]["src"].startswith("https://cdn.shopify.com"))  # '//' fixed up

    def test_sync_keeps_older_products_found_live(self):
        older = scraparts.compact(product(900, "old-thing", "Old Thing", ["OLD-900"]), self.site)
        scraparts.save_catalog(scraparts.Catalog([older], 1.0))
        try:
            cat = scraparts.sync(site=self.site)
            self.assertEqual(len(cat.products), 5)
            self.assertEqual(cat.get("old-thing")["variants"][0]["sku"], "OLD-900")
        finally:
            os.remove(scraparts.CATALOG_FILE)

    def test_empty_site_keeps_old_catalogue(self):
        FakeShop.page_size = 0
        try:
            with self.assertRaises(RuntimeError):
                scraparts.sync(site=self.site)
        finally:
            FakeShop.page_size = 2


class TestSearch(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cat = scraparts.sync(site=cls.site)

    def top(self, q):
        r = self.cat.search(q)
        return r[0]["handle"] if r else None

    def test_exact_sku_ignores_punctuation_and_case(self):
        self.assertEqual(self.top("bp-1001-f"), "front-brake-pad-set")
        self.assertEqual(self.top("BP1001F"), "front-brake-pad-set")
        self.assertEqual(self.top("of3003"), "oil-filter")  # stored as 'OF 3003'

    def test_sku_prefix_and_variant_hit(self):
        r = self.cat.search("RR-2002-X")
        self.assertEqual(r[0]["handle"], "rear-rotor")
        self.assertEqual(r[0]["matched"], [10021])  # variant id 1002*10+1

    def test_product_id_and_barcode(self):
        self.assertEqual(self.top("1003"), "oil-filter")
        self.assertEqual(self.top("00001001" + "0"), "front-brake-pad-set")

    def test_text_search_all_tokens(self):
        self.assertEqual(self.top("toyota rotor"), "rear-rotor")
        r = self.cat.search("brake")  # all four are type "Brakes"; the two with it in the title rank first
        self.assertEqual({x["handle"] for x in r[:2]}, {"front-brake-pad-set", "rear-rotor"})
        self.assertEqual(len(r), 4)
        self.assertEqual(self.cat.search("zzzzqq"), [])

    def test_card_shape_and_stock(self):
        c = self.cat.search("oil filter")[0]
        self.assertFalse(c["available"])
        self.assertEqual(self.cat.search("mystery")[0]["image"], None)
        self.assertTrue(self.cat.search("brake pad")[0]["image"].startswith("https://cdn.shopify.com"))

    def test_search_is_fast(self):
        big = scraparts.Catalog([
            scraparts.compact(product(i, f"h{i}", f"Part number {i} widget", [f"SKU-{i}"]), self.site)
            for i in range(20000, 40000)
        ])
        import time
        t = time.perf_counter()
        big.search("SKU-3999")
        big.search("widget 3999")
        self.assertLess(time.perf_counter() - t, 1.0)


class TestExtract(unittest.TestCase):
    def test_xml_tags_namespaces_and_attrs(self):
        xml = """<?xml version="1.0"?><feed xmlns:g="http://base.google.com/ns/1.0">
          <item><g:id>BP-1001-F</g:id><title>x</title></item>
          <item><product-id>1002</product-id><SKU> RR-2002 </SKU></item>
          <row id="1003" sku="OF 3003"/></feed>"""
        self.assertEqual(scraparts.extract_codes(xml), ["BP-1001-F", "1002", "RR-2002", "1003", "OF 3003"])

    def test_unknown_xml_falls_back_to_code_like_leaves(self):
        xml = "<a><b>ABC-123</b><b>Some long description with spaces</b></a>"
        self.assertEqual(scraparts.extract_codes(xml), ["ABC-123"])

    def test_malformed_xml(self):
        self.assertEqual(scraparts.extract_codes("<a><sku>X-1</sku><sku>X-2</a>"), ["X-1", "X-2"])

    def test_plain_lists_and_dedupe(self):
        self.assertEqual(scraparts.extract_codes("A1, B2\nC3;a1\t D4 "), ["A1", "B2", "C3", "D4"])

    def test_rejects_entities(self):
        with self.assertRaises(ValueError):
            scraparts.extract_codes('<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')


class TestServer(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        scraparts.SITE = cls.site  # live endpoints read this at call time via defaults below
        scraparts.fetch_live_product.__defaults__ = (None, cls.site)
        scraparts.live_search.__defaults__ = (None, cls.site, 10)
        scraparts.STATE.catalog = scraparts.sync(site=cls.site)
        cls.srv = scraparts.make_server("127.0.0.1", 0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        super().tearDownClass()

    def test_index_and_cors(self):
        r = requests.get(self.api + "/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<title>Scraparts</title>", r.text)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(requests.options(self.api + "/api/lookup").status_code, 204)

    def test_status_search_product(self):
        s = requests.get(self.api + "/api/status").json()
        self.assertEqual((s["products"], s["variants"]), (4, 5))
        r = requests.get(self.api + "/api/search", params={"q": "BP1001F"}).json()
        self.assertEqual(r["results"][0]["handle"], "front-brake-pad-set")
        self.assertLess(r["ms"], 50)
        p = requests.get(self.api + "/api/product/rear-rotor").json()
        self.assertEqual(len(p["variants"]), 2)
        self.assertEqual(requests.get(self.api + "/api/product/1002").json()["handle"], "rear-rotor")
        self.assertEqual(requests.get(self.api + "/api/product/nope").status_code, 404)

    def test_lookup_xml_and_list(self):
        xml = "<items><item><sku>BP-1001-F</sku></item><item><sku>NOPE-9</sku></item><item><product-id>1003</product-id></item></items>"
        r = requests.post(self.api + "/api/lookup", data=xml, headers={"Content-Type": "text/xml"}).json()
        self.assertEqual((r["total"], r["found"]), (3, 2))
        miss = [x for x in r["rows"] if not x["found"]][0]
        self.assertEqual(miss["query"], "NOPE-9")
        kinds = {x["query"]: x["matches"][0]["kind"] for x in r["rows"] if x["found"]}
        self.assertEqual(kinds, {"BP-1001-F": "sku", "1003": "product id"})
        j = requests.post(self.api + "/api/lookup", json={"text": "rr2002\nzzz"}).json()
        self.assertEqual(j["found"], 1)
        self.assertEqual(requests.post(self.api + "/api/lookup", data="<!DOCTYPE a [<!ENTITY b 'c'>]><a/>").status_code, 400)

    def test_lookup_falls_back_to_live_for_uncached_products(self):
        # a cache holding only the first two products stands in for "the newest 25k"
        cat = scraparts.Catalog([scraparts.compact(p, self.site) for p in PRODUCTS[:2]])
        r = scraparts.lookup(cat, ["OF 3003", "NOPE-9", "BP-1001-F"], live=True)
        self.assertEqual([(x["query"], x["found"], x["source"]) for x in r["rows"]],
                         [("OF 3003", True, "live"), ("NOPE-9", False, "cache"), ("BP-1001-F", True, "cache")])
        self.assertEqual(r["live_checked"], 2)
        self.assertEqual(len(cat.products), 3)  # the live find is now cached
        self.assertEqual(scraparts.lookup(cat, ["OF 3003"], live=True)["rows"][0]["source"], "cache")
        # without live=True nothing leaves the process
        self.assertFalse(scraparts.lookup(scraparts.Catalog(), ["OF 3003"])["rows"][0]["found"])

    def test_live_search_reports_new_products(self):
        scraparts.STATE.catalog = scraparts.Catalog([scraparts.compact(PRODUCTS[0], self.site)])
        scraparts.STATE.saved_at = time.time()  # keep persist_soon from touching the disk mid-test
        try:
            r = requests.get(self.api + "/api/live-search", params={"q": "ford"}).json()
            self.assertEqual((r["new"], [c["handle"] for c in r["results"]]), (1, ["oil-filter"]))
            self.assertEqual(requests.get(self.api + "/api/live-search", params={"q": "ford"}).json()["new"], 0)
            self.assertEqual(scraparts.STATE.catalog.exact("OF 3003")[0]["product"]["handle"], "oil-filter")
        finally:
            scraparts.STATE.catalog = scraparts.sync(site=self.site)

    def test_live_refresh_and_live_search(self):
        p = requests.get(self.api + "/api/product/oil-filter", params={"live": 1}).json()
        self.assertEqual(p["variants"][0]["price"], 19.5)
        r = requests.get(self.api + "/api/live-search", params={"q": "camry"}).json()
        self.assertEqual([c["handle"] for c in r["results"]], ["rear-rotor"])
        self.assertEqual(len(scraparts.STATE.catalog.products), 4)  # upsert replaced, no duplicates
        self.assertEqual(len(scraparts.STATE.catalog.search("RR-2002")), 1)


if __name__ == "__main__":
    unittest.main()
