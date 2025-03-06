FROM python:3.9-slim

# Install core dependencies
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
    && rm -rf /var/lib/apt/lists/*

# Install Chrome and Chromedriver using the for-testing API
RUN mkdir -p /opt/chrome /opt/chrome-driver && \
    # Get latest stable versions
    latest_stable_json="https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json" && \
    json_data=$(curl -s "$latest_stable_json") && \
    chrome_url=$(echo "$json_data" | jq -r ".channels.Stable.downloads.chrome[0].url") && \
    driver_url=$(echo "$json_data" | jq -r ".channels.Stable.downloads.chromedriver[0].url") && \
    # Download and extract Chrome
    curl -Lo /opt/chrome-linux.zip "$chrome_url" && \
    unzip -q /opt/chrome-linux.zip -d /opt/chrome && \
    rm -f /opt/chrome-linux.zip && \
    # Download and extract Chromedriver
    curl -Lo /opt/chromedriver-linux.zip "$driver_url" && \
    unzip -q /opt/chromedriver-linux.zip -d /opt/chrome-driver && \
    rm -f /opt/chromedriver-linux.zip && \
    # Create symlinks for easy access
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