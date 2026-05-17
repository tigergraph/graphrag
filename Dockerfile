FROM python:3.10-slim

WORKDIR /app

# Install runtime dependencies
RUN pip install --no-cache-dir requests

# Copy source repository
COPY . .

# Set default port and run dashboard API
ENV PORT=8080
EXPOSE 8080

CMD ["python", "dashboard_api.py"]
