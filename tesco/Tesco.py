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

class TescoScraper:
    def __init__(self):
        self.chrome_options = Options()
        self.chrome_options.add_argument('--start-maximized')  # Ensures the window is maximized
        self.chrome_options.add_argument('--enable-javascript')  # Explicitly enable JavaScript
        self.chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])  # Disable automation flag
        self.chrome_options.add_experimental_option('useAutomationExtension', False)
        self.chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        self.chrome_options.add_argument('--headless')
        self.chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36...")
    
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)

    def get_max_pages(self, driver: webdriver.Chrome, category: str) -> int:
        """Fetch the maximum number of pages for a category."""
        try:
            url = f"https://www.tesco.com/groceries/en-GB/shop/{category}/all?page=1"
            driver.get(url)
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            max_pages = [x.text for x in soup.find_all('span',class_="styled__Text-sc-1i711qa-1 bsLJsh ddsweb-link__text")][-1]
            return int(max_pages) if max_pages else 1
        except Exception as e:
            self.logger.error(f"Error getting max pages for {category}: {e}")
            return 1

    def extract_page_data(self, soup: BeautifulSoup) -> Tuple[List[str], List[str]]:
        """Extract product data from a page."""
        tesco_names =[]
        tesco_prices = []
        try:
            product_items = soup.find_all('div', class_ = "styled__StyledVerticalTile-sc-1r1v9f3-1 iAEUS")
            for item in product_items:
                # Extract product name
                name_elem = item.find('span', class_="styled__Text-sc-1i711qa-1 bsLJsh ddsweb-link__text")
                name = name_elem.text.strip() if name_elem else "N/A"
                tesco_names.append(name)
                
                # Extract price (handling out of stock)
                price_elem = item.find('p', class_="text__StyledText-sc-1jpzi8m-0 gyHOWz ddsweb-text styled__PriceText-sc-v0qv7n-1 cXlRF")
                price = price_elem.text.strip() if price_elem else "Out of Stock"
                tesco_prices.append(price)
            return tesco_names, tesco_prices
        except Exception as e:
            self.logger.error(f"Error extracting page data: {e}")
            return [], []

    def scrape_category(self, category: str) -> pd.DataFrame:
        """Scrape all products from a category."""
        all_data = []
        driver = webdriver.Chrome(options=self.chrome_options)
        
        try:
            # Handle cookies once at the start
            driver.get(f"https://www.tesco.com/groceries/en-GB/shop/{category}/all?page=1")
            # self.handle_cookies(driver)
            
            max_pages = self.get_max_pages(driver, category)
            self.logger.info(f"Category {category}: {max_pages} pages found")

            for page in range(1, 1 + max_pages):
                try:
                    url = f"https://www.tesco.com/groceries/en-GB/shop/{category}/all?page={page}"
                    driver.get(url)
                    # driver.save_screenshot(f"debug{page}.png")
                    time.sleep(2)
                    soup = BeautifulSoup(driver.page_source, 'html.parser')
                    names, prices = self.extract_page_data(soup)
                    
                    for name, price in zip(names, prices):
                        all_data.append({
                            'product_name': name,
                            'price': price,
                            #'weight': weight,
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
    categories = ['frozen-food',"food-cupboard","fresh-food","bakery" ]  # Add your categories here
    scraper = TescoScraper()
    
    # Run the scraper
    df = scraper.scrape_all_categories(categories)
    
    # Save results
    df.to_csv('tesco_test.csv', index=False)
    print(f"Scraped {len(df)} products across {len(categories)} categories")
    return df

if __name__ == "__main__":
    main()