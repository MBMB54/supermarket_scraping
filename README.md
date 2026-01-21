# Supermarket Scraping

A web scraping project that collects price data from UK supermarkets including Tesco, Aldi, and M&S (via Ocado).

## Features

- **Multi-supermarket support**: Scrapes product data from:
  - Tesco
  - Aldi
  - M&S/Ocado (Marks & Spencer products on Ocado)
- **Selenium-based scraping**: Uses Selenium WebDriver with headless Chrome for dynamic content
- **Data extraction**: Collects product names, prices, and price-per-weight information
- **AWS integration**: Supports deployment to AWS Lambda (Aldi scraper configured for Lambda)
- **Concurrent processing**: Uses ThreadPoolExecutor for efficient multi-page scraping

## Requirements

- Python 3.9+
- Chrome/Chromium browser
- ChromeDriver

## Installation

This project uses [uv](https://github.com/astral-sh/uv) for dependency management.

1. Install dependencies:
```bash
uv sync
```

2. The project will automatically install all required packages:
   - beautifulsoup4
   - pandas
   - pyarrow
   - requests
   - selenium
   - boto3
   - undetected-chromedriver (for Tesco)

## Usage

### Running scrapers locally

```bash
# Run Tesco scraper
uv run Tesco.py

# Run Aldi scraper
uv run Aldi.py

# Run M&S/Ocado scraper
uv run marks.py
```

### Project Structure

- `Tesco.py` - Tesco supermarket scraper
- `Aldi.py` - Aldi scraper (configured for AWS Lambda deployment)
- `marks.py` - M&S/Ocado scraper
- `aldi_processing.py` - Data processing utilities for Aldi data
- `pyproject.toml` - Project dependencies and configuration
- `uv.lock` - Locked dependency versions for reproducibility

## AWS Lambda Deployment

The Aldi scraper is configured to run on AWS Lambda with:
- Chrome binary at `/opt/chrome/chrome-linux64/chrome`
- ChromeDriver at `/opt/chrome-driver/chromedriver-linux64/chromedriver`
- S3 integration via boto3

## Development

### Adding new dependencies

```bash
uv add <package-name>
```

### Removing dependencies

```bash
uv remove <package-name>
```

## Notes

- All scrapers use headless Chrome for operation
- Anti-bot detection measures are implemented (user agents, automation flags)
- Logging is configured for debugging and monitoring
- The project uses undetected-chromedriver for Tesco to bypass bot detection
