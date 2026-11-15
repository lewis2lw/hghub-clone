FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY proxy/ ./proxy/
COPY web/ ./web/
COPY start.sh ./start.sh
RUN chmod +x /app/start.sh
EXPOSE 8081
CMD ["/app/start.sh"]
