import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent


def load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_dotenv()

try:
    TIMEZONE = ZoneInfo(os.getenv("PRICE_TRACKER_TIMEZONE", "UTC"))
except ZoneInfoNotFoundError as exc:
    raise RuntimeError("PRICE_TRACKER_TIMEZONE must be a valid IANA timezone") from exc

try:
    DAILY_TIME = datetime_time.fromisoformat(
        os.getenv("PRICE_TRACKER_DAILY_TIME", "10:00")
    )
except ValueError as exc:
    raise RuntimeError("PRICE_TRACKER_DAILY_TIME must use HH:MM format") from exc

DATABASE_PATH = Path(
    os.getenv("PRICE_TRACKER_DATABASE", str(ROOT / "data" / "prices.sqlite3"))
)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# The shop whose product pages are tracked, e.g. https://www.example-shop.com.
STORE_URL = os.getenv("PRICE_TRACKER_STORE_URL", "").strip().rstrip("/")
STORE_NAME = os.getenv("PRICE_TRACKER_STORE_NAME", "").strip()
PRODUCTS_FILE = Path(os.getenv("PRICE_TRACKER_PRODUCTS_FILE", str(ROOT / "products.txt")))
USER_AGENT = "Mozilla/5.0 (compatible; smartMarket/1.0)"
WEEKDAYS = ("Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή")


class ScrapeError(Exception):
    pass


class TelegramError(Exception):
    pass


@dataclass(frozen=True)
class Promotion:
    paid_items: int
    free_items: int

    @property
    def label(self) -> str:
        return f"{self.paid_items}+{self.free_items} ΔΩΡΟ"


HOME_BREADCRUMBS = {"αρχική", "home", "homepage", "start"}
OFFER_PATTERN = re.compile(r"(?<!\d)(\d{1,2})\s*\+\s*(\d{1,2})(?!\d)")


class ProductPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.page_title_parts: list[str] = []
        self.json_ld_parts: list[str] = []
        self.visible_text_parts: list[str] = []
        self.product_detail_text_parts: list[str] = []
        self.breadcrumb_categories: list[str] = []
        self.prices: list[str] = []
        self._in_h1 = False
        self._in_title = False
        self._in_json_ld = False
        self._product_detail_depth = 0
        self._product_detail_found = False
        self._breadcrumb_depth = 0
        self._skip_text_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs}
        tag = tag.lower()
        if tag == "section":
            if self._product_detail_depth:
                self._product_detail_depth += 1
            elif "product-detail" in (values.get("class") or "").split():
                self._product_detail_depth = 1
                self._product_detail_found = True
            if self._breadcrumb_depth:
                self._breadcrumb_depth += 1
            elif "breadCrumb" in (values.get("class") or "").split():
                self._breadcrumb_depth = 1
        if self._breadcrumb_depth and tag == "a":
            category = (values.get("title") or "").strip()
            if category and category.casefold() not in HOME_BREADCRUMBS:
                self.breadcrumb_categories.append(category)
        if tag in ("script", "style"):
            self._skip_text_tags.append(tag)
        if tag == "h1":
            self._in_h1 = True
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (values.get("property") or values.get("itemprop") or "").lower()
            if key in ("og:title", "name") and values.get("content"):
                self.title_parts.append(values["content"] or "")
            if key == "price" and values.get("content"):
                self.prices.append(values["content"] or "")
        elif tag == "script" and values.get("type") == "application/ld+json":
            self._in_json_ld = True
            self.json_ld_parts.append("")

        if self._product_detail_depth:
            for key in ("title", "alt", "aria-label"):
                value = values.get(key)
                if value:
                    self.product_detail_text_parts.append(value)
            if values.get("src"):
                self.product_detail_text_parts.append(values["src"] or "")
            for key, value in values.items():
                if value and ("offer" in key or "promotion" in key):
                    self.product_detail_text_parts.append(value)

        data_price = values.get("data-price")
        if data_price and "price" in (values.get("class") or "").lower():
            self.prices.append(data_price)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "section" and self._product_detail_depth:
            self._product_detail_depth -= 1
        if tag == "section" and self._breadcrumb_depth:
            self._breadcrumb_depth -= 1
        if tag == "h1":
            self._in_h1 = False
        elif tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_json_ld = False
        if tag in ("script", "style") and tag in self._skip_text_tags:
            self._skip_text_tags.remove(tag)

    def handle_data(self, data: str) -> None:
        if self._in_h1:
            self.title_parts.append(data)
        if self._in_title:
            self.page_title_parts.append(data)
        if self._in_json_ld and self.json_ld_parts:
            self.json_ld_parts[-1] += data
        if self._skip_text_tags:
            return
        if data.strip():
            self.visible_text_parts.append(data)
            if self._product_detail_depth:
                self.product_detail_text_parts.append(data)


