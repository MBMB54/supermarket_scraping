from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
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
import boto3
from tempfile import mkdtemp

class OcadoScraper:
    def __init__(self):
        self.chrome_options = Options()
        
        # Configure Chrome for Lambda layer
        self.chrome_options = Options()
        self.chrome_options.add_argument('--start-maximized')  # Ensures the window is maximized
        self.chrome_options.add_argument('--enable-javascript')  # Explicitly enable JavaScript
        self.chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])  # Disable automation flag
        self.chrome_options.add_experimental_option('useAutomationExtension', False)
        self.chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        self.chrome_options.add_argument('--headless')
        self.chrome_options.add_argument("--no-sandbox")
        self.chrome_options.add_argument("--disable-dev-shm-usage")
        self.chrome_options.add_argument("--disable-gpu")
        self.chrome_options.add_argument("--disable-dev-tools")
        self.chrome_options.add_argument("--no-zygote")
        self.chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36...")
        self.chrome_options.add_argument(f"--user-data-dir={mkdtemp()}")
        self.chrome_options.add_argument(f"--data-path={mkdtemp()}")
        self.chrome_options.add_argument(f"--disk-cache-dir={mkdtemp()}")
        self.chrome_options.add_argument("--remote-debugging-pipe")
        self.chrome_options.add_argument("--verbose")
        self.chrome_options.add_argument("--log-path=/tmp")
        self.chrome_options.binary_location = "/opt/chrome/chrome-linux64/chrome"

        self.service = Service(
        executable_path="/opt/chrome-driver/chromedriver-linux64/chromedriver",
        service_log_path="/tmp/chromedriver.log")
        
        self.logger = logging.getLogger()
        self.logger.setLevel(logging.INFO)

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
        driver = webdriver.Chrome(options=self.chrome_options, service = self.service)
        
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

    def save_df_to_s3(self,df, bucket_name, file_prefix, folder=None, file_format='csv'):
        # Create S3 client
        s3_client = boto3.client('s3')
        
        # Generate filename with today's date
        today_str = datetime.now().strftime('%Y%m%d')
        filename = f"{file_prefix}_{today_str}"
        
        # Create a buffer to store the file
        if file_format.lower() == 'csv':
            buffer = df.to_csv(index=False)
            filename += '.csv'
            content_type = 'text/csv'
        elif file_format.lower() == 'parquet':
            buffer = df.to_parquet()
            filename += '.parquet'
            content_type = 'application/octet-stream'
        else:
            raise ValueError("Supported formats are 'csv' and 'parquet'")
        
        # Construct the full S3 key (path)
        if folder:
            # Remove leading/trailing slashes and combine with filename
            folder = folder.strip('/')
            s3_key = f"{folder}/{filename}"
        else:
            s3_key = filename
        
        # Upload to S3
        try:
            s3_client.put_object(
                Bucket=bucket_name,
                Key=s3_key,
                Body=buffer,
                ContentType=content_type
            )
            s3_path = f"s3://{bucket_name}/{s3_key}"
            self.logger.info(f"Aldi data uploaded to {bucket_name}")
        except Exception as e:
            raise Exception(f"Error saving file to S3: {str(e)}")

def main():
    
    categories = ['frozen-303714','best-of-fresh-294566','food-cupboard-drinks-bakery-294572']   # Add your categories here
    scraper = OcadoScraper()
    
    # Run the scraper
    df = scraper.scrape_all_categories(categories)
    scraper.save_df_to_s3(df=df,bucket_name='uksupermarketdata',file_prefix='ocado',folder='ocado')
    print(f"Scraped {len(df)} products across {len(categories)} categories")
   
if __name__ == "__main__":
    main()
