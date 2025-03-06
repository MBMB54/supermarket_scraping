FROM python:3.9-slim
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
COPY ./chrome-installer.sh ./chrome-installer.sh
RUN chmod +x ./chrome-installer.sh
RUN ./chrome-installer.sh
RUN rm ./chrome-installer.sh
# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt
# Copy the main application code
COPY marks.py ./
# Command to run the Lambda function
CMD [ "python", "marks.py"]