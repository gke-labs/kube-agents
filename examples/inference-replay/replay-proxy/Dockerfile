FROM python:3.11-slim
WORKDIR /app
RUN pip install fastapi uvicorn httpx
COPY replay_proxy.py .
EXPOSE 8080
CMD ["uvicorn", "replay_proxy:app", "--host", "0.0.0.0", "--port", "8080"]

