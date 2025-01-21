from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
import pandas as pd
import re
from concurrent.futures import ThreadPoolExecutor
import logging
from typing import List, Dict, Tuple
import time

class AldiScraper:
    def __init__(self):
        self.chrome_options = Options()
        self.chrome_options.add_argument('--start-maximized')  # Ensures the window is maximized
        self.chrome_options.add_argument('--enable-javascript')  # Explicitly enable JavaScript
        self.chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])  # Disable automation flag
        self.chrome_options.add_experimental_option('useAutomationExtension', False)
        self.chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        self.chrome_options.add_argument('--headless')
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)

    def get_max_pages(self, driver: webdriver.Chrome, category: str) -> int:
        """Fetch the maximum number of pages for a category."""
        try:
            url = f"https://groceries.aldi.co.uk/en-GB/{category}?&page=1"
            driver.get(url)
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            x = soup.find('span', class_='d-flex-inline pt-2')
            return int(re.findall(r'\d+', x.text)[0]) if x else 1
        except Exception as e:
            self.logger.error(f"Error getting max pages for {category}: {e}")
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
            time.sleep(2)
            cookie_button.click()
            # Wait for the cookie banner to disappear
            time.sleep(3)
        except Exception as e:
            self.logger.warning(f"Cookie handling failed: {e}")
            # Try an alternative method if the first one fails
            try:
                cookie_button = driver.find_element(By.ID, "onetrust-accept-btn-handler")
                driver.execute_script("arguments[0].click();", cookie_button)
            except Exception as e2:
                self.logger.warning(f"Alternative cookie handling also failed: {e2}")

    def extract_page_data(self, soup: BeautifulSoup) -> Tuple[List[str], List[str], List[str]]:
        """Extract product data from a page."""
        try:
            names = [x['title'] for x in soup.find_all('a', class_="p text-default-font")]
            prices = [x.text for x in soup.find_all('span',class_='h4')]
            weights = [x.text for x in soup.find_all('div', class_='text-gray-small')]
            # print(names)
            return names, prices, weights
        except Exception as e:
            self.logger.error(f"Error extracting page data: {e}")
            return [], [], []

    def scrape_category(self, category: str) -> pd.DataFrame:
        """Scrape all products from a category."""
        all_data = []
        driver = webdriver.Chrome(options=self.chrome_options)
        
        try:
            # Handle cookies once at the start
            driver.get(f"https://groceries.aldi.co.uk/en-GB/{category}?&page=1")
            self.handle_cookies(driver)
            
            max_pages = self.get_max_pages(driver, category)
            self.logger.info(f"Category {category}: {max_pages} pages found")

            for page in range(1, 1+max_pages):
                try:
                    url = f"https://groceries.aldi.co.uk/en-GB/{category}?&page={page}"
                    driver.get(url)
                    time.sleep(4)
                    soup = BeautifulSoup(driver.page_source, 'html.parser')
                    names, prices, weights = self.extract_page_data(soup)
                    
                    for name, price, weight in zip(names, prices, weights):
                        all_data.append({
                            'product_name': name,
                            'price': price,
                            'weight': weight,
                            'category': category
                        })
                    
                    self.logger.info(f"Scraped page {page}/{max_pages} of {category}")
                    
                except Exception as e:
                    self.logger.error(f"Error on page {page} of {category}: {e}")
                    continue
                    
        finally:
            driver.quit()   
        return pd.DataFrame(all_data)

    def scrape_all_categories(self, categories: List[str], max_workers: int = 5) -> pd.DataFrame:
        """Scrape all categories using ThreadPoolExecutor."""
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(self.scrape_category, categories))
        return pd.concat(results, ignore_index=True)

def main():
    
    categories = ["aldi_categories"]  # Add your categories here
    scraper = AldiScraper()
    
    # Run the scraper
    df = scraper.scrape_all_categories(categories)
    
    # Save results
    df.to_csv('aldi_products.csv', index=False)
    print(f"Scraped {len(df)} products across {len(categories)} categories")
    return df

if __name__ == "__main__":
    main()