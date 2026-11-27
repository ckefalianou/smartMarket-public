import sqlite3
import unittest
from datetime import datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from PIL import Image

from app import (
    Promotion,
    ScrapeError,
    STORE_NAME,
    build_telegram_report,
    build_telegram_report_rows,
    calculate_promotion_savings,
    calculate_unit_price,
    get_products_payload,
    init_database,
    is_scheduled_collection_due,
    normalize_product_url,
    parse_product_html,
    run_collection,
    send_telegram_photo,
    sync_products_from_file,
    summarize_history,
)


class ProductParsingTests(unittest.TestCase):
    def test_extracts_name_and_euro_price_from_product_markup(self) -> None:
        source = """
        <html>
          <head><meta property="og:title" content="ΜΑΡΑΤΑ Γάλα Πλήρες 1lt"></head>
          <body><div class="main-price"><div class="price" data-price="1,05">1,05 €</div></div></body>
        </html>
        """
        name, price, promotion, category = parse_product_html(source)
        self.assertEqual(name, "ΜΑΡΑΤΑ Γάλα Πλήρες 1lt")
        self.assertEqual(price, Decimal("1.05"))
        self.assertIsNone(promotion)
        self.assertIsNone(category)

    def test_extracts_two_plus_one_offer_and_calculates_effective_item_price(self) -> None:
        source = """
        <section class="product-detail">
          <h1>Χαρτοπετσέτες</h1>
          <div class="promotion-badge">2+1 ΔΩΡΟ</div>
          <div class="price" data-price="1,50">1,50 €/τεμ.</div>
        </section>
        <section class="related-products">3+1 ΔΩΡΟ</section>
        """
        name, price, promotion, category = parse_product_html(source)
        self.assertEqual(name, "Χαρτοπετσέτες")
        self.assertEqual(promotion, Promotion(2, 1))
        self.assertIsNone(category)
        self.assertEqual(calculate_unit_price(price, promotion), Decimal("1.00"))
        self.assertEqual(
            calculate_promotion_savings(price, promotion),
            (Decimal("0.50"), Decimal("1.50")),
        )

    def test_ignores_offers_outside_the_main_product_detail(self) -> None:
        source = """
        <section class="product-detail">
          <h1>Χαρτοπετσέτες</h1>
          <div class="price" data-price="1,50">1,50 €/τεμ.</div>
        </section>
        <section class="related-products">2+1 ΔΩΡΟ</section>
        """
        _, _, promotion, _ = parse_product_html(source)
        self.assertIsNone(promotion)

    def test_extracts_the_most_specific_product_category_from_breadcrumbs(self) -> None:
        source = """
        <section class="toolBar breadCrumb">
          <ul>
            <li><a title="Αρχική">Αρχική</a></li>
            <li><a title="Χαρτικά">Χαρτικά</a></li>
            <li><a title="Χαρτοπετσέτες">Χαρτοπετσέτες</a></li>
          </ul>
        </section>
        <section class="product-detail">
          <h1>Χαρτοπετσέτες</h1>
          <div class="price" data-price="1,50">1,50 €/τεμ.</div>
        </section>
        """
        _, _, _, category = parse_product_html(source)
        self.assertEqual(category, "Χαρτοπετσέτες")

    def test_rejects_product_page_without_price(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "does not contain a product price"):
            parse_product_html("<h1>Προϊόν</h1>")

    def test_normalizes_configured_store_product_url(self) -> None:
        self.assertEqual(
            normalize_product_url("https://example.com/product/example#details"),
            "https://www.example.com/product/example/",
        )

    def test_rejects_urls_from_a_different_store(self) -> None:
        with self.assertRaisesRegex(ValueError, "Only HTTPS"):
            normalize_product_url("https://another-example.com/product")


