# Use Python 3.10 Slim image
FROM python:3.11-slim

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Run bot in webhook mode by default on Render (can be overridden in Render env vars)
ENV USE_WEBHOOK=1

# Install Aria2 and curl. Use no-install-recommends to keep image small
# and clean apt lists after installation to reduce image size.
RUN apt-get update && \
    apt-get install -y --no-install-recommends aria2 curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create downloads folder
RUN mkdir -p /app/downloads

# Copy all project files into the container
COPY . .

# Give execute permission to the start script
RUN chmod +x start.sh

# Command to run when container starts
CMD ["./start.sh"]