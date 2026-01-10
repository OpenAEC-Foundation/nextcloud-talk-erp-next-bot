FROM python:3.12-slim

# Install Node.js for Claude CLI
RUN apt-get update && apt-get install -y \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI globally
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .

# Create workspace directory
RUN mkdir -p /workspace

EXPOSE 8085

CMD ["gunicorn", "--bind", "0.0.0.0:8085", "--workers", "2", "--timeout", "180", "app:app"]
