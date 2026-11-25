# smartMarket 🛒

> A small side project / weekend experiment: "is this actually a good price, or does it just *look* like a deal?"

smartMarket is a tiny, self-hosted price tracker for an online grocery shop. You give it a handful of product pages. Once a day it records each price, remembers the history, and sends you a short report as an image on Telegram:

- **↓** the price dropped since the last check
- **★** the price is below its own historical average
- **N+M offers** (e.g. "2+1 free") are converted into the real per-item price, so a multi-buy is compared fairly with a plain discount

There's also a small local dashboard with the last two weeks of history and the "cheapest weekday so far" for each product.

It's deliberately simple: one Python file, SQLite, Pillow for the report image, and no framework. It's an experiment, not a product, so expect rough edges.

## How it works

```
products list ──► fetch each product page once a day ──► parse name / price / offer
                                                              │
                     Telegram report image ◄── compare with ◄─┘
                     + local dashboard         history (SQLite)
```

The parser reads common, standard page markup: `og:title`, `<h1>`, `itemprop="price"`, `data-price` attributes and JSON-LD `Product`/`Offer` data. Categories come from breadcrumbs. Many shops expose some of these, but every site is different, so you may need to adjust `ProductPageParser` in `app.py` for yours.

> **Note:** the dashboard and the Telegram report are currently in Greek, because that's where the experiment started. The code and configuration are in English.

## Quick start

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env               # then set PRICE_TRACKER_STORE_URL
cp products.example.txt products.txt   # then add your product URLs

python app.py --sync-products      # dashboard on http://127.0.0.1:8000
```

Click the "collect now" button (**Συλλογή τώρα**) in the dashboard to record the first prices. While the app is running, it also collects automatically once a day at `PRICE_TRACKER_DAILY_TIME`.

Other commands:

```bash
python app.py --sync-products --collect          # collect right now, then exit
python app.py --sync-products --collect-if-due   # collect only if today's run is due (for cron)
python -m unittest discover -s tests             # run the tests
```

## Configuration

Everything is set through environment variables or a local `.env` file. Neither the shop nor your shopping list is hard-coded.

| Variable | Required | Description |
| --- | --- | --- |
| `PRICE_TRACKER_STORE_URL` | yes | The shop, e.g. `https://www.example-shop.com`. Only product URLs from this host (with or without `www.`) are accepted. |
| `PRICE_TRACKER_STORE_NAME` | no | Label for the dashboard and report. Defaults to the domain. |
| `PRICE_TRACKER_PRODUCTS` | no* | Product URLs separated by commas or newlines. Takes priority over the products file. |
| `PRICE_TRACKER_PRODUCTS_FILE` | no* | File with one URL per line. Defaults to `products.txt`. |
| `PRICE_TRACKER_DAILY_TIME` | no | Daily collection time, `HH:MM`. Defaults to `10:00`. |
| `PRICE_TRACKER_TIMEZONE` | no | IANA time zone, e.g. `Europe/Athens`. Defaults to `UTC`. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | no | Enable the daily Telegram report. |
| `PRICE_TRACKER_DATABASE` | no | SQLite path. Defaults to `data/prices.sqlite3`. |
| `PRICE_TRACKER_HOST`, `PRICE_TRACKER_PORT` | no | Dashboard address. Defaults to `127.0.0.1:8000`. |

\* Provide your products with one of these two.

The product list is the source of truth: `--sync-products` adds new URLs and **removes products (and their history) that are no longer listed**.

`.env`, `products.txt` and `data/` are git-ignored, so your shop, list and history stay on your machine.

## Running it daily for free with GitHub Actions

`.github/workflows/daily-prices.yml` can do the daily collection and the Telegram report without a server. It is **off by default**. To turn it on, in your fork go to **Settings → Secrets and variables → Actions**:

1. **Secrets:** `PRICE_TRACKER_STORE_URL`, `PRICE_TRACKER_PRODUCTS`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Secrets keep your shop and list private even in a public repo.
2. **Variables:** `PRICE_TRACKER_ENABLED=true`, plus optionally `PRICE_TRACKER_TIMEZONE`, `PRICE_TRACKER_DAILY_TIME` and `PRICE_TRACKER_STORE_NAME`.
3. In the **Actions** tab, run **Daily price collection → Run workflow** once to test it.

The price history is kept in the **Actions cache**, not committed to the repo, so nothing is published. GitHub's scheduled runs are best-effort and often late, so the workflow checks every 20 minutes and collects only once per day. Narrow the cron hours if you want to use fewer runner minutes. If no run happens for 7 days, GitHub evicts the cache and the history starts over.

## Limitations & caveats

- **Scraping is fragile.** If the shop changes its HTML, the parser reports an error instead of saving a guessed price.
- Prices are whatever the public product page shows to an anonymous visitor. Region-, store- or loyalty-card-specific prices aren't handled.
- Offers are detected only when they appear as `N+M` text in the product's own HTML. Offers that are added with JavaScript are missed.
- The report needs a Greek-capable font (DejaVu, Liberation or Arial) on the machine that renders it.

## Be a good citizen

This is meant for **personal use with a small list of products**. Requests are plain HTTPS GETs with a 1-second pause between products, once per day. There's no login, no bypassing of anything, and no bulk catalogue copying. Before pointing it at a site, read that site's terms of use and `robots.txt` and respect them. Don't use it to republish someone else's prices.

## License

MIT, see [LICENSE](LICENSE). No warranty; it's a side project.
