# SmartMarket

A small, self-hosted tracker for product prices. It records daily prices,
keeps local history in SQLite, and can send a Telegram report showing price
drops, below-average prices, and multi-buy offers converted to an effective
per-item price. A local dashboard displays tracked products and their history.

The repository contains application code and examples only. Do not commit your
`.env`, `products.txt`, or `data/prices.sqlite3`; these are ignored by Git.

## Run locally

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
cp products.example.txt products.txt
```

Edit `.env` and set `PRICE_TRACKER_STORE_URL` to the HTTPS origin of the shop
you want to track. Add one product URL from that shop per line in
`products.txt`. To enable Telegram, add your `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` to `.env`. Then start the dashboard:

```bash
python app.py
```

Open `http://127.0.0.1:8000` and choose **Collect now**. While the app is
running, it also collects once a day at the configured local time. You can
change the time and timezone in `.env`; the defaults are 10:00 and
`Europe/Athens`.

Other useful commands:

```bash
python app.py --sync-products products.txt --collect
python app.py --sync-products products.txt --collect-if-due
python -m unittest discover -s tests
```

`--sync-products` treats the file as the source of truth: removing a URL also
removes that product and its price history from the local database.

## Optional GitHub Actions and Cloudflare scheduling

GitHub Actions can collect prices and send the report without your computer
running. The workflow is opt-in and does not run until you configure the
repository.

1. In **Settings → Secrets and variables → Actions**, add these repository
   secrets:
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` if you want Telegram reports.
   - `PRICE_TRACKER_STORE_URL`: the HTTPS origin of the shop.
   - `PRICE_TRACKER_PRODUCTS`: one product URL from that shop per line. This
     list is written to a temporary, git-ignored `products.txt` during a run.
2. Add the repository variable `PRICE_TRACKER_ENABLED` with value `true`.
   Optionally set `PRICE_TRACKER_DAILY_TIME` and `PRICE_TRACKER_TIMEZONE`.
3. Run **Daily price collection → Run workflow** once from the Actions tab to
   test the setup.

The workflow stores SQLite history in the GitHub Actions cache, not in the
repository. GitHub may evict caches that have not been used for seven days, so
history can restart if scheduled runs stop for that long. The GitHub schedule
is a fallback and currently checks from 07:00 to 11:47 UTC to cover the default
10:00 Athens collection time. If you change the workflow timezone or time,
adjust its cron window too. A daily-run guard prevents a second scheduled
report.

For a more punctual start, the optional Cloudflare Worker in `cloudflare/`
dispatches the same GitHub workflow. It uses two UTC Cron triggers and selects
the one that corresponds to 10:00 in `Europe/Athens` (07:00 UTC in summer,
08:00 UTC in winter). To set it up:

1. In `cloudflare/wrangler.jsonc`, set `vars.GITHUB_REPOSITORY` to your
   `owner/repository` and change `name` if you already use that Worker name.
   The Worker currently targets 10:00 in `Europe/Athens`; change its timezone
   check and Cron triggers if you need a different schedule.
2. Create a fine-grained GitHub token restricted to that repository with
   **Actions: Read and write** permission.
3. From `cloudflare/`, authenticate with Wrangler, save the token as a Worker
   secret, and deploy:

   ```bash
   npx wrangler@latest login
   npx wrangler@latest secret put GITHUB_TOKEN
   npx wrangler@latest deploy
   ```

The Worker URL is public but returns 404; only its scheduled Cron triggers
dispatch the workflow. The token is stored as a Cloudflare secret and must not
be committed. Free services can have execution delays and do not guarantee an
exact start time.

## What it tracks

- One effective price per product per day, including recognized `N+M` offers.
- Changes from the previous recorded price and comparisons with the historical
  average.
- The cheapest weekday observed once enough history is available.
- A Telegram PNG report and a local dashboard.

## Limitations

The tracker validates product URLs against `PRICE_TRACKER_STORE_URL` and reads
common product-page markup. Shops use different page structures, so you may
need to adapt the parser in `app.py` for your shop. Prices are those shown to
an anonymous visitor; the tracker does not select a specific store or region.
Page changes can break parsing, in which case collection reports an error
instead of saving a guessed price.

Use a small product list, respect the shop's terms and `robots.txt`, and do not
republish collected prices without permission.

## License

MIT. See [LICENSE](LICENSE).
