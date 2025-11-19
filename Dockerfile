# Use Python 3.10 Slim image
FROM python:3.10-slim-buster

# Install Aria2 and Curl (for health checks if needed)
RUN apt-get update && \
    apt-get install -y aria2 curl && \
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