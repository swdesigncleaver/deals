# cheapfood_watch

Turns cheapfood.co.uk category/brand/search listing pages into a personal RSS
feed of newly-listed products, since the site (a BigCommerce "Stencil"
storefront) doesn't publish one natively — no `?rss=1`, no `/feed`, nothing
in its sitemap.

## How it works

1. You give it one or more listing page URLs (a category, ideally sorted
   `?sort=newest`; a brand page; a search results page — they all share the
   same product-grid markup).
2. It parses out each product's name, URL, price, and image.
3. It diffs against a JSON "seen products" file to find ones it's never
   observed before.
4. It (re)writes `feed.xml`, a standard RSS 2.0 file, with everything it has
   ever seen (capped at `--max-items`, newest-first).

Run it on a schedule. Every run's new discoveries appear in the feed; your
reader takes care of showing only the ones you haven't read yet, exactly
like any other RSS subscription.

## Install

```bash
pip install requests beautifulsoup4 lxml
```

## Usage

```bash
python3 cheapfood_watch.py \
  "https://cheapfood.co.uk/food/sports-energy/?sort=newest" \
  "https://cheapfood.co.uk/food/chocolate/?sort=newest" \
  --state ~/.cheapfood/state.json \
  --feed ~/.cheapfood/feed.xml \
  --title "cheapfood.co.uk — protein & chocolate"
```

Add `-k`/`--keyword` to only keep products whose title contains a given word
(case-insensitive). Repeat it to match on any of several words:

```bash
python3 cheapfood_watch.py \
  "https://cheapfood.co.uk/food/sports-energy/?sort=newest" \
  "https://cheapfood.co.uk/food/chocolate/?sort=newest" \
  -k protein -k collagen \
  --state ~/.cheapfood/state.json \
  --feed ~/.cheapfood/feed.xml
```

That keeps "N!ck's Peanut Butter **Protein** Bar" and "No Sugar Company
**Collagen** Bar" but drops a plain Kit Kat. Filtering happens before a
product is written to the state file, so non-matching items are never
tracked and never appear in the feed at all — you're not just hiding them in
a reader, the scan genuinely ignores them. This also means if you widen your
keyword list later, older non-matching products won't retroactively appear;
only newly-scraped pages are affected.

Watch as many pages as you like — pass multiple URLs. `?sort=newest` isn't
required (the tool tracks "new to me", not "new to the store"), but it puts
fresh stock at the top of the page, which is a nice sanity check when you're
eyeballing results.

The first run seeds the state file with everything currently listed and adds
it all to the feed (so the feed isn't empty on day one). Only the *second*
run onward reflects genuinely new arrivals — that's inherent to "new since
last check", not a bug.

### Scheduling it

**cron** (every 30 minutes):

```cron
*/30 * * * * cd /path/to/cheapfood_watch && /usr/bin/python3 cheapfood_watch.py "https://cheapfood.co.uk/food/sports-energy/?sort=newest" --state state.json --feed /var/www/html/cheapfood.xml >> run.log 2>&1
```

**Or** skip cron and let the script loop itself:

```bash
python3 cheapfood_watch.py "..." --interval 1800   # scans every 30 min, forever
```

### Getting the feed into a reader

`feed.xml` is a file on disk — your reader needs to fetch it over HTTP(S) (or
be one of the few that read local files, e.g. NetNewsWire on macOS). Easiest
options:

- Point the cron job's `--feed` at a directory already served by a web server
  you control (as in the cron example above).
- Push `feed.xml` to a static host after each run (GitHub Pages, a Netlify
  drop folder, an S3 bucket with static hosting, a Gist raw URL).
- Run a trivial static file server next to it (`python3 -m http.server`) if
  the machine running this is reachable from wherever your reader lives.

Then subscribe your reader to that URL like any other feed.

## Options

| Flag | Purpose |
|---|---|
| `--state PATH` | JSON "seen products" store (default `cheapfood_state.json`) |
| `--feed PATH` | Output RSS file (default `feed.xml`) |
| `--max-items N` | Cap on items kept in the feed (default 100) |
| `-k/--keyword WORD` | Only keep products whose title contains WORD (repeatable, OR'd, case-insensitive) |
| `--interval SECONDS` | Loop forever instead of running once |
| `--dump-html PATH` | Save the raw HTML of the first URL, for debugging |
| `--selector-card/-title/-price/-image` | Override the CSS selectors (see below) |
| `--ignore-robots` | Skip the robots.txt check |
| `-v/--verbose` | Debug logging |

## A note on the selectors — please verify before relying on this

I built and unit-tested the parser against BigCommerce's standard
Stencil/Cornerstone product-grid markup (`li.product` → `h4.card-title a` →
`span.price`, images lazy-loaded via `data-src`), which is what
cheapfood.co.uk's `meta-platform: bigcommerce.stencil` tag confirms it's
running. I could not do a live end-to-end run against the real site from my
own sandbox (no outbound network access there), so I wasn't able to confirm
the exact class names against the live DOM myself.

Before you trust this unattended, run once with `-v --dump-html raw.html`
against a real listing page:

```bash
python3 cheapfood_watch.py "https://cheapfood.co.uk/food/chocolate/" -v --dump-html raw.html
```

- If you see `Parsed N product card(s)` with N matching what's actually on
  the page, you're good.
- If you see the "No products found" warning, open `raw.html`, find a
  product card, and pass corrected selectors via `--selector-card`,
  `--selector-title`, `--selector-price`, `--selector-image` (or just edit
  `DEFAULT_SELECTORS` in the script — it's one dict at the top).

## Tests

```bash
pip install pytest
pytest tests/
```

The fixture in `tests/test_parse_products.py` is a hand-built stand-in for
the real markup (see the note at its top) — it locks in the parsing logic
(dedup, tracking-param stripping, lazy-image handling, skipping malformed
cards) so a future refactor can't silently break it, but it doesn't replace
the `--dump-html` check above against the live site.

## Etiquette

The script checks `robots.txt` before each fetch by default and identifies
itself with a custom User-Agent you should edit to include your own contact
info (`USER_AGENT` at the top of the script, or `--user-agent`). Keep the
scan interval reasonable (30 min is plenty for a "don't miss a restock" use
case) — there's no need to hammer a small independent grocer's server.
