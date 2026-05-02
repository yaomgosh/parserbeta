# Compliant Marketplace Lead Research Tool

Tool for collecting publicly visible marketplace seller leads.

## Guardrails
- Public pages only
- No bypass of login/captcha/rate limits/anti-bot
- No private personal data collection
- No automated messaging
- If blocked, page is logged into `blocked_pages`

## Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Modes

### 1) Search mode (auto-discovery)
Input columns: `marketplace,search_url`
```bash
python lead_research.py --input input/example_search_urls.csv --output output/sellers.csv --db output/leads.db --headless
```

### 2) Product URL mode (manual browsing workflow, recommended when category pages are blocked)
Input columns: `marketplace,product_url`
```bash
python lead_research.py --product-urls-file input/product_urls_template.csv --output output/sellers.csv --db output/leads.db --channel chrome
```

## Output
- `output/leads.db` (SQLite tables: `products`, `blocked_pages`)
- `output/sellers.csv` (dedup sellers + manual columns)

Manual columns:
- `contact_source`
- `contact`
- `outreach_status`
- `creative_opportunity`
- `lead_score`
