#!/bin/bash
set -e

# Create layer directory structure
LAYER_DIR="python"
mkdir -p ${LAYER_DIR}

# Install dependencies into the layer directory
pip install -r requirements.txt --target ${LAYER_DIR}

# Install Chromium and ChromeDriver binaries
mkdir -p ${LAYER_DIR}/headless-chromium
curl -Lo "${LAYER_DIR}/headless-chromium/chromium.zip" "https://github.com/adieuadieu/serverless-chrome/releases/download/v1.0.0-57/stable-headless-chromium-amazonlinux-2.zip"
unzip "${LAYER_DIR}/headless-chromium/chromium.zip" -d "${LAYER_DIR}/headless-chromium/"
rm "${LAYER_DIR}/headless-chromium/chromium.zip"

# Create deployment package
zip -r layer.zip python/

# Clean up
rm -rf ${LAYER_DIR} 