def _json_ld_values(value: object) -> tuple[list[str], list[str]]:
    names: list[str] = []
    prices: list[str] = []
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str):
            names.append(name)
        offer = value.get("offers")
        if isinstance(offer, dict) and offer.get("price") is not None:
            prices.append(str(offer["price"]))
        elif isinstance(offer, list):
            for item in offer:
                child_names, child_prices = _json_ld_values(item)
                names.extend(child_names)
                prices.extend(child_prices)
        for key in ("@graph", "mainEntity"):
            child_names, child_prices = _json_ld_values(value.get(key))
            names.extend(child_names)
            prices.extend(child_prices)
    elif isinstance(value, list):
        for item in value:
            child_names, child_prices = _json_ld_values(item)
            names.extend(child_names)
            prices.extend(child_prices)
    return names, prices


def parse_price(raw: str) -> Decimal:
    cleaned = re.sub(r"[^\d,.-]", "", raw.strip())
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ScrapeError("The product page has an invalid price") from exc
    if not value.is_finite() or value <= 0:
        raise ScrapeError("The product page has an invalid price")
    return value.quantize(Decimal("0.01"))


def parse_promotion(source: str) -> Promotion | None:
    for match in OFFER_PATTERN.finditer(source):
        paid_items, free_items = (int(value) for value in match.groups())
        if 0 < paid_items <= 20 and 0 < free_items <= 20:
            return Promotion(paid_items, free_items)
    return None


