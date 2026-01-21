import logging
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


class TescoScraper:
    def __init__(self, max_workers: int = 3, headless: bool = False):
        """
        Initialize Tesco scraper with optimized settings.

        Args:
            max_workers: Number of parallel browser instances (default: 3)
            headless: Run browsers in headless mode (default: False for better bot detection bypass)
        """
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
        self.logger = logging.getLogger(__name__)
        self.max_workers = max_workers
        self.headless = headless

    def _get_chrome_options(self, worker_id: int = 0) -> uc.ChromeOptions:
        """
        Create optimized ChromeOptions for each driver instance.

        2025 Best Practices:
        - Separate user data dirs per worker to avoid race conditions
        - Modern user agent strings
        - Performance optimizations
        """
        chrome_options = uc.ChromeOptions()

        # Use separate profile per worker to avoid conflicts
        profile_dir = f"/tmp/chrome_profile_{worker_id}"
        chrome_options.add_argument(f"--user-data-dir={profile_dir}")

        # Performance optimizations
        chrome_options.add_argument("--start-maximized")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")  # Better stability
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")

        # Modern user agent (Chrome 131, 2025)
        chrome_options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

        return chrome_options

    def handle_cookies(self, driver: webdriver.Chrome):
        """
        Handle cookie consent popup using explicit waits (2025 best practice).

        Avoids time.sleep() in favor of WebDriverWait for better performance.
        """
        try:
            # Wait for cookie button to be clickable (not just present)
            accept_button = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable(
                    (
                        By.class_name,
                        "ddsweb-consent-banner__button tsmNeW_button ddsweb-button ddsweb-button--text-button _4jOaWW_textButton _4jOaWW_base _4jOaWW_hasOutline _4jOaWW_secondary _4jOaWW_md",
                    )
                )
            )

            # Click with JavaScript for better reliability
            driver.execute_script("arguments[0].click();", accept_button)

            # Wait for popup to disappear (explicit wait instead of sleep)
            WebDriverWait(driver, 5).until(
                EC.invisibility_of_element_located((By.ID, "onetrust-accept-btn-handler"))
            )
            self.logger.info("✓ Cookie consent handled")

        except TimeoutException:
            self.logger.warning("Cookie popup not found - may already be accepted or absent")
        except Exception as e:
            self.logger.warning(f"Cookie handling error: {e}. Continuing anyway...")

    def get_max_pages(self, driver: webdriver.Chrome, category: str) -> int:
        """
        Fetch maximum number of pages using explicit waits and lxml parser.

        2025 Best Practice: lxml parser is ~2x faster than html.parser
        """
        try:
            url = f"https://www.tesco.com/groceries/en-GB/shop/{category}/all?page=1"
            driver.get(url)

            # Wait for page to fully load - try multiple selectors
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(
                        (
                            By.CSS_SELECTOR,
                            'ul.pagination, nav[aria-label*="pagination"], a[data-auto="pagination-button"]',
                        )
                    )
                )
            except TimeoutException:
                # If pagination doesn't load, page content still might be there
                self.logger.debug(f"Pagination element not found for {category}")

            # Use lxml parser for better performance
            soup = BeautifulSoup(driver.page_source, "lxml")

            # Try multiple pagination selector strategies
            pagination_links = (
                soup.find_all("a", {"data-auto": "pagination-button"})
                or soup.select("ul.pagination li a")
                or soup.select('nav[aria-label*="pagination"] a')
            )

            if pagination_links:
                # Extract page numbers and get max
                page_numbers = []
                for link in pagination_links:
                    text = link.get_text(strip=True)
                    # Also try data attributes
                    if not text:
                        text = link.get("data-page", "")
                    if text.isdigit():
                        page_numbers.append(int(text))

                if page_numbers:
                    max_page = max(page_numbers)
                    self.logger.info(f"Found {max_page} pages for {category}")
                    return max_page

            self.logger.info(f"No pagination found for {category} - assuming single page")
            return 1

        except Exception as e:
            self.logger.error(f"Error getting max pages for {category}: {e}")
            return 1

    def extract_page_data(self, html: str) -> tuple[list[str], list[str], list[str]]:
        """
        Extract product data using lxml parser (2x faster than html.parser).

        Uses more robust selectors with fallbacks.
        """
        tesco_names = []
        tesco_prices = []
        tesco_prices_weights = []

        try:
            # Use lxml parser for 2x performance improvement
            soup = BeautifulSoup(html, "lxml")

            # Find product containers with more flexible selector
            product_items = soup.find_all("li", class_=re.compile(r"product-tile|WL_DZ"))

            if not product_items:
                # Fallback: try alternative product container classes
                product_items = soup.select('div[data-auto="product-tile"]')

            for item in product_items:
                # Extract product name - updated selectors for 2025
                name_elem = (
                    item.find("h2", class_=re.compile(r"_64Yvfa_title"))  # Primary product title
                    or item.find("a", class_=re.compile(r"_64Yvfa_titleLink"))  # Title link
                    or item.find("h3", class_=re.compile(r"title"))
                    or item.select_one('[data-auto="product-tile-title"]')
                )
                name = name_elem.get_text(strip=True) if name_elem else "N/A"
                tesco_names.append(name)

                # Extract price with fallback selectors
                price_elem = (
                    item.find("p", class_=re.compile(r"_64Yvfa_price|PriceText|ddsweb-price"))
                    or item.select_one('[data-auto="price-value"]')
                    or item.find("span", class_=re.compile(r"price.*value"))
                )
                price = price_elem.get_text(strip=True) if price_elem else "Out of Stock"
                tesco_prices.append(price)

                # Extract price per weight
                price_weight_elem = (
                    item.find(
                        "p",
                        class_=re.compile(
                            r"_64Yvfa_pricePerUnit|Subtext|price__subtext|price.*unit"
                        ),
                    )
                    or item.select_one('[data-auto="price-per-quantity"]')
                    or item.find("span", class_=re.compile(r"price.*unit"))
                )
                price_weight = (
                    price_weight_elem.get_text(strip=True) if price_weight_elem else "N/A"
                )
                tesco_prices_weights.append(price_weight)

            self.logger.debug(f"Extracted {len(tesco_names)} products")
            return tesco_names, tesco_prices, tesco_prices_weights

        except Exception as e:
            self.logger.error(f"Error extracting page data: {e}")
            return [], [], []

    def scrape_category(self, args: tuple[str, int]) -> pd.DataFrame:
        """
        Scrape all products from a category with optimizations.

        Args:
            args: Tuple of (category, worker_id)

        2025 Best Practices:
        - Uses explicit waits instead of time.sleep()
        - Separate Chrome profiles per worker
        - lxml parser for speed
        """
        category, worker_id = args
        all_data = []

        # Create driver with worker-specific profile
        driver = uc.Chrome(
            options=self._get_chrome_options(worker_id),
            headless=self.headless,
            use_subprocess=False,  # Better for headless mode
        )

        try:
            # Navigate and handle cookies
            driver.get(f"https://www.tesco.com/groceries/en-GB/shop/{category}/all?page=1")
            self.handle_cookies(driver)

            max_pages = self.get_max_pages(driver, category)
            self.logger.info(f"[Worker {worker_id}] Category '{category}': {max_pages} pages")

            for page in range(1, max_pages + 1):
                try:
                    url = f"https://www.tesco.com/groceries/en-GB/shop/{category}/all?page={page}"
                    driver.get(url)

                    # Wait for products to load - try multiple selectors
                    try:
                        WebDriverWait(driver, 15).until(
                            EC.presence_of_element_located(
                                (
                                    By.CSS_SELECTOR,
                                    'li.product-list--list-item, li[class*="product"], ul.product-list li, div[data-auto="product-tile"]',
                                )
                            )
                        )
                    except TimeoutException:
                        # Page might still have content, log and try to extract anyway
                        self.logger.warning(
                            f"[Worker {worker_id}] Product elements not found via wait, attempting extraction anyway..."
                        )

                    # Save debug HTML on first page to understand structure
                    if page == 1:
                        debug_file = f"/tmp/tesco_debug_{category}_page{page}.html"
                        with open(debug_file, "w") as f:
                            f.write(driver.page_source)
                        self.logger.debug(f"Saved debug HTML to {debug_file}")

                    # Extract data using optimized lxml parser
                    names, prices, price_weights = self.extract_page_data(driver.page_source)

                    if not names:
                        self.logger.warning(
                            f"[Worker {worker_id}] No products extracted from page {page}"
                        )
                        # Try to log what we can see
                        soup = BeautifulSoup(driver.page_source, "lxml")
                        li_tags = soup.find_all("li")
                        self.logger.debug(f"Found {len(li_tags)} <li> tags total on page")
                        continue

                    for name, price, price_weight in zip(
                        names, prices, price_weights, strict=False
                    ):
                        all_data.append(
                            {
                                "product_name": name,
                                "price": price,
                                "price/weight": price_weight,
                                "category": category,
                            }
                        )

                    self.logger.info(
                        f"[Worker {worker_id}] ✓ Page {page}/{max_pages} - {len(names)} products"
                    )

                except Exception as e:
                    self.logger.error(f"[Worker {worker_id}] ✗ Page {page} error: {e}")
                    continue

        finally:
            driver.quit()
            # Clean up profile directory
            profile_dir = f"/tmp/chrome_profile_{worker_id}"
            if os.path.exists(profile_dir):
                try:
                    shutil.rmtree(profile_dir)
                except Exception:
                    pass

        return pd.DataFrame(all_data)

    def scrape_all_categories(self, categories: list[str]) -> pd.DataFrame:
        """
        Scrape all categories in parallel using ThreadPoolExecutor.

        Uses worker IDs to avoid Chrome profile conflicts.
        """
        start_time = time.time()

        # Initialize ChromeDriver once to avoid race conditions
        self.logger.info("Initializing ChromeDriver...")
        test_driver = uc.Chrome(options=self._get_chrome_options(999), headless=self.headless)
        test_driver.quit()

        # Clean up test profile
        test_profile = "/tmp/chrome_profile_999"
        if os.path.exists(test_profile):
            try:
                shutil.rmtree(test_profile)
            except Exception:
                pass

        # Create tasks with worker IDs
        tasks = [(category, idx) for idx, category in enumerate(categories)]

        # Run in parallel
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            results = list(executor.map(self.scrape_category, tasks))

        elapsed = time.time() - start_time
        total_products = sum(len(df) for df in results)

        self.logger.info(
            f"✓ Scraped {total_products} products in {elapsed:.1f}s ({total_products / elapsed:.1f} products/sec)"
        )

        return pd.concat(results, ignore_index=True) if results else pd.DataFrame()


