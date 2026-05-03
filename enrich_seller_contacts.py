#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Enrich seller leads with likely official social/profile/contact URLs.

Input:  output/sellers.csv from lead_research_selenium.py
Output: output/sellers_enriched.csv

The script searches public web results, filters marketplace-owned contacts,
and assigns a confidence score based on seller/brand/topic matches.
"""

from __future__ import annotations

import argparse
import csv
import html
import http.client
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse, urlunparse
from urllib.request import Request, urlopen

import pandas as pd


SEARCH_URLS = [
    ("duckduckgo", "https://duckduckgo.com/html/?q={query}"),
    ("bing", "https://www.bing.com/search?q={query}"),
]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0 Safari/537.36"
)

PLATFORMS = {
    "instagram": ["instagram.com"],
    "vk": ["vk.com"],
    "telegram": ["t.me", "telegram.me"],
    "facebook": ["facebook.com", "fb.com"],
    "avito": ["avito.ru"],
}

BLOCKED_DOMAINS = {
    "wildberries.ru",
    "www.wildberries.ru",
    "seller.wildberries.ru",
    "wb.ru",
    "www.wb.ru",
    "market.yandex.ru",
    "ozon.ru",
    "www.ozon.ru",
    "seller.ozon.ru",
}

SOCIAL_NOISE_WORDS = {
    "wildberries",
    "club9695053",
}

PROFILE_PATH_BLOCKLIST = {
    "instagram": {"explore", "p", "reel", "stories", "accounts", "about", "developer"},
    "vk": {"feed", "public", "search", "away.php", "al_feed.php", "market"},
    "telegram": {"share", "addstickers", "iv", "proxy", "joinchat"},
    "facebook": {"share", "sharer", "groups", "pages", "watch", "marketplace"},
    "avito": {"brands", "shops", "rossiya"},
}

GENERIC_WORDS = {
    "shop",
    "store",
    "brand",
    "brands",
    "official",
    "online",
    "seller",
    "wildberries",
    "wb",
    "market",
    "marketplace",
    "home",
    "club",
    "group",
    "sale",
    "original",
    "russia",
    "russian",
    "moscow",
    "spb",
    "the",
    "and",
    "ooo",
    "llc",
}

GENERIC_CYRILLIC = {
    "бренд",
    "бренды",
    "магазин",
    "официальный",
    "товары",
    "товар",
    "продавец",
    "продавать",
    "маркетплейс",
    "одежда",
    "женский",
    "мужской",
    "детский",
    "купить",
    "цена",
    "скидка",
    "россия",
    "москва",
    "санкт",
    "петербург",
}


@dataclass
class Candidate:
    platform: str
    url: str
    title: str
    snippet: str
    score: int
    evidence: str


@dataclass
class EnrichStats:
    total: int = 0
    processed: int = 0
    enriched: int = 0
    skipped: int = 0
    errors: int = 0
    started_at: float = 0.0


def fetch_text(url: str, timeout: int = 20) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def strip_tags(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def normalize_url(url: str) -> str:
    url = html.unescape(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            url = unquote(target)
            parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return url.split("#", 1)[0]


def parse_duckduckgo_results(page_html: str, limit: int) -> list[tuple[str, str, str]]:
    if "challenge-form" in page_html or "anomaly.js" in page_html:
        return []

    results: list[tuple[str, str, str]] = []
    blocks = re.findall(r'<div[^>]+class="[^"]*result[^"]*"[\s\S]*?</div>\s*</div>', page_html, flags=re.I)
    if not blocks:
        blocks = re.findall(r'<a[^>]+class="[^"]*result__a[^"]*"[\s\S]*?</a>', page_html, flags=re.I)

    for block in blocks:
        href_match = re.search(r'href="([^"]+)"', block, flags=re.I)
        if not href_match:
            continue
        url = normalize_url(href_match.group(1))
        if not url:
            continue
        title_match = re.search(r'class="[^"]*result__a[^"]*"[^>]*>([\s\S]*?)</a>', block, flags=re.I)
        snippet_match = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>([\s\S]*?)</a>', block, flags=re.I)
        title = strip_tags(title_match.group(1) if title_match else block)
        snippet = strip_tags(snippet_match.group(1) if snippet_match else "")
        results.append((url, title, snippet))
        if len(results) >= limit:
            break

    return dedupe_results(results)


def parse_bing_results(page_html: str, limit: int) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    blocks = re.findall(r'<li[^>]+class="[^"]*b_algo[^"]*"[\s\S]*?</li>', page_html, flags=re.I)
    for block in blocks:
        link_match = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', block, flags=re.I)
        if not link_match:
            continue
        url = normalize_url(link_match.group(1))
        if not url:
            continue
        title = strip_tags(link_match.group(2))
        snippet_match = re.search(r"<p[^>]*>([\s\S]*?)</p>", block, flags=re.I)
        snippet = strip_tags(snippet_match.group(1) if snippet_match else "")
        results.append((url, title, snippet))
        if len(results) >= limit:
            break
    return dedupe_results(results)


def dedupe_results(results: Iterable[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for url, title, snippet in results:
        key = urlparse(url)._replace(query="").geturl().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        unique.append((url, title, snippet))
    return unique


def search_web(query: str, limit: int, pause_sec: float) -> list[tuple[str, str, str]]:
    all_results: list[tuple[str, str, str]] = []
    for engine, template in SEARCH_URLS:
        url = template.format(query=quote_plus(query))
        try:
            page_html = fetch_text(url)
        except (HTTPError, URLError, TimeoutError, http.client.RemoteDisconnected):
            continue
        time.sleep(pause_sec)
        if engine == "duckduckgo":
            results = parse_duckduckgo_results(page_html, limit)
        else:
            results = parse_bing_results(page_html, limit)
        all_results = dedupe_results([*all_results, *results])
        if len(all_results) >= limit:
            break
    return all_results[:limit]


def append_error_csv(path: Path, seller: str, platform: str, reason: str, detail: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["seller", "platform", "reason", "detail", "logged_at"])
        writer.writerow([seller, platform, reason, detail[:1000], time.strftime("%Y-%m-%d %H:%M:%S")])


def progress_bar(done: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return "[" + "-" * width + "]"
    filled = min(width, int(width * done / total))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def write_progress_file(path: Path, stats: EnrichStats, current_seller: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    elapsed = max(time.time() - stats.started_at, 0.001)
    percent = (stats.processed / stats.total * 100) if stats.total else 0
    rate = stats.processed / elapsed
    remaining = max(stats.total - stats.processed, 0)
    eta_sec = int(remaining / rate) if rate > 0 else 0
    path.write_text(
        "\n".join(
            [
                f"processed={stats.processed}",
                f"total={stats.total}",
                f"percent={percent:.1f}",
                f"enriched={stats.enriched}",
                f"skipped={stats.skipped}",
                f"errors={stats.errors}",
                f"eta_sec={eta_sec}",
                f"current_seller={current_seller}",
                f"updated_at={time.strftime('%Y-%m-%d %H:%M:%S')}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def print_progress(stats: EnrichStats, current_seller: str) -> None:
    percent = (stats.processed / stats.total * 100) if stats.total else 0
    safe_print(
        f"{progress_bar(stats.processed, stats.total)} {stats.processed}/{stats.total} {percent:5.1f}% | "
        f"enriched:{stats.enriched} skipped:{stats.skipped} errors:{stats.errors} | {current_seller}"
    )


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def platform_for_url(url: str) -> str:
    domain = domain_of(url)
    for platform, domains in PLATFORMS.items():
        if any(domain == item or domain.endswith("." + item) for item in domains):
            return platform
    return "website"


def is_blocked_domain(url: str) -> bool:
    domain = domain_of(url)
    return any(domain == item or domain.endswith("." + item) for item in BLOCKED_DOMAINS)


def profile_handle(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return ""
    first = path.split("/", 1)[0].lower()
    if first in {"s"} and path.count("/") >= 1:
        first = path.split("/", 1)[1].split("/", 1)[0].lower()
    return first.lstrip("@")


def is_social_noise(url: str, title: str = "", snippet: str = "", text: str = "") -> bool:
    url_bits = f"{url} {title} {snippet}".lower()
    if any(word in url_bits for word in SOCIAL_NOISE_WORDS):
        return True
    platform = platform_for_url(url)
    handle = profile_handle(url)
    if handle in PROFILE_PATH_BLOCKLIST.get(platform, set()):
        return True
    if platform in {"telegram", "instagram", "vk", "facebook"} and not handle:
        return True
    return False


def seller_handle_variants(row: pd.Series) -> list[str]:
    names = quoted_parts(
        str(row.get("seller_shop_name", "") or ""),
        str(row.get("brand", "") or ""),
        str(row.get("seller_search_query", "") or ""),
    )
    variants: list[str] = []
    for name in names:
        compact = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ]", "", name).lower()
        latin = re.sub(r"[^a-zA-Z0-9]", "", name).lower()
        for base in [compact, latin]:
            if len(base) < 4:
                continue
            for candidate in [base, f"{base}s", f"{base}ss", f"{base}shop", f"{base}store"]:
                if candidate not in variants:
                    variants.append(candidate)
    return variants[:12]


def tokenize(value: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}", str(value or "").lower())
    stop = GENERIC_WORDS | GENERIC_CYRILLIC
    return [token for token in tokens if token not in stop]


def unique_tokens(*values: str) -> list[str]:
    result: list[str] = []
    for value in values:
        for token in tokenize(value):
            if token not in result:
                result.append(token)
    return result


def quoted_parts(*values: str) -> list[str]:
    parts = []
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
        if cleaned and cleaned.lower() not in GENERIC_WORDS and cleaned.lower() not in GENERIC_CYRILLIC:
            parts.append(cleaned)
    return parts


def score_candidate(
    row: pd.Series,
    url: str,
    title: str,
    snippet: str,
    fetched_text: str = "",
) -> tuple[int, str]:
    haystack = f"{url} {title} {snippet} {fetched_text}".lower()
    seller = str(row.get("seller_shop_name", "") or "")
    brand = str(row.get("brand", "") or "")
    product = str(row.get("sample_product_title", "") or "")
    inn = str(row.get("public_legal_entity_or_inn", "") or "").strip()

    identity_tokens = unique_tokens(seller, brand)
    topic_tokens = unique_tokens(product)
    evidence: list[str] = []
    score = 0

    if is_blocked_domain(url):
        return 0, "blocked_marketplace_domain"
    if is_social_noise(url, title, snippet, fetched_text):
        return 0, "social_or_marketplace_noise"

    domain = domain_of(url)
    if platform_for_url(url) != "website":
        score += 15
        evidence.append(f"platform:{platform_for_url(url)}")
    elif domain:
        score += 8
        evidence.append("website_candidate")

    for phrase in quoted_parts(seller, brand):
        if phrase.lower() in haystack:
            score += 35
            evidence.append(f"exact:{phrase}")

    identity_matches = [token for token in identity_tokens if token in haystack]
    topic_matches = [token for token in topic_tokens if token in haystack]
    handle = profile_handle(url)
    handle_matches = [token for token in identity_tokens if token and token in handle]
    if handle_matches:
        score += min(len(handle_matches) * 22, 44)
        evidence.append("handle:" + ",".join(handle_matches[:5]))
    for variant in seller_handle_variants(row):
        if variant and variant in handle:
            score += 35
            evidence.append(f"handle_variant:{variant}")
            break
    if identity_matches:
        score += min(len(identity_matches) * 12, 36)
        evidence.append("identity:" + ",".join(identity_matches[:5]))
    if topic_matches:
        score += min(len(topic_matches) * 5, 20)
        evidence.append("topic:" + ",".join(topic_matches[:5]))
    if inn and inn.lower() in haystack:
        score += 45
        evidence.append("inn_match")

    if not identity_matches and not handle_matches and not any(item.startswith("exact:") for item in evidence):
        score -= 30
        evidence.append("weak_identity_match")
    if "wildberries" in haystack or "seller.wildberries" in haystack:
        score -= 20
        evidence.append("marketplace_noise")

    return max(0, min(score, 100)), "; ".join(evidence)


def fetch_candidate_text(url: str, pause_sec: float, timeout: int = 12) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    try:
        text = strip_tags(fetch_text(url, timeout=timeout))[:5000]
    except (HTTPError, URLError, TimeoutError, UnicodeError, http.client.RemoteDisconnected):
        return ""
    time.sleep(pause_sec)
    return text


def candidate_is_live(url: str, pause_sec: float) -> tuple[bool, str]:
    platform = platform_for_url(url)
    if platform == "telegram":
        check_url = url
        handle = profile_handle(url)
        if handle:
            check_url = f"https://t.me/s/{handle}"
        text = fetch_candidate_text(check_url, pause_sec, timeout=4)
        lower = text.lower()
        if not text or "tgme_channel_info" not in lower and "telegram" not in lower:
            return False, "telegram_not_loaded"
        if any(token in lower for token in ["channel not found", "username not found", "this channel is unavailable"]):
            return False, "telegram_not_found"
        return True, "telegram_live"

    if platform in {"vk", "instagram", "facebook", "avito", "website"}:
        text = fetch_candidate_text(url, pause_sec, timeout=6)
        lower = text.lower()
        if not text:
            return False, "empty_or_blocked"
        missing_markers = [
            "page not found",
            "profile not found",
            "this page isn't available",
            "страница не найдена",
            "пользователь не найден",
            "ничего не найдено",
        ]
        if any(marker in lower for marker in missing_markers):
            return False, "not_found_marker"
        return True, "live"

    return True, "not_checked"


def build_queries(row: pd.Series, platform: str) -> list[str]:
    seller = str(row.get("seller_shop_name", "") or "").strip()
    brand = str(row.get("brand", "") or "").strip()
    product = str(row.get("sample_product_title", "") or "").strip()
    identity = " ".join(quoted_parts(seller, brand)) or str(row.get("seller_search_query", "") or "").strip()
    if not identity:
        return []

    domains = PLATFORMS.get(platform, [])
    site_part = f"site:{domains[0]}" if domains else ""
    topic_tokens = " ".join(unique_tokens(product)[:4])
    queries = [
        f'"{identity}" {site_part}'.strip(),
        f'"{identity}" {topic_tokens} {site_part}'.strip(),
    ]
    if platform == "website":
        queries = [
            f'"{identity}" официальный сайт',
            f'"{identity}" contacts',
            f'"{identity}" {topic_tokens}',
        ]
    return list(dict.fromkeys(query for query in queries if query.strip()))


def build_queries(row: pd.Series, platform: str) -> list[str]:
    seller = str(row.get("seller_shop_name", "") or "").strip()
    brand = str(row.get("brand", "") or "").strip()
    product = str(row.get("sample_product_title", "") or "").strip()
    identity = " ".join(quoted_parts(seller, brand)) or str(row.get("seller_search_query", "") or "").strip()
    if not identity:
        return []

    domains = PLATFORMS.get(platform, [])
    site_part = f"site:{domains[0]}" if domains else ""
    topic_tokens = " ".join(unique_tokens(product)[:4])
    queries = [
        f'"{identity}" {site_part}'.strip(),
        f'"{identity}" {topic_tokens} {site_part}'.strip(),
        f"{identity} {platform} contacts".strip(),
        f"{identity} {platform} official".strip(),
        f"{identity} {platform} контакты".strip(),
        f"{identity} {platform} официальный".strip(),
    ]
    for handle in seller_handle_variants(row)[:6]:
        queries.append(f"{handle} {site_part}".strip())
        queries.append(f"{handle} {platform}".strip())
    if platform == "website":
        queries = [
            f'"{identity}" official website',
            f'"{identity}" contacts',
            f'"{identity}" официальный сайт',
            f'"{identity}" контакты',
            f'"{identity}" {topic_tokens}',
        ]
    return list(dict.fromkeys(query for query in queries if query.strip()))


def direct_profile_candidates(row: pd.Series, platform: str) -> list[str]:
    candidates: list[str] = []
    for handle in seller_handle_variants(row):
        if platform == "telegram":
            candidates.append(f"https://t.me/{handle}")
        elif platform == "instagram":
            candidates.append(f"https://www.instagram.com/{handle}")
        elif platform == "vk":
            candidates.append(f"https://vk.com/{handle}")
        elif platform == "facebook":
            candidates.append(f"https://www.facebook.com/{handle}")
        elif platform == "avito":
            candidates.append(f"https://www.avito.ru/brands/{handle}")
    return candidates


def choose_best_candidate(
    row: pd.Series,
    platform: str,
    results_per_query: int,
    pause_sec: float,
    min_confidence: int,
) -> Candidate | None:
    best: Candidate | None = None
    for url in direct_profile_candidates(row, platform):
        live, live_reason = candidate_is_live(url, pause_sec)
        if not live:
            continue
        fetched_text = fetch_candidate_text(url if platform != "telegram" else f"https://t.me/s/{profile_handle(url)}", pause_sec)
        score, evidence = score_candidate(row, url, "", "", fetched_text)
        candidate = Candidate(platform, canonical_url(url), "", "", score, f"{evidence}; {live_reason}")
        if best is None or candidate.score > best.score:
            best = candidate
        if best.score >= 100:
            break

    if best and best.score >= 90:
        return best

    for query in build_queries(row, platform):
        for url, title, snippet in search_web(query, results_per_query, pause_sec):
            if platform_for_url(url) != platform and platform != "website":
                continue
            if platform == "website" and platform_for_url(url) != "website":
                continue
            live, live_reason = candidate_is_live(url, pause_sec)
            if not live:
                continue
            fetched_text = fetch_candidate_text(url, pause_sec)
            score, evidence = score_candidate(row, url, title, snippet, fetched_text)
            candidate = Candidate(platform, canonical_url(url), title, snippet, score, f"{evidence}; {live_reason}")
            if best is None or candidate.score > best.score:
                best = candidate
        if best and best.score >= 90:
            break
    if best and best.score >= min_confidence:
        return best
    return None


def has_enrichment(row: pd.Series, platforms: list[str]) -> bool:
    for platform in platforms:
        url = str(row.get(f"{platform}_url", "")).strip()
        if not url:
            continue
        try:
            confidence = int(float(row.get(f"{platform}_confidence", 0) or 0))
        except ValueError:
            confidence = 0
        if confidence >= 65 and not is_social_noise(url):
            return True
    return False


def clear_enrichment_columns(df: pd.DataFrame, index: int, platforms: list[str]) -> None:
    for platform in platforms:
        for suffix in ["url", "confidence", "evidence"]:
            df.at[index, f"{platform}_{suffix}"] = ""


def trusted_existing_contact(row: pd.Series) -> str:
    contact = str(row.get("best_contact", "") or "").strip()
    if not contact or is_social_noise(contact):
        return ""
    if contact.startswith("http") and is_blocked_domain(contact):
        return ""
    return contact


def clean_public_social_links(value: str) -> str:
    links = [item.strip() for item in str(value or "").split(";") if item.strip()]
    return "; ".join(link for link in links if not is_social_noise(link) and not is_blocked_domain(link))


def enrich(
    input_csv: Path,
    output_csv: Path,
    limit: int | None,
    min_confidence: int,
    pause_sec: float,
    progress_file: Path,
    errors_file: Path,
    export_every: int,
    skip_existing: bool,
    selected_platforms: list[str],
) -> None:
    if skip_existing and output_csv.exists():
        df = pd.read_csv(output_csv).fillna("")
    else:
        df = pd.read_csv(input_csv).fillna("")
    if limit:
        df = df.head(limit).copy()

    all_platforms = ["instagram", "vk", "telegram", "facebook", "avito", "website"]
    platforms = [platform for platform in selected_platforms if platform in all_platforms] or all_platforms
    for platform in platforms:
        for suffix in ["url", "confidence", "evidence"]:
            col = f"{platform}_{suffix}"
            if col not in df.columns:
                df[col] = ""

    stats = EnrichStats(total=len(df), started_at=time.time())

    for index, row in df.iterrows():
        label = row.get("seller_shop_name") or row.get("brand") or row.get("sample_product_url")
        if "public_social_links" in df.columns:
            df.at[index, "public_social_links"] = clean_public_social_links(row.get("public_social_links", ""))
        if skip_existing and has_enrichment(row, platforms):
            stats.processed += 1
            stats.skipped += 1
            print_progress(stats, str(label))
            write_progress_file(progress_file, stats, str(label))
            continue

        clear_enrichment_columns(df, index, platforms)
        found_any = False
        for platform in platforms:
            safe_print(f"  checking {platform}: {label}")
            try:
                candidate = choose_best_candidate(row, platform, 5, pause_sec, min_confidence)
            except Exception as exc:
                stats.errors += 1
                append_error_csv(errors_file, str(label), platform, exc.__class__.__name__, str(exc))
                continue
            if not candidate:
                continue
            df.at[index, f"{platform}_url"] = candidate.url
            df.at[index, f"{platform}_confidence"] = str(candidate.score)
            df.at[index, f"{platform}_evidence"] = candidate.evidence
            found_any = True

        found_urls = [str(df.at[index, f"{platform}_url"]) for platform in platforms]
        found_urls = [url for url in found_urls if url]
        existing_contact = trusted_existing_contact(row)
        df.at[index, "best_contact"] = found_urls[0] if found_urls else existing_contact
        df.at[index, "contact_source"] = "web_enrichment" if found_urls else row.get("contact_source", "")
        if not found_urls and not existing_contact:
            df.at[index, "contact_source"] = ""
        stats.processed += 1
        if found_any:
            stats.enriched += 1
        print_progress(stats, str(label))
        write_progress_file(progress_file, stats, str(label))

        if export_every > 0 and stats.processed % export_every == 0:
            output_csv.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_csv, index=False, quoting=csv.QUOTE_MINIMAL)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False, quoting=csv.QUOTE_MINIMAL)


def safe_print(message: str) -> None:
    print(str(message).encode("ascii", errors="replace").decode("ascii"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find likely official seller socials/websites for outreach.")
    parser.add_argument("--input", type=Path, default=Path("output/sellers.csv"))
    parser.add_argument("--output", type=Path, default=Path("output/sellers_enriched.csv"))
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N sellers.")
    parser.add_argument("--min-confidence", type=int, default=65)
    parser.add_argument("--pause-sec", type=float, default=2.0)
    parser.add_argument("--progress-file", type=Path, default=Path("output/enrichment_progress.txt"))
    parser.add_argument("--errors-file", type=Path, default=Path("output/enrichment_errors.csv"))
    parser.add_argument("--export-every", type=int, default=5)
    parser.add_argument("--no-skip-existing", action="store_true")
    parser.add_argument(
        "--platforms",
        default="instagram,vk,telegram,facebook,avito,website",
        help="Comma-separated platforms: instagram,vk,telegram,facebook,avito,website",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    enrich(
        args.input,
        args.output,
        args.limit,
        args.min_confidence,
        args.pause_sec,
        args.progress_file,
        args.errors_file,
        args.export_every,
        not args.no_skip_existing,
        [item.strip() for item in args.platforms.split(",") if item.strip()],
    )
