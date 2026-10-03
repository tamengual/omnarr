FROM python:3.12-alpine
WORKDIR /srv
RUN apk add --no-cache su-exec
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENV OMNARR_CONFIG=/config/config.yml PYTHONUNBUFFERED=1
EXPOSE 8765
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8765/api/health >/dev/null || exit 1
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
