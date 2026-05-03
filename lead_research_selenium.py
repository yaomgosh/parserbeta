#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Compliant marketplace lead research tool (Selenium + regular Chrome).

Modes:
1) Search mode: marketplace,search_url
2) Product mode: marketplace,product_url (manual browsing workflow)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus, urljoin, urlparse

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options


MANUAL_COLUMNS = [
    "best_contact",
    "contact_source",
    "contact_notes",
    "outreach_status",
    "creative_opportunity",
    "lead_score",
]

CONTACT_SEARCH_COLUMNS = [
    "seller_search_query",
    "google_search_url",
    "yandex_search_url",
    "vk_search_url",
    "telegram_search_url",
]


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
    progress_file: Path
    errors_file: Path
    export_every: int
    skip_existing: bool


@dataclass
class RunStats:
    total: int = 0
    processed: int = 0
    saved: int = 0
    skipped: int = 0
    blocked: int = 0
    errors: int = 0
    started_at: float = 0.0


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
            public_emails TEXT,
            public_phones TEXT,
            public_social_links TEXT,
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
    ensure_column(conn, "products", "public_emails", "TEXT")
    ensure_column(conn, "products", "public_phones", "TEXT")
    ensure_column(conn, "products", "public_social_links", "TEXT")
    conn.commit()
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def is_access_blocked(html: str) -> tuple[bool, str]:
    patterns = [
        "captcha",
        "access denied",
        "temporarily blocked",
        "unusual traffic",
        "verify you are human",
        "forbidden",
        "too many requests",
    ]
    lower = html.lower()
    for pattern in patterns:
        if pattern in lower:
            return True, pattern
    return False, ""


def log_blocked_page(
    conn: sqlite3.Connection, marketplace: str, url: str, reason: str, html: str
) -> None:
    conn.execute(
        "INSERT INTO blocked_pages (marketplace, url, reason, html_snippet) VALUES (?, ?, ?, ?)",
        (marketplace, url, reason, html[:3000]),
    )
    conn.commit()


def product_exists(conn: sqlite3.Connection, product_url: str) -> bool:
    row = conn.execute("SELECT 1 FROM products WHERE product_url = ? LIMIT 1", (product_url,)).fetchone()
    return row is not None


def count_products(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0])


