FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir grpcio==1.66.2 grpcio-tools==1.66.2 httpx==0.27.2
COPY ext_mcp.proto bridge.py ./
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. ext_mcp.proto
CMD ["python", "bridge.py"]

