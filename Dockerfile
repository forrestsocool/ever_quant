FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# Install system dependencies (tzdata for accurate Beijing time, curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for caching
COPY radar_requirements.txt .
RUN pip install --no-cache-dir -r radar_requirements.txt

# Copy application files
COPY national_team_radar/ ./national_team_radar/
COPY tushare/sxsc_tushare/ ./tushare/sxsc_tushare/
COPY run_radar.py run_radar_mcp.py ./

# Ensure data directory exists for volume mount
RUN mkdir -p /app/national_team_radar/data

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:8765/api/status || exit 1

CMD ["python", "run_radar.py", "--host", "0.0.0.0", "--port", "8765"]
