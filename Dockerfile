FROM python:3.12-alpine
# Keine pip-Abhaengigkeiten - alles laeuft auf der Standardbibliothek.
WORKDIR /app
COPY agent.py server.py /app/
COPY web /app/web
COPY config.example.json /app/config.example.json
ENV WH_CONFIG=/app/data/config.json \
    WH_STATE=/app/data/state.json \
    PORT=8088 \
    PYTHONUNBUFFERED=1
EXPOSE 8088
CMD ["python", "/app/server.py"]
