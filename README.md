# Scraparts

Fast local search for **reliableaftermarketparts.com** (a Shopify store that is slow
to browse). It downloads the public catalogue once, keeps it in memory, and answers
every search from there - SKU, product ID, barcode, part number or name - with the
photos shown as small CDN thumbnails.

- **Search** - type a SKU / product ID / name; results update as you type (sub-millisecond lookups on the server, no page loads).
- **Bulk check** - paste or load an **XML** file (or a plain list) and every SKU / product ID in it is checked against the site: found / not found, product, SKU, price, stock. Download the result as CSV.
- **Product detail** - gallery, every variant with SKU / barcode / price / stock, copy buttons, link to the real page, and *Refresh live* to pull the current price + stock for that one item.
- **Ask the live site** - when something is newer than your last sync, one click queries the store directly (Shopify predictive search) and adds the hits.

It only reads public storefront endpoints (`/products.json`, `/products/<handle>.js`,
`/search/suggest.json`, `sitemap.xml`). No login, no credentials.

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

## Run

**Windows:** double-click **`run.bat`** - first run downloads the catalogue, then opens the page.

**CLI:**

```bash
python scraparts.py sync                    # download / refresh the catalogue -> data/catalog.json
python scraparts.py serve --open            # http://127.0.0.1:8765
python scraparts.py window                  # same UI in a native window (pip install pywebview)
python scraparts.py lookup BP-1001-F 1002   # check codes from the terminal
python scraparts.py lookup --xml feed.xml   # check every sku / product-id in an XML file
```

The **Sync** button on the page re-downloads the catalogue in the background.
If `/products.json` is disabled on the store, sync falls back to the sitemap and
fetches each product's `.js` record. A sync that returns 0 products is discarded and
the previous catalogue is kept.

Set `SCRAPARTS_SITE` to point it at another Shopify store, `SCRAPARTS_DATA` to move
the `data/` folder.

## What counts as a code in the XML

Any element or attribute named `sku`, `product-id` / `productid`, `id`, `item-id`,
`mpn`, `part-number`, `variant-id`, `upc`, `gtin`, `ean`, `barcode`, `handle`,
`model` (case, `-`, `_` and namespace prefixes like `g:` ignored). If the file uses
none of those, every short code-looking value in it is checked instead. Matching is
case- and punctuation-insensitive (`bp1001f` = `BP-1001-F`) against SKU, barcode,
variant ID, product ID and handle. Not-found codes list the closest catalogue hits.
XML with `DOCTYPE` / `ENTITY` declarations is rejected.

## Integrating into app.raply.space

The page is one self-contained file (`index.html`, no dependencies) and the server
sends open CORS headers, so either works:

```html
<!-- iframe the page; ?q= pre-fills a search, ?theme=dark|light forces a theme -->
<iframe src="https://YOUR-SCRAPARTS-HOST/?q=BP-1001-F&theme=dark" style="border:0;width:100%;height:700px"></iframe>
```

```text
# or host index.html with your app and point it at the API:
https://app.raply.space/parts.html?api=https://YOUR-SCRAPARTS-HOST&q=BP-1001-F
# (or set window.SCRAPARTS_API before the script runs)
```

JSON API:

| Endpoint | |
| --- | --- |
| `GET /api/status` | counts, last sync time, sync progress |
| `GET /api/search?q=&limit=` | ranked cards: exact SKU > SKU prefix > SKU contains > name tokens |
| `GET /api/product/<id\|handle>[?live=1]` | full record; `live=1` re-reads it from the store |
| `GET /api/live-search?q=` | asks the store itself |
| `POST /api/lookup` | body = XML or a list; returns per-code results |
| `POST /api/sync` | start a background sync |

The server binds to `127.0.0.1` by default. To serve it to other machines use
`serve --host 0.0.0.0` behind HTTPS - a page on an `https://` site cannot reliably
call an `http://` server on someone's own PC, so host the API over HTTPS for raply.space.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests run against a local fake Shopify store (pagination, both product shapes,
sitemap fallback, live endpoints) - no internet needed.

## Files

| File | Role |
| --- | --- |
| `scraparts.py` | Sync, in-memory index + search, XML code extraction, HTTP server, CLI. |
| `index.html` | The whole UI (search, detail, bulk check). |
| `run.bat` | Windows launcher. |
| `tests/test_scraparts.py` | Test suite with a fake store. |
