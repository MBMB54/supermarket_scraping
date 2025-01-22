FROM public.ecr.aws/lambda/python:3.9-arm64

# Install Chrome and ChromeDriver more efficiently
RUN yum update -y && \
    yum install -y \
    unzip \
    chromium \
    chromium-headless \
    chromium-chromedriver && \
    yum clean all && \
    rm -rf /var/cache/yum

# Set Chrome options for Lambda environment
ENV CHROME_PATH=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

# Install only required dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}
RUN pip install --no-cache-dir -r requirements.txt

# Copy function code
COPY Aldi.py ${LAMBDA_TASK_ROOT}

# Set the CMD to your handler
CMD [ "Aldi.lambda_handler" ] 