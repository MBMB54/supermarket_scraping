FROM python:3.9-slim
RUN apt-get update && apt-get install -y \
    wget \
    unzip \
    gnupg \
    curl
RUN apt-get update && apt-get install -y \
    wget \
    unzip \
    gnupg \
    # Chrome dependencies
    libatk1.0-0 \
    libcups2 \
    libgtk-3-0 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxi6 \
    libxrandr2 \
    libxtst6 \
    libnss3 \
    libxss1 \
    libasound2 \
    libdbus-glib-1-2 \
    libgbm1 \
    xauth \
    xvfb
# Copy and run the chrome installer script
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable

# Install Chromedriver (match Chrome version)
RUN CHROME_VERSION=$(google-chrome --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+') \
    && CHROMEDRIVER_VERSION=$(curl -s "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_$CHROME_VERSION") \
    && wget -q "https://chromedriver.storage.googleapis.com/$CHROMEDRIVER_VERSION/chromedriver_linux64.zip" \
    && unzip chromedriver_linux64.zip \
    && mv chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chrochromedriver
# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt
# Copy the main application code
COPY marks.py ./
# Command to run the Lambda function
CMD [ "python", "marks.py"]