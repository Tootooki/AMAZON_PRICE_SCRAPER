"""
Amazon ASIN Price Scraper - Scraping Module
Uses Playwright headless browser with stealth to extract Amazon buybox prices.
"""

import re
import random
import time

# Stealth script to bypass bot detection
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
    ],
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);
delete navigator.__proto__.webdriver;
"""

AMAZON_DOMAIN = "https://www.amazon.com"
PAGE_TIMEOUT = 25000
MIN_DELAY = 2
MAX_DELAY = 4


def extract_price(page):
    """Try multiple strategies to find the buybox price."""

    # Strategy 1: .a-price-whole + .a-price-fraction
    price_containers = [
        "#corePriceDisplay_desktop_feature_div .a-price",
        "#corePrice_feature_div .a-price",
        "#apex_offerDisplay_desktop .a-price",
        "#buyBoxInner .a-price",
        "#rightCol .a-price",
        "#desktop_buybox .a-price",
        ".a-price",
    ]
    for container_sel in price_containers:
        try:
            container = page.query_selector(container_sel)
            if container:
                whole_el = container.query_selector(".a-price-whole")
                fraction_el = container.query_selector(".a-price-fraction")
                if whole_el:
                    whole = whole_el.text_content().strip().rstrip(".")
                    fraction = fraction_el.text_content().strip() if fraction_el else "00"
                    if whole and whole.replace(",", "").replace(".", "").isdigit():
                        return f"${whole}.{fraction}"
        except Exception:
            continue

    # Strategy 2: .a-offscreen elements (text_content, NOT inner_text)
    offscreen_selectors = [
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#apex_offerDisplay_desktop .a-price .a-offscreen",
        "#buyBoxInner .a-price .a-offscreen",
        "#rightCol .a-price .a-offscreen",
        "#desktop_buybox .a-price .a-offscreen",
        "#tp_price_block_total_price_ww .a-offscreen",
        "span[data-a-color='price'] .a-offscreen",
    ]
    for selector in offscreen_selectors:
        try:
            el = page.query_selector(selector)
            if el:
                text = el.text_content().strip()
                if text and "$" in text:
                    return text
        except Exception:
            continue

    # Strategy 3: Legacy price blocks
    for selector in ["#priceblock_ourprice", "#priceblock_dealprice",
                     "#priceblock_saleprice", "#price_inside_buybox",
                     "#kindle-price", "#digital-list-price", "#newBuyBoxPrice"]:
        try:
            el = page.query_selector(selector)
            if el:
                text = el.text_content().strip()
                if text and ("$" in text or re.match(r'[\d,.]+', text)):
                    return text
        except Exception:
            continue

    # Strategy 4: Broader fallback
    try:
        all_prices = page.query_selector_all(
            "#centerCol .a-price .a-offscreen, "
            "#rightCol .a-price .a-offscreen, "
            "#buybox .a-price .a-offscreen"
        )
        for el in all_prices:
            text = el.text_content().strip()
            if text and "$" in text:
                return text
    except Exception:
        pass

    # Strategy 5: Regex on buybox HTML
    try:
        buybox_el = page.query_selector("#buyBoxAccordion, #addToCart_feature_div, #rightCol")
        if buybox_el:
            html = buybox_el.inner_html()
            prices = re.findall(r'\$[\d,]+\.\d{2}', html)
            if prices:
                return prices[0]
    except Exception:
        pass

    return None


def extract_title(page):
    """Extract the product title."""
    try:
        el = page.query_selector("#productTitle")
        if el:
            title = el.text_content().strip()
            return title[:120] if len(title) > 120 else title
    except Exception:
        pass
    return "Title not found"


def scrape_asin(page, asin):
    """Scrape a single ASIN. Returns (title, price, error)."""
    url = f"{AMAZON_DOMAIN}/dp/{asin}"
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

        if response and response.status == 404:
            return None, None, "Product not found (404)"
        if response and response.status == 503:
            return None, None, "Blocked by Amazon (503)"

        try:
            page.wait_for_selector(
                ".a-price, #priceblock_ourprice, #priceblock_dealprice, #corePriceDisplay_desktop_feature_div",
                timeout=10000,
            )
        except Exception:
            pass

        page.wait_for_timeout(2000)

        captcha = page.query_selector(
            "form[action*='validateCaptcha'], "
            "input[id='captchacharacters'], "
            "#captchacharacters"
        )
        if captcha:
            return None, None, "CAPTCHA detected"

        title = extract_title(page)
        price = extract_price(page)

        if price is None:
            return title, None, "Price not found (out of stock or unavailable)"

        return title, price, None

    except Exception as e:
        return None, None, f"Error: {str(e)[:100]}"


def run_scrape_job(asins, job_state):
    """
    Background scraping job. Updates job_state dict with progress.
    job_state keys: status, progress, total, results, error
    """
    from playwright.sync_api import sync_playwright

    job_state["status"] = "starting"
    job_state["total"] = len(asins)
    job_state["progress"] = 0
    job_state["results"] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-zygote",
                    "--disable-gpu",
                    "--single-process",
                ],
            )

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                java_script_enabled=True,
                has_touch=False,
                is_mobile=False,
                color_scheme="light",
            )

            context.add_init_script(STEALTH_SCRIPT)
            context.add_cookies([
                {
                    "name": "session-id",
                    "value": f"{random.randint(100,999)}-{random.randint(1000000,9999999)}-{random.randint(1000000,9999999)}",
                    "domain": ".amazon.com",
                    "path": "/",
                },
                {
                    "name": "i18n-prefs",
                    "value": "USD",
                    "domain": ".amazon.com",
                    "path": "/",
                },
            ])

            page = context.new_page()

            # Visit homepage first to establish session
            job_state["status"] = "initializing"
            try:
                page.goto("https://www.amazon.com", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)
            except Exception:
                pass

            job_state["status"] = "scraping"

            for i, asin in enumerate(asins):
                asin = asin.strip().upper()

                title, price, error = scrape_asin(page, asin)

                result = {
                    "asin": asin,
                    "title": title or "N/A",
                    "price": price or "N/A",
                    "error": error,
                    "status": "error" if error else "ok",
                }
                job_state["results"].append(result)
                job_state["progress"] = i + 1

                # Delay between requests
                if i < len(asins) - 1:
                    delay = random.uniform(MIN_DELAY, MAX_DELAY)
                    time.sleep(delay)

            browser.close()

        job_state["status"] = "complete"

    except Exception as e:
        job_state["status"] = "error"
        job_state["error"] = str(e)
