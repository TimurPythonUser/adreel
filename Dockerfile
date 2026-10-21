FROM python:3.12-slim

# ffmpeg с libass (субтитры) и libx264 — из репозитория Debian
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Версия зафиксирована: с :latest сборка в разные дни давала бы разный uv
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

# Не root: бот открывает чужие страницы и файлы (trafilatura, yt-dlp, ffmpeg) — дыра
# в парсере не должна давать прав на весь контейнер. uid 1000 — обычный владелец
# ./workdir и ./logs на Linux-хосте, иначе в bind-mount не записать.
RUN useradd --create-home --uid 1000 app && mkdir -p /app && chown app:app /app
WORKDIR /app
USER app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1

# Сначала только зависимости — слой кешируется, пока не меняется uv.lock
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app . .
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "--no-dev", "python", "-m", "adreel.bot"]
