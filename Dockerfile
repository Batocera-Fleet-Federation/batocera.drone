FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    USERDATA_ROOT=/userdata \
    ROMS_ROOT=/userdata/roms \
    BIOS_ROOT=/userdata/bios \
    THEMES_ROOT=/userdata/themes \
    LOG_DIR=/userdata/system/drone-app/logs \
    HTTPS_PORT=443

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssl curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 443 8443
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "app/main.py"]
