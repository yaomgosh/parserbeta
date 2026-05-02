#!/usr/bin/env python3
"""Compliant marketplace lead research tool.

Supports:
1) search mode: discover product URLs from category/search pages.
2) product mode: process pre-collected product URLs (manual browsing workflow).
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import pandas as pd
from playwright.sync_api import Browser, Page, sync_playwright

MANUAL_COLUMNS = ["contact_source", "contact", "outreach_status", "creative_opportunity", "lead_score"]


@dataclass
class ToolConfig:
    input_csv: Path
    output_csv: Path
    db_path: Path
    headless: bool
    max_products_per_search: int
    max_scrolls: int
    scroll_pause_sec: float
    page_pause_sec: float
    product_urls_file: Path | None
    browser_channel: str | None


def read_input_urls(path: Path) -> list[dict[str, str]]:
    df = pd.read_csv(path)
    required = {"marketplace", "search_url"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required input columns: {sorted(missing)}")
    return df.fillna("").to_dict(orient="records")


def read_product_urls(path: Path) -> list[dict[str, str]]:
    df = pd.read_csv(path)
    required = {"marketplace", "product_url"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required product file columns: {sorted(missing)}")
    return df.fillna("").to_dict(orient="records")


def setup_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marketplace TEXT,
            search_url TEXT,
            product_url TEXT UNIQUE,
            title TEXT,
            price TEXT,
            rating TEXT,
            reviews_count TEXT,
            brand TEXT,
            seller_name TEXT,
            seller_profile_url TEXT,
            legal_entity_or_inn TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blocked_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marketplace TEXT,
            url TEXT,
            reason TEXT,
            html_snippet TEXT,
            logged_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


def is_access_blocked(html: str) -> tuple[bool, str]:
    patterns = ["captcha", "access denied", "temporarily blocked", "unusual traffic", "verify you are human", "forbidden", "too many requests"]
    lower = html.lower()
    for pattern in patterns:
        if pattern in lower:
            return True, pattern
    return False, ""


def log_blocked_page(conn: sqlite3.Connection, marketplace: str, url: str, reason: str, html: str) -> None:
    conn.execute(
        "INSERT INTO blocked_pages (marketplace, url, reason, html_snippet) VALUES (?, ?, ?, ?)",
        (marketplace, url, reason, html[:3000]),
    )
    conn.commit()


def slow_scroll(page: Page, max_scrolls: int, pause_sec: float) -> None:
    for _ in range(max_scrolls):
        page.mouse.wheel(0, 1000)
        time.sleep(pause_sec)


def collect_product_links(page: Page, base_url: str, limit: int) -> list[str]:
    anchors = page.locator("a[href]")
    links: set[str] = set()
    for idx in range(min(anchors.count(), 2000)):
        href = anchors.nth(idx).get_attribute("href")
        if not href:
            continue
        candidate = urljoin(base_url, href)
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            continue
        if any(token in parsed.path.lower() for token in ["product", "item", "dp", "p/", "detail.aspx", "/catalog/"]):
            links.add(candidate.split("#", 1)[0])
        if len(links) >= limit:
            break
    return sorted(links)


def first_text(page: Page, selectors: Iterable[str]) -> str:
    for sel in selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            text = loc.first.inner_text().strip()
            if text:
                return re.sub(r"\s+", " ", text)
    return ""


def first_attr(page: Page, selectors: Iterable[str], attr: str) -> str:
    for sel in selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            val = loc.first.get_attribute(attr)
            if val:
                return val.strip()
    return ""


def extract_legal_or_inn(text_blob: str) -> str:
    matches = re.findall(r"\b(?:INN|ИИН|ИНН|VAT|Tax ID)\s*[:#]?\s*([A-Z0-9\-]{6,20})", text_blob, flags=re.I)
    return matches[0] if matches else ""


def parse_product_page(page: Page, marketplace: str, product_url: str) -> dict[str, str]:
    title = first_text(page, ["h1", "[data-testid='product-title']", ".product-title", "title"])
    price = first_text(page, ["[itemprop='price']", ".price", "[data-testid='price']"])
    rating = first_text(page, ["[itemprop='ratingValue']", ".rating", "[data-testid='rating']"])
    reviews_count = first_text(page, ["[itemprop='reviewCount']", ".reviews-count", "[data-testid='reviews']"])
    brand = first_text(page, ["[itemprop='brand']", ".brand", "[data-testid='brand']"])
    seller_name = first_text(page, ["[data-testid='seller-name']", ".seller-name", "a[href*='seller']", "a[href*='shop']", "a[href*='brands']"])
    seller_profile_url = first_attr(page, ["a[href*='seller']", "a[href*='shop']", "a[href*='brands']"], "href")
    if seller_profile_url:
        seller_profile_url = urljoin(product_url, seller_profile_url)

    legal_entity_or_inn = extract_legal_or_inn(page.inner_text("body"))

    return {
        "marketplace": marketplace,
        "product_title": title,
        "product_url": product_url,
        "price": price,
        "rating": rating,
        "reviews_count": reviews_count,
        "brand": brand,
        "seller_shop_name": seller_name,
        "seller_profile_url": seller_profile_url,
        "public_legal_entity_or_inn": legal_entity_or_inn,
    }


def upsert_product(conn: sqlite3.Connection, search_url: str, data: dict[str, str]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO products (
            marketplace, search_url, product_url, title, price, rating, reviews_count,
            brand, seller_name, seller_profile_url, legal_entity_or_inn
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["marketplace"],
            search_url,
            data["product_url"],
            data["product_title"],
            data["price"],
            data["rating"],
            data["reviews_count"],
            data["brand"],
            data["seller_shop_name"],
            data["seller_profile_url"],
            data["public_legal_entity_or_inn"],
        ),
    )
    conn.commit()


def export_deduped_sellers(conn: sqlite3.Connection, output_csv: Path) -> None:
    query = """
    SELECT marketplace, MIN(title) AS sample_product_title, MIN(product_url) AS sample_product_url,
           MIN(price) AS sample_price, MIN(rating) AS sample_rating, MIN(reviews_count) AS sample_reviews_count,
           MIN(brand) AS brand, seller_name AS seller_shop_name, seller_profile_url,
           MIN(legal_entity_or_inn) AS public_legal_entity_or_inn, COUNT(*) AS products_seen
    FROM products
    WHERE seller_name IS NOT NULL AND TRIM(seller_name) != ''
    GROUP BY marketplace, seller_name, seller_profile_url
    ORDER BY products_seen DESC
    """
    df = pd.read_sql_query(query, conn)
    for col in MANUAL_COLUMNS:
        df[col] = ""
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False, quoting=csv.QUOTE_MINIMAL)


def process_product_url(page: Page, conn: sqlite3.Connection, marketplace: str, product_url: str, pause_sec: float) -> None:
    page.goto(product_url, wait_until="domcontentloaded", timeout=45_000)
    time.sleep(pause_sec)
    html = page.content()
    blocked, reason = is_access_blocked(html)
    if blocked:
        log_blocked_page(conn, marketplace, product_url, reason, html)
        return
    upsert_product(conn, "", parse_product_page(page, marketplace, product_url))


def process_search_page(page: Page, conn: sqlite3.Connection, marketplace: str, search_url: str, cfg: ToolConfig) -> None:
    page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
    time.sleep(cfg.page_pause_sec)
    html = page.content()
    blocked, reason = is_access_blocked(html)
    if blocked:
        log_blocked_page(conn, marketplace, search_url, reason, html)
        return
    slow_scroll(page, cfg.max_scrolls, cfg.scroll_pause_sec)
    for link in collect_product_links(page, search_url, cfg.max_products_per_search):
        process_product_url(page, conn, marketplace, link, cfg.page_pause_sec)


def run(cfg: ToolConfig) -> None:
    conn = setup_db(cfg.db_path)

    with sync_playwright() as p:
        launch_kwargs = {"headless": cfg.headless}
        if cfg.browser_channel:
            launch_kwargs["channel"] = cfg.browser_channel
        browser: Browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(45_000)

        if cfg.product_urls_file:
            for row in read_product_urls(cfg.product_urls_file):
                marketplace = row["marketplace"].strip() or "unknown"
                product_url = row["product_url"].strip()
                if product_url:
                    process_product_url(page, conn, marketplace, product_url, cfg.page_pause_sec)
        else:
            for row in read_input_urls(cfg.input_csv):
                marketplace = row["marketplace"].strip() or "unknown"
                search_url = row["search_url"].strip()
                if search_url:
                    process_search_page(page, conn, marketplace, search_url, cfg)

        context.close()
        browser.close()

    export_deduped_sellers(conn, cfg.output_csv)
    conn.close()


def parse_args() -> ToolConfig:
    parser = argparse.ArgumentParser(description="Compliant marketplace lead research tool")
    parser.add_argument("--input", default=Path("input/example_search_urls.csv"), type=Path, help="CSV with marketplace,search_url")
    parser.add_argument("--product-urls-file", type=Path, default=None, help="CSV with marketplace,product_url for manual browsing workflow")
    parser.add_argument("--output", default=Path("output/sellers.csv"), type=Path)
    parser.add_argument("--db", default=Path("output/leads.db"), type=Path)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--channel", default=None, help="Playwright browser channel, e.g. chrome")
    parser.add_argument("--max-products-per-search", type=int, default=50)
    parser.add_argument("--max-scrolls", type=int, default=8)
    parser.add_argument("--scroll-pause-sec", type=float, default=1.4)
    parser.add_argument("--page-pause-sec", type=float, default=1.2)
    args = parser.parse_args()
    return ToolConfig(args.input, args.output, args.db, args.headless, args.max_products_per_search, args.max_scrolls, args.scroll_pause_sec, args.page_pause_sec, args.product_urls_file, args.channel)


if __name__ == "__main__":
    run(parse_args())
