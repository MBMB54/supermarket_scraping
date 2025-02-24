from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import NoSuchElementException
from bs4 import BeautifulSoup
import pandas as pd
import re
from concurrent.futures import ThreadPoolExecutor
import logging
from typing import List, Dict, Tuple
import time
from io import StringIO
from datetime import datetime

class OcadoScraper:
    def __init__(self):
        self.chrome_options = Options()
        
        # Configure Chrome for Lambda layer
        # self.chrome_options.binary_location = '/opt/python/headless-chromium/headless-chromium'
        self.chrome_options = Options()
        self.chrome_options.add_argument('--start-maximized')  # Ensures the window is maximized
        self.chrome_options.add_argument('--enable-javascript')  # Explicitly enable JavaScript
        self.chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])  # Disable automation flag
        self.chrome_options.add_experimental_option('useAutomationExtension', False)
        self.chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        self.chrome_options.add_argument('--headless')
        self.chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36...")
        
        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
    def get_total_products(self, driver: webdriver.Chrome, category: str) -> int:
        """Fetch the maximum number of pages for a category."""
        try:
            url = f"https://www.ocado.com/browse/m-s-at-ocado-294578/{category}?display=2400"
            driver.get(url)
            total_products = driver.find_element(By.CSS_SELECTOR, "div.total-product-number").text
            return int(re.findall(r'\d+',total_products)[0]) if total_products else 1
        except Exception as e:
            self.logger.error(f"Error getting max products for {category}: {e}")
            return 1

    def handle_cookies(self, driver: webdriver.Chrome):
        """Handle cookie consent popup."""
        try:
            # Wait longer for the cookie popup and add a small initial delay
            time.sleep(2)
            cookie_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "onetrust-accept-btn-handler"))
            )
            # Add a small delay before clicking to ensure the element is truly interactive
            cookie_button.click()
            # Wait for the cookie banner to disappear
            time.sleep(2)
        except Exception as e:
            self.logger.warning(f"Cookie handling failed: {e}")

    def extract_page_data(self, driver: webdriver.Chrome) -> Tuple[List[str], List[str], List[str]]:
        """Extract product data from a page."""
        containers = driver.find_elements(By.CSS_SELECTOR, "li[class*='fops-item']")
        n_containers = len(containers)
        self.logger.info(f"Total products locted: {n_containers}")

        # Scroll in batches
        batch_size = 100
        for i in range(0, n_containers, batch_size):
            # Calculate the index of the last element in the current batch
            target_idx = min(i + batch_size - 1, n_containers - 1)
            # Scroll to the target element (disable smooth scrolling for speed)
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'auto', block: 'center'});", 
                containers[target_idx]
            )
            # Tiny delay to allow rendering
            time.sleep(0.9)

        # Re-fetch containers to avoid staleness
        containers = driver.find_elements(By.CSS_SELECTOR, "li[class*='fops-item']")
         # Extract names
        product_names = []
        product_prices = []
        product_weights = []
        for container in containers:
            try:
                name = container.find_element(By.CSS_SELECTOR, "h4.fop-title").get_attribute("title").strip()
                product_names.append(name)
            except NoSuchElementException as e:
                self.logger.error(f"Error extracting page {i} name data: {e}")
                container.screenshot(f'error{i}.png')
                product_names.append("N/A")
            try:
                price = container.find_element(By.CSS_SELECTOR, "span.fop-price").text
                product_prices.append(price)
            except NoSuchElementException as e:
                self.logger.error(f"Error extracting page {i} price data: {e}")
                product_prices.append("N/A")
            try:
                weight = container.find_element(By.CSS_SELECTOR, "span.fop-catch-weight").text
                product_weights.append(weight)
            except NoSuchElementException as e:
                self.logger.error(f"Error extracting page {i} weight data: {e}")
                product_weights.append("N/A")
            
        return product_names,product_prices,product_weights

    def scrape_category(self, category: str) -> pd.DataFrame:
        """Scrape all products from a category."""
        all_data = []
        driver = webdriver.Chrome(options=self.chrome_options)
        
        try:
            # Handle cookies once at the start
            driver.get(f"https://www.ocado.com/browse/m-s-at-ocado-294578/{category}?display=2400")
            self.handle_cookies(driver)
            total_products = self.get_total_products(driver, category)
            self.logger.info(f"Category {category}: {total_products} products found")
            names, prices, weights = self.extract_page_data(driver)
            for name, price, weight in zip(names, prices, weights):
                        all_data.append({
                            'product_name': name,
                            'price': price,
                            'weight': weight,
                            'category': category
                        })
                    
            self.logger.info(f"Scraped {len(names)}/{total_products} of products for {category}")
                    
        except Exception as e:
            self.logger.error(f"Error on {category} page: {e}")
                    
        finally:
            driver.quit()   
        return pd.DataFrame(all_data)

    def scrape_all_categories(self, categories: List[str], max_workers: int = 5) -> pd.DataFrame:
        """Scrape all categories using ThreadPoolExecutor."""
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(self.scrape_category, categories))
        return pd.concat(results, ignore_index=True)

def main():
    
    categories = ['frozen-303714','best-of-fresh-294566','food-cupboard-drinks-bakery-294572']   # Add your categories here
    scraper = OcadoScraper()
    
    # Run the scraper
    df = scraper.scrape_all_categories(categories)
    
    # Save results
    df.to_csv('ocado_products.csv', index=False)
    print(f"Scraped {len(df)} products across {len(categories)} categories")
    return df

if __name__ == "__main__":
    main()