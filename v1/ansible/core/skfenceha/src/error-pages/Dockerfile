FROM python:3.11-slim

# Note: Docker CLI not needed - we mount /var/run/docker.sock and use subprocess
# Docker socket is mounted at runtime for Docker API access

# Install Python dependencies
RUN pip install --no-cache-dir psutil docker

# Create app directory
WORKDIR /app

# Copy application files
COPY server.py /app/
COPY error.html /app/error.html

# Make server executable
RUN chmod +x /app/server.py

# Expose port
EXPOSE 8080

# Run server
CMD ["python3", "/app/server.py"]

