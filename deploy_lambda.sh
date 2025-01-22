#!/bin/bash
set -e

# Load environment variables
if [ -f .env ]; then
    source .env
fi

# Required variables
AWS_REGION=${AWS_REGION:-"eu-west-1"}
LAMBDA_FUNCTION_NAME=${LAMBDA_FUNCTION_NAME:-"aldi-scraper-function"}
LAMBDA_MEMORY=${LAMBDA_MEMORY:-2048}
LAMBDA_TIMEOUT=${LAMBDA_TIMEOUT:-900}

# Create and upload Lambda layer
echo "Creating Lambda layer..."
./create_layer.sh

LAYER_VERSION=$(aws lambda publish-layer-version \
    --layer-name aldi-scraper-dependencies \
    --description "Dependencies for Aldi scraper" \
    --zip-file fileb://layer.zip \
    --compatible-runtimes python3.9 \
    --region ${AWS_REGION} \
    --query 'Version' \
    --output text)

# Create deployment package for Lambda function
zip function.zip Aldi.py

# Check if Lambda function exists
if aws lambda get-function --function-name ${LAMBDA_FUNCTION_NAME} --region ${AWS_REGION} >/dev/null 2>&1; then
    # Update existing function
    aws lambda update-function-code \
        --function-name ${LAMBDA_FUNCTION_NAME} \
        --zip-file fileb://function.zip \
        --region ${AWS_REGION}

    # Update function configuration
    aws lambda update-function-configuration \
        --function-name ${LAMBDA_FUNCTION_NAME} \
        --runtime python3.9 \
        --handler Aldi.lambda_handler \
        --layers "arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:layer:aldi-scraper-dependencies:${LAYER_VERSION}" \
        --memory-size ${LAMBDA_MEMORY} \
        --timeout ${LAMBDA_TIMEOUT} \
        --environment "Variables={S3_BUCKET_NAME=${S3_BUCKET_NAME}}" \
        --region ${AWS_REGION}
else
    # Create new function
    aws lambda create-function \
        --function-name ${LAMBDA_FUNCTION_NAME} \
        --runtime python3.9 \
        --handler Aldi.lambda_handler \
        --role ${LAMBDA_ROLE_ARN} \
        --layers "arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:layer:aldi-scraper-dependencies:${LAYER_VERSION}" \
        --memory-size ${LAMBDA_MEMORY} \
        --timeout ${LAMBDA_TIMEOUT} \
        --environment "Variables={S3_BUCKET_NAME=${S3_BUCKET_NAME}}" \
        --zip-file fileb://function.zip \
        --region ${AWS_REGION}
fi

# Clean up
rm -f layer.zip function.zip

echo "Deployment completed successfully!" 