class PriceHistoryTests(unittest.TestCase):
    def test_averages_prices_and_selects_lowest_weekday(self) -> None:
        average, best_weekday = summarize_history(
            [
                ("2026-11-23", 2.0),  # Monday
                ("2026-11-24", 1.5),  # Tuesday
                ("2026-11-30", 1.8),  # Monday
            ]
        )
        self.assertEqual(average, Decimal("1.766666666666666666666666667"))
        self.assertEqual(best_weekday, "Τρίτη")

    def test_waits_for_multiple_weekdays_before_reporting_a_best_day(self) -> None:
        average, best_weekday = summarize_history([("2026-11-23", 2.0)])
        self.assertEqual(average, Decimal("2.0"))
        self.assertIsNone(best_weekday)

    def test_scheduled_collection_runs_once_at_the_configured_local_time(self) -> None:
        timezone = ZoneInfo("Europe/Athens")
        before_schedule = datetime(2026, 11, 24, 9, 59, tzinfo=timezone)
        scheduled_time = datetime(2026, 11, 24, 10, 0, tzinfo=timezone)
        self.assertFalse(is_scheduled_collection_due(before_schedule, None, time(10, 0)))
        self.assertTrue(is_scheduled_collection_due(scheduled_time, None, time(10, 0)))
        self.assertFalse(
            is_scheduled_collection_due(scheduled_time, "2026-11-24", time(10, 0))
        )


class ProductSyncTests(unittest.TestCase):
    def test_sync_adds_products_and_removes_urls_no_longer_in_the_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "prices.sqlite3"
            product_file = root / "products.txt"
            init_database(database)

            first_url = "https://www.example.com/product/first/"
            second_url = "https://www.example.com/product/second/"
            product_file.write_text(f"{first_url}\n{second_url}\n", encoding="utf-8")
            self.assertEqual(sync_products_from_file(product_file, database), (2, 0))

            product_file.write_text(f"{first_url}\n", encoding="utf-8")
            self.assertEqual(sync_products_from_file(product_file, database), (0, 1))