def main():
    """Main entry point with CLI argument support."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Optimized Tesco grocery scraper (2025)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scrape bakery (3 parallel workers)
  python Tesco.py --categories bakery

  # Scrape multiple categories in parallel
  python Tesco.py --categories bakery dairy frozen --workers 5

  # Run in headless mode (less stable but faster)
  python Tesco.py --categories bakery --headless
        """,
    )

    parser.add_argument(
        "--categories", nargs="+", default=["bakery"], help="Categories to scrape (default: bakery)"
    )
    parser.add_argument(
        "--workers", type=int, default=3, help="Number of parallel workers (default: 3)"
    )
    parser.add_argument(
        "--headless", action="store_true", help="Run in headless mode (less stable)"
    )
    parser.add_argument(
        "--output",
        default="tesco_products.csv",
        help="Output CSV file (default: tesco_products.csv)",
    )

    args = parser.parse_args()

    # Create optimized scraper
    scraper = TescoScraper(max_workers=args.workers, headless=args.headless)

    print(f"\n{'=' * 60}")
    print("🛒 Tesco Scraper 2025 - Optimized Edition")
    print(f"{'=' * 60}")
    print(f"Categories: {', '.join(args.categories)}")
    print(f"Workers: {args.workers}")
    print(f"Headless: {args.headless}")
    print(f"{'=' * 60}\n")

    # Scrape
    df = scraper.scrape_all_categories(args.categories)

    # Save results
    if not df.empty:
        df.to_csv(args.output, index=False)
        print(f"\n{'=' * 60}")
        print("✅ Success!")
        print(f"{'=' * 60}")
        print(f"Products scraped: {len(df)}")
        print(f"Categories: {len(args.categories)}")
        print(f"Output file: {args.output}")
        print(f"{'=' * 60}\n")
    else:
        print("\n⚠️  No products scraped. Check logs for errors.\n")

    return df


if __name__ == "__main__":
    main()
