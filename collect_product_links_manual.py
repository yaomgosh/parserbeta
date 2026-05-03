import csv
import time
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options


OUTPUT = Path("input/product_urls_collected.csv")
MARKETPLACE = "wildberries"
RUN_SECONDS = 180  # 3 минуты на ручной скролл
POLL_EVERY_SEC = 1.5


def is_product_url(url: str) -> bool:
    if not url:
        return False
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        return False
    path = p.path.lower()
    return ("detail.aspx" in path) or ("/catalog/" in path and "/detail" in path)


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument("--start-maximized")
    driver = webdriver.Chrome(options=options)

    print("Откроется Chrome. Перейди на категорию и просто листай страницу вручную.")
    print(f"Сбор ссылок будет идти {RUN_SECONDS} сек. Потом сохранит CSV и завершится.")

    driver.get("https://www.wildberries.ru/")

    seen = set()
    started = time.time()

    try:
        while time.time() - started < RUN_SECONDS:
            anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
            for a in anchors:
                href = a.get_attribute("href")
                if is_product_url(href):
                    seen.add(href.split("#", 1)[0])
            print(f"\rСобрано ссылок: {len(seen)}", end="")
            time.sleep(POLL_EVERY_SEC)
    finally:
        print()
        driver.quit()

    with OUTPUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["marketplace", "product_url"])
        for u in sorted(seen):
            w.writerow([MARKETPLACE, u])

    print(f"Готово: {OUTPUT} | ссылок: {len(seen)}")


if __name__ == "__main__":
    main()