def calculate_unit_price(price: Decimal, promotion: Promotion | None) -> Decimal:
    if promotion is None:
        return price
    total_items = promotion.paid_items + promotion.free_items
    return (
        price * Decimal(promotion.paid_items) / Decimal(total_items)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_promotion_savings(
    price: Decimal, promotion: Promotion | None
) -> tuple[Decimal, Decimal]:
    if promotion is None:
        return Decimal("0.00"), Decimal("0.00")
    unit_price = calculate_unit_price(price, promotion)
    savings_per_item = (price - unit_price).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    savings_per_bundle = (price * Decimal(promotion.free_items)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return savings_per_item, savings_per_bundle


def parse_product_html(
    source: str,
) -> tuple[str, Decimal, Promotion | None, str | None]:
    parser = ProductPageParser()
    parser.feed(source)

    names = [name.strip() for name in parser.title_parts if name.strip()]
    prices = list(parser.prices)
    for raw in parser.json_ld_parts:
        try:
            structured = json.loads(raw)
        except json.JSONDecodeError:
            continue
        json_names, json_prices = _json_ld_values(structured)
        names.extend(name.strip() for name in json_names if name.strip())
        prices.extend(json_prices)

    name = next((name for name in names if name), "")
    if not name:
        name = " ".join(parser.page_title_parts).strip()
    if not name:
        raise ScrapeError("The product page does not contain a product name")
    if not prices:
        raise ScrapeError("The product page does not contain a product price")
    offer_text = (
        parser.product_detail_text_parts
        if parser._product_detail_found
        else parser.visible_text_parts
    )
    offer_text.extend(parser.json_ld_parts)
    promotion = parse_promotion(" ".join(offer_text))
    category = parser.breadcrumb_categories[-1] if parser.breadcrumb_categories else None
    return name, parse_price(prices[0]), promotion, category


def store_host(store_url: str | None = None) -> str:
    store_url = STORE_URL if store_url is None else store_url
    if not store_url:
        raise ValueError("Set PRICE_TRACKER_STORE_URL to the shop you want to track")
    parts = urlsplit(store_url if "://" in store_url else f"https://{store_url}")
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("PRICE_TRACKER_STORE_URL must be an HTTPS address")
    return parts.hostname.lower()


def store_display_name() -> str:
    if STORE_NAME:
        return STORE_NAME
    try:
        return store_host().removeprefix("www.")
    except ValueError:
        return "Store"


def normalize_product_url(raw_url: str, store_url: str | None = None) -> str:
    host = store_host(store_url)
    bare_host = host.removeprefix("www.")
    try:
        parts = urlsplit(raw_url.strip())
    except ValueError as exc:
        raise ValueError("Enter a valid product URL") from exc
    if (
        parts.scheme != "https"
        or (parts.hostname or "").lower() not in (bare_host, f"www.{bare_host}")
        or parts.username
        or parts.password
        or not parts.path.strip("/")
    ):
        raise ValueError(f"Only HTTPS product URLs from {bare_host} are allowed")
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return urlunsplit(("https", host, path, parts.query, ""))


def init_database(db_path: Path = DATABASE_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL UNIQUE,
                name TEXT,
                category TEXT,
                last_checked_at TEXT,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                observed_on TEXT NOT NULL,
                price REAL NOT NULL,
                offer TEXT,
                unit_price REAL,
                savings_per_item REAL,
                savings_per_bundle REAL,
                UNIQUE(product_id, observed_on)
            );
            CREATE INDEX IF NOT EXISTS observations_product_date
                ON observations(product_id, observed_on);
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        observation_columns = {
            row[1] for row in db.execute("PRAGMA table_info(observations)").fetchall()
        }
        product_columns = {
            row[1] for row in db.execute("PRAGMA table_info(products)").fetchall()
        }
        if "category" not in product_columns:
            db.execute("ALTER TABLE products ADD COLUMN category TEXT")
        if "offer" not in observation_columns:
            db.execute("ALTER TABLE observations ADD COLUMN offer TEXT")
        if "unit_price" not in observation_columns:
            db.execute("ALTER TABLE observations ADD COLUMN unit_price REAL")
        if "savings_per_item" not in observation_columns:
            db.execute("ALTER TABLE observations ADD COLUMN savings_per_item REAL")
        if "savings_per_bundle" not in observation_columns:
            db.execute("ALTER TABLE observations ADD COLUMN savings_per_bundle REAL")
        db.execute("PRAGMA foreign_keys = ON")
        db.commit()


def connect_database(db_path: Path = DATABASE_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def get_state(key: str, db_path: Path = DATABASE_PATH) -> str | None:
    with closing(connect_database(db_path)) as db:
        row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(key: str, value: str, db_path: Path = DATABASE_PATH) -> None:
    with closing(connect_database(db_path)) as db:
        db.execute(
            "INSERT INTO app_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        db.commit()


def summarize_history(
    observations: list[tuple[str, float]],
) -> tuple[Decimal | None, str | None]:
    if not observations:
        return None, None
    prices = [Decimal(str(price)) for _, price in observations]
    average = sum(prices) / len(prices)
    weekday_prices: dict[int, list[Decimal]] = {}
    for observed_on, price in observations:
        weekday = date.fromisoformat(observed_on).weekday()
        weekday_prices.setdefault(weekday, []).append(Decimal(str(price)))
    if len(weekday_prices) < 2:
        return average, None
    best_weekday = min(
        weekday_prices,
        key=lambda weekday: sum(weekday_prices[weekday]) / len(weekday_prices[weekday]),
    )
    return average, WEEKDAYS[best_weekday]


def get_products_payload(db_path: Path = DATABASE_PATH) -> list[dict[str, object]]:
    with closing(connect_database(db_path)) as db:
        products = db.execute(
            "SELECT id, url, name, category, last_checked_at, last_error "
            "FROM products ORDER BY rowid"
        ).fetchall()
        result: list[dict[str, object]] = []
        for product in products:
            rows = db.execute(
                "SELECT observed_on, price, offer, unit_price, "
                "savings_per_item, savings_per_bundle FROM observations "
                "WHERE product_id = ? ORDER BY observed_on",
                (product["id"],),
            ).fetchall()
            observations = [
                (
                    row["observed_on"],
                    row["unit_price"] if row["unit_price"] is not None else row["price"],
                )
                for row in rows
            ]
            average, best_weekday = summarize_history(observations)
            latest = rows[-1] if rows else None
            current_price = (
                Decimal(
                    str(
                        latest["unit_price"]
                        if latest["unit_price"] is not None
                        else latest["price"]
                    )
                )
                if latest
                else None
            )
            result.append(
                {
                    "id": product["id"],
                    "url": product["url"],
                    "name": product["name"],
                    "category": product["category"],
                    "last_checked_at": product["last_checked_at"],
                    "last_error": product["last_error"],
                    "current_price": float(current_price) if current_price is not None else None,
                    "current_list_price": latest["price"] if latest else None,
                    "current_offer": latest["offer"] if latest else None,
                    "current_savings_per_item": (
                        latest["savings_per_item"] or 0 if latest else 0
                    ),
                    "current_savings_per_bundle": (
                        latest["savings_per_bundle"] or 0 if latest else 0
                    ),
                    "average_price": float(average) if average is not None else None,
                    "below_average": (
                        current_price < average if current_price is not None and average else False
                    ),
                    "best_weekday": best_weekday,
                    "history": [
                        {
                            "date": row["observed_on"],
                            "price": (
                                row["unit_price"]
                                if row["unit_price"] is not None
                                else row["price"]
                            ),
                            "list_price": row["price"],
                            "offer": row["offer"],
                            "savings_per_item": row["savings_per_item"] or 0,
                            "savings_per_bundle": row["savings_per_bundle"] or 0,
                        }
                        for row in rows[-14:]
                    ],
                }
            )
    return result


def parse_product_list(text: str) -> set[str]:
    """Parse product URLs separated by newlines and/or commas. Lines starting with # are skipped."""
    urls: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        for entry in line.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                urls.add(normalize_product_url(entry))
            except ValueError as exc:
                raise ValueError(
                    f"Invalid product URL on line {line_number}: {exc}"
                ) from exc
    return urls


def read_product_list(products_file: Path | None = None) -> set[str]:
    """Read product URLs from the given file, PRICE_TRACKER_PRODUCTS, or products.txt."""
    if products_file is None:
        inline = os.getenv("PRICE_TRACKER_PRODUCTS", "").strip()
        if inline:
            return parse_product_list(inline)
        products_file = PRODUCTS_FILE
    if not products_file.is_file():
        raise ValueError(
            f"Products file not found: {products_file}. "
            "Create it or set PRICE_TRACKER_PRODUCTS."
        )
    return parse_product_list(products_file.read_text(encoding="utf-8"))


def sync_products_from_file(
    products_file: Path | None = None, db_path: Path = DATABASE_PATH
) -> tuple[int, int]:
    return sync_products(read_product_list(products_file), db_path)


def sync_products(urls: set[str], db_path: Path = DATABASE_PATH) -> tuple[int, int]:
    with closing(connect_database(db_path)) as db:
        existing = {
            row["url"]: row["id"]
            for row in db.execute("SELECT id, url FROM products").fetchall()
        }
        added = 0
        for url in urls - existing.keys():
            db.execute(
                "INSERT INTO products(id, url) VALUES(?, ?)",
                (str(uuid.uuid4()), url),
            )
            added += 1
        removed = set(existing) - urls
        if removed:
            db.executemany(
                "DELETE FROM products WHERE url = ?",
                [(url,) for url in removed],
            )
        db.commit()
    return added, len(removed)


def scrape_product(
    url: str, opener: object
) -> tuple[str, Decimal, Promotion | None, str | None]:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with opener.open(request, timeout=25) as response:
            if response.status != 200:
                raise ScrapeError(f"The product page returned HTTP {response.status}")
            body = response.read(2_000_001)
    except HTTPError as exc:
        raise ScrapeError(f"The product page returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise ScrapeError(f"Could not reach the product page: {exc.reason}") from exc
    if len(body) > 2_000_000:
        raise ScrapeError("The product page is larger than the 2 MB limit")
    return parse_product_html(body.decode("utf-8", errors="replace"))


def format_price(price: Decimal) -> str:
    return f"{price:.2f} €"


def format_table_price(price: Decimal | float | None) -> str:
    if price is None:
        return "—"
    return f"{Decimal(str(price)):.2f}".replace(".", ",") + "€"


def load_report_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    font_names = (
        ("DejaVuSans-Bold.ttf", "arialbd.ttf")
        if bold
        else ("DejaVuSans.ttf", "arial.ttf")
    )
    font_paths = [
        Path("/usr/share/fonts/truetype/dejavu") / name for name in font_names
    ] + [
        Path("/usr/share/fonts/truetype/liberation2") / name
        for name in (
            ("LiberationSans-Bold.ttf",)
            if bold
            else ("LiberationSans-Regular.ttf",)
        )
    ] + [
        Path("C:/Windows/Fonts") / name for name in font_names
    ] + [
        Path("/System/Library/Fonts/Supplemental") / name for name in font_names
    ]
    font_path = next((path for path in font_paths if path.is_file()), None)
    if font_path is None:
        raise TelegramError("A Greek-capable font is required for the Telegram image")
    return ImageFont.truetype(str(font_path), size)


def wrap_report_text(
    text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if font.getlength(candidate) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        part = ""
        for character in word:
            if part and font.getlength(part + character) > max_width:
                lines.append(part)
                part = character
            else:
                part += character
        current = part
    if current:
        lines.append(current)
    return lines or [""]


def build_telegram_report_rows(
    successes: list[dict[str, object]],
    db_path: Path = DATABASE_PATH,
) -> list[tuple[list[str], bool, bool]]:
    today = datetime.now(TIMEZONE).date().isoformat()
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in successes:
        category = str(item.get("category") or "Χωρίς κατηγορία")
        grouped.setdefault(category, []).append(item)

    rows: list[tuple[list[str], bool, bool]] = []
    for category in sorted(grouped, key=str.casefold):
        for item in sorted(
            grouped[category],
            key=lambda product: (
                Decimal(str(product["unit_price"])),
                str(product["name"]).casefold(),
            ),
        ):
            history = get_history(str(item["id"]), db_path)
            previous_history = [
                (observed_on, price)
                for observed_on, price in history
                if observed_on < today
            ]
            average, _ = summarize_history(previous_history)
            previous_price = (
                Decimal(str(previous_history[-1][1])) if previous_history else None
            )
            unit_price = Decimal(str(item["unit_price"]))
            below_average = average is not None and unit_price < average
            day_over_day_drop = (
                previous_price is not None and unit_price < previous_price
            )
            markers = []
            if day_over_day_drop:
                markers.append("↓")
            if below_average:
                markers.append("★")
            unit_price_text = " ".join(markers + [format_table_price(unit_price)])
            rows.append(
                (
                    [
                        str(item["name"]),
                        unit_price_text,
                        format_table_price(Decimal(str(item["price"]))),
                        str(item["offer"] or "—"),
                    ],
                    below_average,
                    day_over_day_drop,
                )
            )
    return rows


def build_telegram_report(
    successes: list[dict[str, object]],
    failures: list[dict[str, str]],
    db_path: Path = DATABASE_PATH,
) -> tuple[bytes, str]:
    local_now = datetime.now(TIMEZONE)
    title = f"Τιμές {store_display_name()} — {local_now:%d/%m/%Y}"
    caption = (
        f"{title}\n↓ Κάτω από την τελευταία τιμή · "
        "★ κάτω από τον ιστορικό μέσο όρο."
    )
    columns = [
        ("Προϊόν", 410),
        ("Τελική τιμή / τεμ.", 190),
        ("Κανονική τιμή / τεμ.", 190),
        ("Προσφορά", 200),
    ]
    table_width = sum(width for _, width in columns)
    rows = build_telegram_report_rows(successes, db_path)

    title_font = load_report_font(32, bold=True)
    subtitle_font = load_report_font(18)
    header_font = load_report_font(18, bold=True)
    body_font = load_report_font(19)
    failure_font = load_report_font(17)
    margin = 30
    padding = 14
    line_height = 27
    header_lines = [
        wrap_report_text(label, header_font, width - 2 * padding)
        for label, width in columns
    ]
    header_height = max(map(len, header_lines)) * line_height + 2 * padding
    laid_out_rows = []
    for values, below_average, day_over_day_drop in rows:
        wrapped = [
            wrap_report_text(value, body_font, width - 2 * padding)
            for value, (_, width) in zip(values, columns)
        ]
        row_height = max(map(len, wrapped)) * line_height + 2 * padding
        laid_out_rows.append(
            (wrapped, below_average, day_over_day_drop, row_height)
        )

    failure_lines = [
        wrap_report_text(
            f"{failure['name']}: {failure['error']}",
            failure_font,
            table_width - 2 * padding,
        )
        for failure in failures
    ]
    table_top = 112
    image_height = table_top + header_height + sum(
        row[3] for row in laid_out_rows
    ) + 28
    if failure_lines:
        image_height += 52 + sum(
            max(1, len(lines)) * 25 + 12 for lines in failure_lines
        )
    image = Image.new(
        "RGB",
        (table_width + 2 * margin, image_height),
        "#F3F6F9",
    )
    draw = ImageDraw.Draw(image)
    draw.text((margin, 24), title, font=title_font, fill="#17324D")
    draw.text(
        (margin, 70),
        "↓ Κάτω από την τελευταία τιμή · ★ κάτω από τον ιστορικό μέσο όρο",
        font=subtitle_font,
        fill="#526579",
    )

    y = table_top
    draw.rectangle(
        (margin, y, margin + table_width, y + header_height),
        fill="#24527A",
    )
    x = margin
    for index, (_, width) in enumerate(columns):
        for line_index, line in enumerate(header_lines[index]):
            draw.text(
                (x + padding, y + padding + line_index * line_height),
                line,
                font=header_font,
                fill="#FFFFFF",
            )
        x += width
        if index < len(columns) - 1:
            draw.line((x, y, x, y + header_height), fill="#6686A2", width=1)
    y += header_height

    for row_index, (
        wrapped,
        below_average,
        day_over_day_drop,
        row_height,
    ) in enumerate(laid_out_rows):
        background = "#FFFFFF" if row_index % 2 == 0 else "#EAF0F5"
        draw.rectangle(
            (margin, y, margin + table_width, y + row_height),
            fill=background,
        )
        x = margin
        for column_index, ((_, width), cell_lines) in enumerate(
            zip(columns, wrapped)
        ):
            if column_index == 1 and (below_average or day_over_day_drop):
                draw.rectangle(
                    (x, y, x + width, y + row_height),
                    fill="#DDF2E5" if day_over_day_drop else "#E3EFF9",
                )
            for line_index, line in enumerate(cell_lines):
                draw.text(
                    (x + padding, y + padding + line_index * line_height),
                    line,
                    font=body_font,
                    fill=(
                        "#17633A"
                        if column_index == 1 and day_over_day_drop
                        else "#24527A"
                        if column_index == 1 and below_average
                        else "#263746"
                    ),
                )
            x += width
            if column_index < len(columns) - 1:
                draw.line((x, y, x, y + row_height), fill="#D4DDE5", width=1)
        draw.line(
            (margin, y + row_height, margin + table_width, y + row_height),
            fill="#D4DDE5",
            width=1,
        )
        y += row_height

    if failure_lines:
        y += 14
        draw.text(
            (margin, y),
            "Δεν διαβάστηκαν τιμές",
            font=header_font,
            fill="#9B2C2C",
        )
        y += 38
        for wrapped in failure_lines:
            for line in wrapped:
                draw.text(
                    (margin, y),
                    line,
                    font=failure_font,
                    fill="#7B2D2D",
                )
                y += 25
            y += 12

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), caption


def get_history(product_id: str, db_path: Path = DATABASE_PATH) -> list[tuple[str, float]]:
    with closing(connect_database(db_path)) as db:
        rows = db.execute(
            "SELECT observed_on, COALESCE(unit_price, price) AS unit_price FROM observations "
            "WHERE product_id = ? ORDER BY observed_on",
            (product_id,),
        ).fetchall()
    return [(row["observed_on"], row["unit_price"]) for row in rows]


def send_telegram_photo(photo: bytes, caption: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise TelegramError("Telegram bot token or chat ID is not configured")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    boundary = f"----price-tracker-{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="chat_id"\r\n\r\n',
            TELEGRAM_CHAT_ID.encode(),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="caption"\r\n\r\n',
            caption.encode("utf-8"),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="photo"; filename="prices.png"\r\n',
            b"Content-Type: image/png\r\n\r\n",
            photo,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    request = Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with build_opener().open(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise TelegramError(f"Telegram returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise TelegramError(f"Could not reach Telegram: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise TelegramError("Telegram returned an invalid response") from exc
    if not result.get("ok"):
        raise TelegramError("Telegram rejected the image; check the bot and chat settings")


def run_collection(
    send_message: bool = True, db_path: Path = DATABASE_PATH
) -> dict[str, object]:
    with closing(connect_database(db_path)) as db:
        products = db.execute(
            "SELECT id, url, name FROM products ORDER BY rowid"
        ).fetchall()
    if not products:
        raise ValueError("Add at least one product URL before collecting prices")

    today = datetime.now(TIMEZONE).date().isoformat()
    checked_at = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    successes: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    opener = build_opener(HTTPCookieProcessor(CookieJar()))

    for index, product in enumerate(products):
        if index:
            time.sleep(1)
        try:
            name, price, promotion, category = scrape_product(product["url"], opener)
        except ScrapeError as exc:
            error = str(exc)
            failures.append(
                {"id": product["id"], "name": product["name"] or product["url"], "error": error}
            )
            with closing(connect_database(db_path)) as db:
                db.execute(
                    "UPDATE products SET last_checked_at = ?, last_error = ? WHERE id = ?",
                    (checked_at, error, product["id"]),
                )
                db.commit()
            continue

        unit_price = calculate_unit_price(price, promotion)
        savings_per_item, savings_per_bundle = calculate_promotion_savings(
            price, promotion
        )
        offer = promotion.label if promotion else None
        with closing(connect_database(db_path)) as db:
            db.execute(
                "UPDATE products SET name = ?, category = ?, last_checked_at = ?, "
                "last_error = NULL WHERE id = ?",
                (name, category, checked_at, product["id"]),
            )
            db.execute(
                "INSERT INTO observations("
                "product_id, observed_on, price, offer, unit_price, "
                "savings_per_item, savings_per_bundle"
                ") VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(product_id, observed_on) DO UPDATE SET "
                "price = excluded.price, offer = excluded.offer, "
                "unit_price = excluded.unit_price, "
                "savings_per_item = excluded.savings_per_item, "
                "savings_per_bundle = excluded.savings_per_bundle",
                (
                    product["id"],
                    today,
                    float(price),
                    offer,
                    float(unit_price),
                    float(savings_per_item),
                    float(savings_per_bundle),
                ),
            )
            db.commit()
        successes.append(
            {
                "id": product["id"],
                "name": name,
                "category": category,
                "price": float(price),
                "unit_price": float(unit_price),
                "offer": offer,
                "savings_per_item": float(savings_per_item),
                "savings_per_bundle": float(savings_per_bundle),
                "url": product["url"],
            }
        )

    telegram_sent = False
    telegram_error: str | None = None
    if send_message:
        try:
            image, caption = build_telegram_report(successes, failures, db_path)
            send_telegram_photo(image, caption)
            telegram_sent = True
        except TelegramError as exc:
            telegram_error = str(exc)

    errors = [failure["error"] for failure in failures]
    if telegram_error:
        errors.append(telegram_error)
    set_state("last_run_at", checked_at, db_path)
    set_state("last_run_error", "; ".join(errors), db_path)
    set_state("last_telegram_sent", "true" if telegram_sent else "false", db_path)
    return {
        "collected": len(successes),
        "failed": len(failures),
        "products": successes,
        "failures": failures,
        "telegram_sent": telegram_sent,
        "telegram_error": telegram_error,
    }


def app_status() -> dict[str, object]:
    return {
        "timezone": str(TIMEZONE),
        "daily_time": DAILY_TIME.strftime("%H:%M"),
        "store_name": store_display_name(),
        "store_url": STORE_URL,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "last_run_at": get_state("last_run_at"),
        "last_run_error": get_state("last_run_error"),
        "last_telegram_sent": get_state("last_telegram_sent"),
    }


def is_scheduled_collection_due(
    now: datetime,
    last_scheduled_date: str | None,
    daily_time: datetime_time = DAILY_TIME,
) -> bool:
    local_now = now.astimezone(TIMEZONE)
    return (
        local_now.time() >= daily_time
        and last_scheduled_date != local_now.date().isoformat()
    )


class TrackerHandler(BaseHTTPRequestHandler):
    server_version = "smartMarket/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        logging.info("%s - %s", self.address_string(), fmt % args)

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request body length") from exc
        if length <= 0 or length > 10_000:
            raise ValueError("Request body must be between 1 byte and 10 KB")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        route = urlsplit(self.path).path
        if route == "/":
            self.serve_static("index.html", "text/html; charset=utf-8")
        elif route == "/static/app.js":
            self.serve_static("app.js", "text/javascript; charset=utf-8")
        elif route == "/static/style.css":
            self.serve_static("style.css", "text/css; charset=utf-8")
        elif route == "/api/status":
            self.send_json(200, app_status())
        elif route == "/api/products":
            self.send_json(200, get_products_payload())
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        try:
            if route == "/api/products":
                payload = self.read_json()
                raw_url = payload.get("url")
                if not isinstance(raw_url, str):
                    raise ValueError("Product URL is required")
                url = normalize_product_url(raw_url)
                product_id = str(uuid.uuid4())
                with closing(connect_database()) as db:
                    try:
                        db.execute(
                            "INSERT INTO products(id, url) VALUES(?, ?)",
                            (product_id, url),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("This product URL is already being tracked") from exc
                    db.commit()
                self.send_json(201, {"id": product_id, "url": url})
            elif route == "/api/collect":
                self.read_json()
                self.send_json(200, run_collection())
            else:
                self.send_json(404, {"error": "Not found"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except sqlite3.Error:
            logging.exception("Database request failed")
            self.send_json(500, {"error": "Database request failed; check the server log"})

    def do_DELETE(self) -> None:
        route = urlsplit(self.path).path
        prefix = "/api/products/"
        if not route.startswith(prefix):
            self.send_json(404, {"error": "Not found"})
            return
        product_id = route[len(prefix) :]
        if not product_id or "/" in product_id:
            self.send_json(404, {"error": "Not found"})
            return
        try:
            with closing(connect_database()) as db:
                cursor = db.execute("DELETE FROM products WHERE id = ?", (product_id,))
                db.commit()
            if not cursor.rowcount:
                self.send_json(404, {"error": "Product not found"})
                return
            self.send_json(200, {"deleted": True})
        except sqlite3.Error:
            logging.exception("Product removal failed")
            self.send_json(500, {"error": "Product removal failed; check the server log"})

    def serve_static(self, filename: str, content_type: str) -> None:
        body = (ROOT / "static" / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def scheduler_loop() -> None:
    while True:
        now = datetime.now(TIMEZONE)
        today = now.date().isoformat()
        last_scheduled_date = get_state("last_scheduled_date")
        if now.time() >= DAILY_TIME and last_scheduled_date != today:
            with closing(connect_database()) as db:
                has_products = db.execute("SELECT 1 FROM products LIMIT 1").fetchone() is not None
            if has_products:
                set_state("last_scheduled_date", today)
                logging.info("Starting scheduled price collection for %s", today)
                try:
                    run_collection()
                except (ValueError, sqlite3.Error):
                    logging.exception("Scheduled collection could not start")
                    set_state("last_run_at", now.isoformat(timespec="seconds"))
                    set_state("last_run_error", "Scheduled collection failed; check the server log")
                last_scheduled_date = today
        next_run = datetime.combine(now.date(), DAILY_TIME, tzinfo=TIMEZONE)
        if next_run <= now:
            next_run += timedelta(days=1)
        seconds_until_run = (next_run - now).total_seconds()
        time.sleep(max(1, min(30, seconds_until_run)))


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="smartMarket: a tiny daily price tracker")
    collection = parser.add_mutually_exclusive_group()
    collection.add_argument(
        "--collect",
        action="store_true",
        help="Collect prices immediately and send the Telegram report",
    )
    collection.add_argument(
        "--collect-if-due",
        action="store_true",
        help="Collect only when the configured local daily time has arrived",
    )
    parser.add_argument(
        "--sync-products",
        type=Path,
        nargs="?",
        const=None,
        default=False,
        metavar="FILE",
        help=(
            "Synchronize tracked products. Reads FILE if given, otherwise "
            "PRICE_TRACKER_PRODUCTS, otherwise products.txt"
        ),
    )
    args = parser.parse_args()

    init_database()
    if args.sync_products is not False:
        try:
            added, removed = sync_products_from_file(args.sync_products)
        except (OSError, ValueError, sqlite3.Error) as exc:
            parser.exit(1, f"Could not sync products: {exc}\n")
        logging.info("Synchronized products: %d added, %d removed", added, removed)

    if args.collect or args.collect_if_due:
        now = datetime.now(TIMEZONE)
        if args.collect_if_due and not is_scheduled_collection_due(
            now, get_state("last_scheduled_date")
        ):
            logging.info("Daily collection is not due yet or has already run today")
            return
        if args.collect_if_due:
            with closing(connect_database()) as db:
                has_products = db.execute("SELECT 1 FROM products LIMIT 1").fetchone() is not None
            if not has_products:
                logging.info("No products configured; skipping scheduled collection")
                return
        try:
            result = run_collection()
        except (ValueError, sqlite3.Error) as exc:
            parser.exit(1, f"Price collection failed: {exc}\n")
        if args.collect_if_due:
            set_state("last_scheduled_date", now.date().isoformat())
        logging.info(
            "Collected %d prices; %d failed; Telegram sent: %s",
            result["collected"],
            result["failed"],
            result["telegram_sent"],
        )
        if result["failures"] or result["telegram_error"]:
            parser.exit(1, "Price collection or Telegram delivery failed\n")
        return

    from threading import Thread

    Thread(target=scheduler_loop, name="daily-price-scheduler", daemon=True).start()
    host = os.getenv("PRICE_TRACKER_HOST", "127.0.0.1")
    port = int(os.getenv("PRICE_TRACKER_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), TrackerHandler)
    logging.info("Dashboard available at http://%s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
