FROM python:3.12-slim-bookworm
LABEL io.hass.name="SPC Bluetooth Logger" \
      io.hass.description="Bluetooth inverter monitoring and priority controls" \
      io.hass.version="0.2.6" \
      io.hass.type="app" \
      io.hass.arch="aarch64|amd64"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY decoder.py poller.py web.py mqtt_bridge.py controls.py boot.py ./
RUN python -c "import decoder, mqtt_bridge, controls, web; print('SPC modules verified during build')"
CMD ["python", "boot.py", "--options", "/data/options.json"]
