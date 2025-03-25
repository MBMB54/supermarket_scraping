# UK Supermarket Price Scraping Platform

A scalable web scraping solution monitoring pricing data across major UK supermarkets (Tesco, Sainsbury's, Asda). Deployed on AWS with Docker containerization for reliable production-grade data collection.

[![AWS Lambda](https://img.shields.io/badge/AWS-Lambda-orange?logo=amazon-aws)](https://aws.amazon.com/lambda/)
[![Docker](https://img.shields.io/badge/Docker-Containerized-blue?logo=docker)](https://www.docker.com/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-brightgreen?logo=python)](https://www.python.org/)

![Architecture Diagram](docs/architecture.png) <!-- Add your diagram image -->

## Key Features
- **Multithreaded Scraping**: Concurrent data collection from multiple retailers
- **Dockerized Environment**: Consistent execution across development/production
- **AWS Lambda Deployment**: Serverless scaling with S3 data storage
- **CI/CD Pipeline**: Automated testing and deployment via GitHub Actions
- **Error Resiliency**: Exponential backoff retry logic with failure tracking
- **Data Export**: CSV/JSON outputs with AWS Athena integration

## Tech Stack Highlights

### Web Scraping Core
- **Selenium WebDriver**: Browser automation for JavaScript-heavy sites
- **BeautifulSoup4**: HTML parsing for static content
- **Scrapy**: Advanced crawling patterns (Middleware, Pipelines)

### AWS Services
- **Lambda**: Serverless execution (Python 3.10 runtime)
- **S3**: Raw data storage & processed datasets
- **CloudWatch**: Monitoring & error tracking
- **EventBridge**: Scheduled execution (cron jobs)
- **API Gateway**: REST endpoints for manual triggers

### DevOps Tooling
- **Docker**: Containerized scraping environment (Alpine Linux base)
- **GitHub Actions**: CI/CD with AWS deployment workflows
- **Terraform**: Infrastructure-as-Code (AWS provisioning)
- **Serverless Framework**: Lambda function management

### Core Technologies
- **Python 3.10**: Async I/O with asyncio/aiohttp
- **Boto3**: AWS service integration
- **Pandas**: Data transformation pipelines
- **Requests**: HTTP session management
- **Prometheus/Grafana**: Performance monitoring

## Installation

### Prerequisites
- Docker 20.10+
- AWS CLI v2 configured
- Python 3.10+

### Local Development
```bash
# Clone repository
git clone https://github.com/yourusername/uk-supermarket-scraper.git
cd uk-supermarket-scraper

# Build Docker image
docker build -t supermarket-scraper:latest .

# Run container with environment variables
docker run -it --rm \
  -e AWS_ACCESS_KEY_ID=<your_key> \
  -e AWS_SECRET_ACCESS_KEY=<your_secret> \
  supermarket-scraper:latest \
  python main.py --retailers tesco sainsburys
