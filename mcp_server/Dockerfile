FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp>=1.9,<2" "httpx>=0.27,<1" "uvicorn[standard]>=0.30,<1"
COPY server.py .
CMD ["python", "server.py"]
