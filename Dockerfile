FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=UTC CASCADA_DATOS=/app/datos
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/salud',timeout=5).status==200 else 1)"
CMD ["python", "-m", "cascada.principal"]
