FROM python:3.11-slim-bookworm

# システム依存パッケージ
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    # Playwright用
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# uv インストール
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 作業ディレクトリ
WORKDIR /workspace

# Pythonパッケージ（uv sync）
COPY pyproject.toml /workspace/pyproject.toml
RUN uv sync

# Playwrightブラウザ（Chromium）
RUN uv run playwright install chromium --with-deps
