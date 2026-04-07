FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY coding_agent/ coding_agent/

RUN pip install --no-cache-dir -e ".[all]"

ENTRYPOINT ["yucode"]
CMD ["--help"]
