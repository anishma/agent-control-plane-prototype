FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.0 "uvicorn[standard]==0.30.6" pyyaml==6.0.2
COPY app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]

