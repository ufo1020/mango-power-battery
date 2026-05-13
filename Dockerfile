FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY local_poll.py .
ENTRYPOINT ["python3", "local_poll.py"]
