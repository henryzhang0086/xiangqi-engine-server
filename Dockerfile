FROM python:3.11-slim

WORKDIR /app

RUN pip install websockets

COPY bridge.py .
COPY pikafish .
COPY elite.nnue .

RUN chmod +x pikafish

EXPOSE 8765

CMD ["python3", "bridge.py", "--engine", "./pikafish", "--port", "8765", "--threads", "2", "--hash", "128"]
