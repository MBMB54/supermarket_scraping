from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
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

class AldiScraper:
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

# # If you want to keep the same format as your original setup
# formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
# for handler in logger.handlers:
#     handler.setFormatter(formatter)

    def get_max_pages(self, driver: webdriver.Chrome, category: str) -> int:
        """Fetch the maximum number of pages for a category."""
        try:
            url = f"https://groceries.aldi.co.uk/en-GB/{category}?&page=1"
            driver.get(url)
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            time.sleep(2)
            x = soup.find('span', class_='d-flex-inline pt-2')
            return int(re.findall(r'\d+', x.text)[0]) if x else 1
        except Exception as e:
            self.logger.error(f"Error getting max pages for {category}: {e}")
            return 1

    def handle_cookies(self, driver: webdriver.Chrome):
        """Handle cookie consent popup."""
        try:
            # Wait longer for the cookie popup and add a small initial delay
            cookie_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "onetrust-accept-btn-handler"))
            )
            # Add a small delay before clicking to ensure the element is truly interactive
            cookie_button.click()
            # Wait for the cookie banner to disappear
            time.sleep(1)
        except Exception as e:
                self.logger.warning(f"Cookie handling failed")

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

    def get_brands_categories(self,categories: List[str]) -> Tuple[List[str], List[str]]:
        driver = webdriver.Chrome(options=self.chrome_options, service = self.service)
        self.handle_cookies(driver)
        brands = []
        sub_categories = []
        for category in categories:
            url = f'https://groceries.aldi.co.uk/en-GB/{category}?&page=1'
            driver.get(url)
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            try:
                brands_checkpoint = soup.find('span', class_ ='align-left text-capitalize', string = ' Brand')
            
                for x in brands_checkpoint.find_all_next("h6", class_ = 'facet-checkbox'):
                    cleaned = re.sub(r"\(\d+\)" , "", x.text).rstrip()
                    brands.append(cleaned)
                for x in brands_checkpoint.find_all_previous("h6", class_ = 'facet-checkbox'):
                    cleaned = re.sub(r"\(\d+\)" , "", x.text).rstrip().lstrip()
                    sub_categories.append(cleaned)
            except AttributeError as e:
                self.logger.error(f"Brand extraction failed on {url}")
                print(epy)

        brands = list(set(brands))
        brands.remove('Vegan')
        brands.remove('Vegetarian')
        brands.remove('Giannis')
        brands.append("Gianni's")
        sub_categories = list(set(sub_categories))
        return brands,sub_categories

    def scrape_category(self, category: str) -> pd.DataFrame:
        """Scrape all products from a category."""
        all_data = []
        driver = webdriver.Chrome(options=self.chrome_options, service = self.service)
        
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

    def scrape_all_categories(self, categories: List[str], max_workers: int = 2) -> pd.DataFrame:
        """Scrape all categories using ThreadPoolExecutor."""
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(self.scrape_category, categories))
        return pd.concat(results, ignore_index=True)

    def save_df_to_s3(self,df, bucket_name, file_prefix, folder=None, file_format='csv'):
        """
        Save a pandas DataFrame to an S3 bucket with today's date in the filename.
        
        Parameters:
        -----------
        df : pandas.DataFrame
            The DataFrame to save
        bucket_name : str
            Name of the S3 bucket
        file_prefix : str
            Prefix for the filename (e.g., 'sales_data', 'user_metrics')
        folder : str, optional
            Folder path within the bucket (e.g., 'aldi', 'aldi/raw_data')
        file_format : str, optional
            Format to save the file in ('csv' or 'parquet'), defaults to 'csv'
        
        Returns:
        --------
        str
            The S3 path where the file was saved
        """
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


    #def save_to_s3_bucket 

def lambda_handler(event,context):
    
    categories = ['frozen','food-cupboard','fresh-food','bakery','chilled-food']   # Add your categories here
    scraper = AldiScraper()
    
    # Run the scraper
    df = scraper.scrape_all_categories(['frozen','chilled-food'])
    # df = scraper.scrape_category('frozen')
    scraper.save_df_to_s3(df=df,bucket_name='uksupermarketdata',file_prefix='aldi',folder='aldi')
    # brands, sub_categories = scraper.get_brands_categories(categories)
    # Save results
    # print(brands)
    # brands.to_csv('aldi_brands.csv')
    # df.to_csv('aldi_products.csv', index=False)
    # print(f"Scraped {len(df)} products across {len(categories)} categories")
    print('success')
    

# if __name__ == "__main__":
#     main()