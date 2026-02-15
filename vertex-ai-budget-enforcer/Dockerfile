FROM python:3.11-slim

WORKDIR /app

# Install uv for fast Python package management
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

COPY requirements.txt .
RUN uv pip install --system -r requirements.txt

COPY main.py .

CMD exec python main.py