class PromotionCollectionTests(unittest.TestCase):
    def test_collection_saves_offer_and_uses_effective_price_for_comparison(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "prices.sqlite3"
            init_database(database)
            with sqlite3.connect(database) as db:
                db.execute(
                    "INSERT INTO products(id, url) VALUES(?, ?)",
                    ("napkins", "https://www.example.com/product/napkins/"),
                )
                db.commit()

            with (
                patch(
                    "app.scrape_product",
                    return_value=(
                        "Χαρτοπετσέτες",
                        Decimal("1.50"),
                        Promotion(2, 1),
                        "Χαρτοπετσέτες",
                    ),
                ),
                patch("app.send_telegram_photo") as send_photo,
            ):
                result = run_collection(db_path=database)

            self.assertEqual(result["collected"], 1)
            self.assertEqual(result["products"][0]["unit_price"], 1.0)
            product = get_products_payload(database)[0]
            self.assertEqual(product["current_price"], 1.0)
            self.assertEqual(product["category"], "Χαρτοπετσέτες")
            self.assertEqual(product["current_list_price"], 1.5)
            self.assertEqual(product["current_offer"], "2+1 ΔΩΡΟ")
            self.assertEqual(product["current_savings_per_item"], 0.5)
            self.assertEqual(product["current_savings_per_bundle"], 1.5)
            send_photo.assert_called_once()
            self.assertTrue(send_photo.call_args.args[0].startswith(b"\x89PNG"))
            self.assertIn(f"Τιμές {STORE_NAME}", send_photo.call_args.args[1])


class TelegramTableTests(unittest.TestCase):
    def test_sorts_products_by_category_then_offer_price_without_category_rows(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "prices.sqlite3"
            init_database(database)
            products = [
                {
                    "id": "coffee-high",
                    "name": "Καφές Α",
                    "category": "Καφές",
                    "price": 2.0,
                    "unit_price": 2.0,
                    "offer": None,
                    "savings_per_item": 0.0,
                    "savings_per_bundle": 0.0,
                },
                {
                    "id": "coffee-low",
                    "name": "Καφές Β",
                    "category": "Καφές",
                    "price": 1.0,
                    "unit_price": 1.0,
                    "offer": None,
                    "savings_per_item": 0.0,
                    "savings_per_bundle": 0.0,
                },
                {
                    "id": "water",
                    "name": "Νερό",
                    "category": "Νερά",
                    "price": 0.5,
                    "unit_price": 0.5,
                    "offer": "2+1 ΔΩΡΟ",
                    "savings_per_item": 0.1,
                    "savings_per_bundle": 0.3,
                },
            ]
            with sqlite3.connect(database) as db:
                db.execute(
                    "INSERT INTO products(id, url) VALUES(?, ?)",
                    ("water", "https://www.example.com/water/"),
                )
                db.execute(
                    "INSERT INTO observations(product_id, observed_on, price, unit_price) "
                    "VALUES(?, ?, ?, ?)",
                    ("water", "2026-11-23", 0.6, 0.6),
                )

            rows = build_telegram_report_rows(products, database)

            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0][0][0], "Καφές Β")
            self.assertEqual(rows[1][0][0], "Καφές Α")
            self.assertEqual(rows[2][0][0], "Νερό")
            self.assertEqual(rows[2][0][1], "↓ ★ 0,50€")
            self.assertEqual(rows[2][0][3], "2+1 ΔΩΡΟ")
            self.assertTrue(rows[2][1])
            self.assertTrue(rows[2][2])
            self.assertEqual(len(rows[2][0]), 4)
            photo, caption = build_telegram_report(products, [], database)
            with Image.open(BytesIO(photo)) as image:
                self.assertEqual(image.format, "PNG")
                self.assertGreater(image.width, image.height)
            self.assertIn(f"Τιμές {STORE_NAME}", caption)

    def test_marks_a_daily_drop_even_when_equal_to_the_historical_average(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "prices.sqlite3"
            init_database(database)
            today = datetime.now(ZoneInfo("Europe/Athens")).date()
            observations = [
                ((today - timedelta(days=2)).isoformat(), 2.0),
                ((today - timedelta(days=1)).isoformat(), 4.0),
            ]
            with sqlite3.connect(database) as db:
                db.execute(
                    "INSERT INTO products(id, url) VALUES(?, ?)",
                    ("coffee", "https://www.example.com/coffee/"),
                )
                db.executemany(
                    "INSERT INTO observations(product_id, observed_on, price, unit_price) "
                    "VALUES(?, ?, ?, ?)",
                    [
                        ("coffee", observed_on, price, price)
                        for observed_on, price in observations
                    ],
                )

            rows = build_telegram_report_rows(
                [
                    {
                        "id": "coffee",
                        "name": "Καφές",
                        "category": "Καφές",
                        "price": 3.0,
                        "unit_price": 3.0,
                        "offer": None,
                    }
                ],
                database,
            )

            self.assertEqual(rows[0][0][1], "↓ 3,00€")
            self.assertFalse(rows[0][1])
            self.assertTrue(rows[0][2])

    def test_sends_png_as_a_telegram_photo(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        opener = MagicMock()
        opener.open.return_value = response
        with (
            patch("app.TELEGRAM_BOT_TOKEN", "test-token"),
            patch("app.TELEGRAM_CHAT_ID", "test-chat"),
            patch("app.build_opener", return_value=opener),
        ):
            send_telegram_photo(b"png-bytes", f"Τιμές {STORE_NAME}")

        request = opener.open.call_args.args[0]
        self.assertIn("multipart/form-data", request.get_header("Content-type"))
        self.assertIn(b'name="photo"; filename="prices.png"', request.data)
        self.assertIn(b"png-bytes", request.data)


class DatabaseMigrationTests(unittest.TestCase):
    def test_existing_database_gains_promotion_columns(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "old-prices.sqlite3"
            with sqlite3.connect(database) as db:
                db.executescript(
                    """
                    CREATE TABLE products (
                        id TEXT PRIMARY KEY,
                        url TEXT NOT NULL UNIQUE,
                        name TEXT,
                        last_checked_at TEXT,
                        last_error TEXT
                    );
                    CREATE TABLE observations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        product_id TEXT NOT NULL,
                        observed_on TEXT NOT NULL,
                        price REAL NOT NULL,
                        UNIQUE(product_id, observed_on)
                    );
                    """
                )

            init_database(database)
            with sqlite3.connect(database) as db:
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(observations)")
                }
                product_columns = {
                    row[1] for row in db.execute("PRAGMA table_info(products)")
                }
            self.assertTrue({"offer", "unit_price", "savings_per_item", "savings_per_bundle"}.issubset(columns))
            self.assertIn("category", product_columns)


if __name__ == "__main__":
    unittest.main()
