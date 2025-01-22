#!/bin/bash
set -e

# Load environment variables from .env file
if [ -f .env ]; then
    source .env
fi

# Required variables
AWS_REGION=${AWS_REGION:-"eu-west-1"}
ECR_REPO_NAME=${ECR_REPO_NAME:-"aldi-scraper"}
LAMBDA_FUNCTION_NAME=${LAMBDA_FUNCTION_NAME:-"aldi-scraper-function"}
LAMBDA_MEMORY=${LAMBDA_MEMORY:-2048}
LAMBDA_TIMEOUT=${LAMBDA_TIMEOUT:-900}
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Create ECR repository if it doesn't exist
aws ecr describe-repositories --repository-names ${ECR_REPO_NAME} --region ${AWS_REGION} || \
    aws ecr create-repository --repository-name ${ECR_REPO_NAME} --region ${AWS_REGION}

# Authenticate Docker to ECR
aws ecr get-login-password --region ${AWS_REGION} | \
    docker login --username AWS --password-stdin ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

# Build and tag Docker image
docker build --platform linux/arm64 -t ${ECR_REPO_NAME}:latest .
docker tag ${ECR_REPO_NAME}:latest ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}:latest

# Push image to ECR
docker push ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}:latest

# Check if Lambda function exists
if aws lambda get-function --function-name ${LAMBDA_FUNCTION_NAME} --region ${AWS_REGION} >/dev/null 2>&1; then
    # Update existing function
    aws lambda update-function-code \
        --function-name ${LAMBDA_FUNCTION_NAME} \
        --image-uri ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}:latest \
        --region ${AWS_REGION}

    # Update function configuration
    aws lambda update-function-configuration \
        --function-name ${LAMBDA_FUNCTION_NAME} \
        --memory-size ${LAMBDA_MEMORY} \
        --timeout ${LAMBDA_TIMEOUT} \
        --region ${AWS_REGION}
else
    # Create new function
    aws lambda create-function \
        --function-name ${LAMBDA_FUNCTION_NAME} \
        --package-type Image \
        --code ImageUri=${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}:latest \
        --role ${LAMBDA_ROLE_ARN} \
        --memory-size ${LAMBDA_MEMORY} \
        --timeout ${LAMBDA_TIMEOUT} \
        --region ${AWS_REGION}
fi

echo "Deployment completed successfully!" 