def count_blocked_pages(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM blocked_pages").fetchone()[0])


def append_error_csv(path: Path, marketplace: str, url: str, reason: str, detail: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["marketplace", "url", "reason", "detail", "logged_at"])
        writer.writerow([marketplace, url, reason, detail[:1000], time.strftime("%Y-%m-%d %H:%M:%S")])


def progress_bar(done: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return "[" + "-" * width + "]"
    filled = min(width, int(width * done / total))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def write_progress_file(path: Path, stats: RunStats, current_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    elapsed = max(time.time() - stats.started_at, 0.001)
    percent = (stats.processed / stats.total * 100) if stats.total else 0
    rate = stats.processed / elapsed
    remaining = max(stats.total - stats.processed, 0)
    eta_sec = int(remaining / rate) if rate > 0 else 0
    content = "\n".join(
        [
            f"processed={stats.processed}",
            f"total={stats.total}",
            f"percent={percent:.1f}",
            f"saved={stats.saved}",
            f"skipped={stats.skipped}",
            f"blocked={stats.blocked}",
            f"errors={stats.errors}",
            f"eta_sec={eta_sec}",
            f"current_url={current_url}",
            f"updated_at={time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")


def print_progress(stats: RunStats, current_url: str) -> None:
    percent = (stats.processed / stats.total * 100) if stats.total else 0
    bar = progress_bar(stats.processed, stats.total)
    short_url = current_url[:78]
    print(
        f"\r{bar} {stats.processed}/{stats.total} {percent:5.1f}% | "
        f"saved:{stats.saved} skipped:{stats.skipped} blocked:{stats.blocked} errors:{stats.errors} | "
        f"{short_url}",
        end="",
        flush=True,
    )


def slow_scroll(driver: webdriver.Chrome, max_scrolls: int, pause_sec: float) -> None:
    for _ in range(max_scrolls):
        driver.execute_script("window.scrollBy(0, 1000);")
        time.sleep(pause_sec)


def collect_product_links(driver: webdriver.Chrome, base_url: str, limit: int) -> list[str]:
    links: set[str] = set()
    anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    for a in anchors[:2000]:
        href = a.get_attribute("href")
        if not href:
            continue
        candidate = urljoin(base_url, href)
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            continue
        if any(
            token in parsed.path.lower()
            for token in ["product", "item", "dp", "p/", "detail.aspx", "/catalog/"]
        ):
            links.add(candidate.split("#", 1)[0])
        if len(links) >= limit:
            break
    return sorted(links)


def first_text(driver: webdriver.Chrome, selectors: Iterable[str]) -> str:
    for sel in selectors:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            for elem in elems[:3]:
                try:
                    text = elem.text.strip()
                except StaleElementReferenceException:
                    continue
                if text:
                    return re.sub(r"\s+", " ", text)
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            continue
    return ""


def first_attr(driver: webdriver.Chrome, selectors: Iterable[str], attr: str) -> str:
    for sel in selectors:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            for elem in elems[:3]:
                try:
                    val = elem.get_attribute(attr)
                except StaleElementReferenceException:
                    continue
                if val:
                    return val.strip()
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            continue
    return ""


def decode_jsonish_text(value: str) -> str:
    value = unescape(str(value or "")).strip()
    try:
        return json.loads(f'"{value}"').strip()
    except (json.JSONDecodeError, TypeError):
        return value


def first_json_text(html: str, keys: Iterable[str]) -> str:
    for key in keys:
        pattern = rf'["\']{re.escape(key)}["\']\s*:\s*["\']([^"\']+)["\']'
        match = re.search(pattern, html, flags=re.I)
        if match:
            value = decode_jsonish_text(match.group(1))
            if value:
                return re.sub(r"\s+", " ", value)
    return ""


def first_json_number(html: str, keys: Iterable[str]) -> str:
    for key in keys:
        pattern = rf'["\']{re.escape(key)}["\']\s*:\s*(\d+)'
        match = re.search(pattern, html, flags=re.I)
        if match:
            return match.group(1)
    return ""


def clean_seller_name(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    bad_values = {
        "",
        "brands",
        "brand",
        "\u0431\u0440\u0435\u043d\u0434\u044b",
        "\u043f\u0440\u043e\u0434\u0430\u0432\u0430\u0442\u044c \u0442\u043e\u0432\u0430\u0440\u044b",
        "seller.wildberries.ru",
    }
    return "" if value.lower() in bad_values else value


def extract_legal_or_inn(text_blob: str) -> str:
    matches = re.findall(
        r"\b(?:INN|\u0418\u0418\u041d|\u0418\u041d\u041d|VAT|Tax ID)\s*[:#]?\s*([A-Z0-9\-]{6,20})",
        text_blob,
        flags=re.I,
    )
    return matches[0] if matches else ""


def extract_public_contacts(text_blob: str, html: str) -> dict[str, str]:
    emails = sorted(
        set(
            re.findall(
                r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
                text_blob + " " + html,
                flags=re.I,
            )
        )
    )
    phones = sorted(
        set(
            re.findall(
                r"(?:\+7|8)[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-()]?\d{2}[\s\-()]?\d{2}",
                text_blob,
            )
        )
    )
    social_links = sorted(
        set(
            match.rstrip("\\'\"<>),.;")
            for match in re.findall(
                r"https?://(?:www\.)?(?:vk\.com|t\.me|telegram\.me|instagram\.com|youtube\.com|rutube\.ru|ok\.ru)/[^\s\\'\"<>]+",
                html,
                flags=re.I,
            )
        )
    )

    return {
        "public_emails": "; ".join(emails[:5]),
        "public_phones": "; ".join(phones[:5]),
        "public_social_links": "; ".join(social_links[:10]),
    }


def clean_lead_part(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    ignored = {"", "seller.wildberries.ru", "brands", "brand"}
    return "" if value.lower() in ignored else value


def build_contact_search_query(row: pd.Series) -> str:
    parts = [
        clean_lead_part(row.get("seller_shop_name", "")),
        clean_lead_part(row.get("brand", "")),
        clean_lead_part(row.get("public_legal_entity_or_inn", "")),
    ]
    unique_parts = []
    for part in parts:
        if part and part not in unique_parts:
            unique_parts.append(part)
    if not unique_parts:
        unique_parts.append(clean_lead_part(row.get("sample_product_title", "")))
    return " ".join(unique_parts)


def score_creative_opportunity(row: pd.Series) -> str:
    title = f"{row.get('sample_product_title', '')} {row.get('brand', '')}".lower()
    if any(token in title for token in ["clothes", "dress", "fashion", "wear", "\u043e\u0434\u0435\u0436", "\u043f\u043b\u0430\u0442\u044c", "\u043a\u043e\u0441\u0442\u044e\u043c"]):
        return "fashion_visuals"
    if any(token in title for token in ["cosmetic", "beauty", "skin", "\u043a\u043e\u0441\u043c\u0435\u0442", "\u0443\u0445\u043e\u0434"]):
        return "beauty_creatives"
    if any(token in title for token in ["home", "decor", "interior", "\u0434\u043e\u043c", "\u0434\u0435\u043a\u043e\u0440", "\u0438\u043d\u0442\u0435\u0440\u044c\u0435\u0440"]):
        return "lifestyle_product_photos"
    return "product_creatives"


def score_lead(row: pd.Series) -> int:
    score = 0
    if str(row.get("best_contact", "")).strip():
        score += 40
    if str(row.get("seller_search_query", "")).strip():
        score += 20
    try:
        products_seen = int(row.get("products_seen", 0) or 0)
    except ValueError:
        products_seen = 0
    score += min(products_seen * 5, 30)
    if str(row.get("public_legal_entity_or_inn", "")).strip():
        score += 10
    return min(score, 100)


def add_contact_discovery_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        for col in CONTACT_SEARCH_COLUMNS + MANUAL_COLUMNS:
            df[col] = ""
        return df

    contact_cols = ["public_emails", "public_phones", "public_social_links"]
    df["best_contact"] = df[contact_cols].fillna("").agg(
        lambda values: next((value for value in values if str(value).strip()), ""),
        axis=1,
    )
    df["seller_search_query"] = df.apply(build_contact_search_query, axis=1)
    encoded = df["seller_search_query"].map(quote_plus)
    df["google_search_url"] = "https://www.google.com/search?q=" + encoded + "+contacts+social"
    df["yandex_search_url"] = "https://yandex.ru/search/?text=" + encoded + "+contacts"
    df["vk_search_url"] = "https://vk.com/search?c%5Bq%5D=" + encoded
    df["telegram_search_url"] = "https://t.me/s/" + encoded.str.replace("+", "", regex=False).str[:40]
    df["contact_source"] = df["best_contact"].map(lambda value: "marketplace_page" if str(value).strip() else "")
    df["contact_notes"] = ""
    df["outreach_status"] = ""
    df["creative_opportunity"] = df.apply(score_creative_opportunity, axis=1)
    df["lead_score"] = df.apply(score_lead, axis=1)
    return df


def parse_product_page(driver: webdriver.Chrome, marketplace: str, product_url: str, html: str) -> dict[str, str]:
    title = first_text(driver, ["h1", "[data-testid='product-title']", ".product-title", "title"])
    if not title:
        title = first_json_text(html, ["imt_name", "imtName", "name", "goodsName"])
    price = first_text(driver, ["[itemprop='price']", ".price", "[data-testid='price']"])
    rating = first_text(driver, ["[itemprop='ratingValue']", ".rating", "[data-testid='rating']"])
    reviews_count = first_text(driver, ["[itemprop='reviewCount']", ".reviews-count", "[data-testid='reviews']"])
    brand = first_text(driver, ["[itemprop='brand']", ".brand", "[data-testid='brand']"])
    if not brand:
        brand = first_json_text(html, ["brand", "brandName"])
    seller_name = first_text(
        driver,
        ["[data-testid='seller-name']", ".seller-name", "a[href*='seller']", "a[href*='shop']", "a[href*='brands']"],
    )
    seller_name = clean_seller_name(seller_name)
    if not seller_name:
        seller_name = clean_seller_name(first_json_text(html, ["supplierName", "supplier", "sellerName"]))
    seller_profile_url = first_attr(driver, ["a[href*='seller']", "a[href*='shop']", "a[href*='brands']"], "href")
    seller_id = first_json_number(html, ["supplierId", "supplierID", "sellerId"])
    if seller_id:
        seller_profile_url = f"https://www.wildberries.ru/seller/{seller_id}"
    if seller_profile_url:
        seller_profile_url = urljoin(product_url, seller_profile_url)

    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        body_text = ""
    legal_entity_or_inn = extract_legal_or_inn(body_text)
    contacts = extract_public_contacts(body_text, html)

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
        **contacts,
    }


def upsert_product(conn: sqlite3.Connection, search_url: str, data: dict[str, str]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO products (
            marketplace, search_url, product_url, title, price, rating, reviews_count,
            brand, seller_name, seller_profile_url, legal_entity_or_inn,
            public_emails, public_phones, public_social_links
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            data["public_emails"],
            data["public_phones"],
            data["public_social_links"],
        ),
    )
    conn.commit()


def export_deduped_sellers(conn: sqlite3.Connection, output_csv: Path) -> None:
    query = """
    SELECT marketplace, MIN(title) AS sample_product_title, MIN(product_url) AS sample_product_url,
           MIN(price) AS sample_price, MIN(rating) AS sample_rating, MIN(reviews_count) AS sample_reviews_count,
           MIN(brand) AS brand, seller_name AS seller_shop_name, seller_profile_url,
           MIN(legal_entity_or_inn) AS public_legal_entity_or_inn,
           MAX(public_emails) AS public_emails, MAX(public_phones) AS public_phones,
           MAX(public_social_links) AS public_social_links, COUNT(*) AS products_seen
    FROM products
    WHERE seller_name IS NOT NULL AND TRIM(seller_name) != ''
    GROUP BY marketplace, seller_name, seller_profile_url
    ORDER BY products_seen DESC
    """
    df = pd.read_sql_query(query, conn)
    df = add_contact_discovery_columns(df)
    ordered_columns = [
        "lead_score",
        "creative_opportunity",
        "marketplace",
        "seller_shop_name",
        "brand",
        "best_contact",
        "public_emails",
        "public_phones",
        "public_social_links",
        "contact_source",
        "contact_notes",
        "outreach_status",
        "seller_search_query",
        "google_search_url",
        "yandex_search_url",
        "vk_search_url",
        "telegram_search_url",
        "seller_profile_url",
        "public_legal_entity_or_inn",
        "products_seen",
        "sample_product_title",
        "sample_product_url",
        "sample_price",
        "sample_rating",
        "sample_reviews_count",
    ]
    df = df[[col for col in ordered_columns if col in df.columns]]
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False, quoting=csv.QUOTE_MINIMAL)


def process_product_url(
    driver: webdriver.Chrome,
    conn: sqlite3.Connection,
    marketplace: str,
    product_url: str,
    pause_sec: float,
) -> str:
    try:
        driver.get(product_url)
        time.sleep(pause_sec)
        html = driver.page_source
        blocked, reason = is_access_blocked(html)
        if blocked:
            log_blocked_page(conn, marketplace, product_url, reason, html)
            return "blocked"
        upsert_product(conn, "", parse_product_page(driver, marketplace, product_url, html))
        return "saved"
    except (TimeoutException, StaleElementReferenceException, WebDriverException) as exc:
        reason = exc.__class__.__name__
        log_blocked_page(conn, marketplace, product_url, reason, str(exc))
        return "error"


def process_search_page(
    driver: webdriver.Chrome,
    conn: sqlite3.Connection,
    marketplace: str,
    search_url: str,
    cfg: ToolConfig,
) -> None:
    driver.get(search_url)
    time.sleep(cfg.page_pause_sec)

    html = driver.page_source
    blocked, reason = is_access_blocked(html)
    if blocked:
        log_blocked_page(conn, marketplace, search_url, reason, html)
        return

    slow_scroll(driver, cfg.max_scrolls, cfg.scroll_pause_sec)
    product_links = collect_product_links(driver, search_url, cfg.max_products_per_search)

    for link in product_links:
        process_product_url(driver, conn, marketplace, link, cfg.page_pause_sec)


def process_product_rows(
    driver: webdriver.Chrome,
    conn: sqlite3.Connection,
    rows: list[dict[str, str]],
    cfg: ToolConfig,
) -> None:
    stats = RunStats(
        total=sum(1 for row in rows if str(row.get("product_url", "")).strip()),
        saved=count_products(conn),
        blocked=count_blocked_pages(conn),
        started_at=time.time(),
    )
    print(f"Product URLs to process: {stats.total}")

    for row in rows:
        marketplace = row["marketplace"].strip() or "unknown"
        product_url = row["product_url"].strip()
        if not product_url:
            continue

        if cfg.skip_existing and product_exists(conn, product_url):
            stats.processed += 1
            stats.skipped += 1
            print_progress(stats, product_url)
            write_progress_file(cfg.progress_file, stats, product_url)
            continue

        status = process_product_url(driver, conn, marketplace, product_url, cfg.page_pause_sec)
        stats.processed += 1
        if status == "saved":
            stats.saved = count_products(conn)
        elif status == "blocked":
            stats.blocked += 1
        else:
            stats.errors += 1
            append_error_csv(cfg.errors_file, marketplace, product_url, status, "See blocked_pages.html_snippet")

        print_progress(stats, product_url)
        write_progress_file(cfg.progress_file, stats, product_url)

        if cfg.export_every > 0 and stats.processed % cfg.export_every == 0:
            export_deduped_sellers(conn, cfg.output_csv)

    print()


def build_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        # new headless mode for modern Chrome
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    # Selenium 4.6+ uses Selenium Manager to resolve driver automatically
    return webdriver.Chrome(options=options)


def run(cfg: ToolConfig) -> None:
    conn = setup_db(cfg.db_path)
    driver = build_driver(cfg.headless)
    driver.set_page_load_timeout(45)

    try:
        if cfg.product_urls_file:
            process_product_rows(driver, conn, read_product_urls(cfg.product_urls_file), cfg)
        else:
            for row in read_input_urls(cfg.input_csv):
                marketplace = row["marketplace"].strip() or "unknown"
                search_url = row["search_url"].strip()
                if search_url:
                    process_search_page(driver, conn, marketplace, search_url, cfg)
    finally:
        driver.quit()

    export_deduped_sellers(conn, cfg.output_csv)
    conn.close()


def parse_args() -> ToolConfig:
    parser = argparse.ArgumentParser(description="Compliant marketplace lead research tool (Selenium)")
    parser.add_argument("--input", default=Path("input/example_search_urls.csv"), type=Path, help="CSV with marketplace,search_url")
    parser.add_argument("--product-urls-file", type=Path, default=None, help="CSV with marketplace,product_url")
    parser.add_argument("--output", default=Path("output/sellers.csv"), type=Path)
    parser.add_argument("--db", default=Path("output/leads.db"), type=Path)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--max-products-per-search", type=int, default=50)
    parser.add_argument("--max-scrolls", type=int, default=8)
    parser.add_argument("--scroll-pause-sec", type=float, default=1.4)
    parser.add_argument("--page-pause-sec", type=float, default=1.2)
    parser.add_argument("--progress-file", default=Path("output/progress.txt"), type=Path)
    parser.add_argument("--errors-file", default=Path("output/errors.csv"), type=Path)
    parser.add_argument("--export-every", type=int, default=25, help="Export sellers CSV every N processed URLs. Use 0 to disable periodic export.")
    parser.add_argument("--no-skip-existing", action="store_true", help="Revisit product URLs already present in the database.")
    args = parser.parse_args()

    return ToolConfig(
        input_csv=args.input,
        output_csv=args.output,
        db_path=args.db,
        headless=args.headless,
        max_products_per_search=args.max_products_per_search,
        max_scrolls=args.max_scrolls,
        scroll_pause_sec=args.scroll_pause_sec,
        page_pause_sec=args.page_pause_sec,
        product_urls_file=args.product_urls_file,
        progress_file=args.progress_file,
        errors_file=args.errors_file,
        export_every=args.export_every,
        skip_existing=not args.no_skip_existing,
    )


if __name__ == "__main__":
    run(parse_args())
