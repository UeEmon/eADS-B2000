FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home adsb
COPY --chown=adsb:adsb adsb ./adsb
USER adsb
CMD ["python", "-m", "adsb.receiver", "--help"]
