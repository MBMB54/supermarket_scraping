FROM python:3.9-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    jq \
    unzip \
    libgbm-dev \
    libxss1 \
    libasound2 \
    libdbus-glib-1-2 \
    libx11-xcb1 \
    libgtk-3-0 \
    libxtst6 \
    fonts-liberation \
    xvfb \
    libnss3 \
    libdrm2 \
    libxkbcommon0 \
    libxshmfence1 \
    libglib2.0-0 \
    libpangocairo-1.0-0 \
    libxcb-dri3-0 \
    libgles2 \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome & Chromedriver
RUN mkdir -p /opt/chrome /opt/chrome-driver && \
    latest_stable_json="https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json" && \
    json_data=$(curl -s "$latest_stable_json") && \
    chrome_version=$(echo "$json_data" | jq -r ".channels.Stable.version") && \
    chrome_url=$(echo "$json_data" | jq -r ".channels.Stable.downloads.chrome[] | select(.platform == \"linux64\").url") && \
    driver_url=$(echo "$json_data" | jq -r ".channels.Stable.downloads.chromedriver[] | select(.platform == \"linux64\").url") && \
    echo "Installing Chrome v$chrome_version" && \
    curl -Lo /opt/chrome-linux.zip "$chrome_url" && \
    unzip -q /opt/chrome-linux.zip -d /opt/chrome && \
    rm -f /opt/chrome-linux.zip && \
    curl -Lo /opt/chromedriver-linux.zip "$driver_url" && \
    unzip -q /opt/chromedriver-linux.zip -d /opt/chrome-driver && \
    rm -f /opt/chromedriver-linux.zip && \
    ln -s /opt/chrome/chrome-linux64/chrome /usr/local/bin/chrome && \
    ln -s /opt/chrome-driver/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver && \
    chmod +x /usr/local/bin/chrome /usr/local/bin/chromedriver
# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt
# Copy the main application code
COPY marks.py ./
# Command to run the Lambda function
CMD [ "python", "marks.py"]