FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "httpx>=0.27,<1" "langchain>=1.0,<2" "langgraph>=1.0,<2" "langchain-openai>=1.0,<2" "langchain-mcp-adapters>=0.3,<1"
COPY agent.py .
ENTRYPOINT ["python", "agent.py"]
