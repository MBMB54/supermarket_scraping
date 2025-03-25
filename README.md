# UK Supermarket Price Scraping Platform

![AWS](https://img.shields.io/badge/AWS-%23FF9900.svg?style=for-the-badge&logo=amazon-aws&logoColor=white)
![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=for-the-badge&logo=docker&logoColor=white)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)

A scalable cloud-native solution for monitoring grocery prices across major UK supermarkets, built with Docker containers and AWS serverless technologies.

## 🚀 Key Features
- **Multi-Supermarket Support**: Simultaneous scraping of Ocado, Tesco, and Aldi.
- **AWS Batch Processing**: Containerized jobs with Fargate Spot instances
- **Data Lake Architecture**: S3 storage with Parquet partitioning
- **Automated Scheduling**: Lambda-driven cron jobs using AWS EventBridge
- **Cost Optimization**: 70% savings through Spot instances and resource right-sizing

## 🛠️ Technical Architecture

```mermaid
graph TD
    A[Scheduler Lambda] -->|Triggers| B[AWS Batch]
    B -->|Runs| C[Docker Containers]
    C -->|Stores Data| D[S3 Data Lake]
    D -->|Query| E[Athena]
    E -->|Visualize| F[QuickSight]
```

##  🌐   Future Roadmap

- Addition of new supermarkets - Sainsburys and Asda
  
- Price change alerts via SNS

- Historical analysis to track long term inflation and compare prices between supermarkets

- ML-powered price prediction and forecasting (SageMaker)

-

