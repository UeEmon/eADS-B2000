FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# rtl-sdr provides the rtl_sdr tool for a USB RTL-SDR (compose.usb.yaml, Linux only)
RUN apt-get update && apt-get install -y --no-install-recommends rtl-sdr && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home adsb \
    && mkdir /data && chown adsb:adsb /data
COPY --chown=adsb:adsb adsb ./adsb
USER adsb
CMD ["python", "-m", "adsb.receiver", "--help"]
