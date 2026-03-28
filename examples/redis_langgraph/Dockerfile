FROM python:3.12-slim

WORKDIR /app

# Copy and install a2akit from local source (with redis + postgres extras)
COPY src/ /tmp/a2akit-src/src/
COPY pyproject.toml README.md /tmp/a2akit-src/
RUN pip install --no-cache-dir "/tmp/a2akit-src[redis,postgres]" && rm -rf /tmp/a2akit-src

# Install remaining dependencies
COPY examples/redis_langgraph/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY examples/redis_langgraph/server.py .

EXPOSE 8000

CMD ["python", "server.py"]
