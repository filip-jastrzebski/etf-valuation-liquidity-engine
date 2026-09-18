FROM python:3.11-slim

# Wyłączenie buforowania wyjścia (logi Pythona widać od razu w konsoli Dockera)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Kopiowanie tylko requirements.txt w celu wykorzystania cache warstw Dockera
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Kopiowanie całego kodu źródłowego i konfiguracji
COPY src/ ./src/
COPY config/ ./config/
COPY sql/ ./sql/
COPY tests/ ./tests/

# Domyślne polecenie: utrzymuje kontener w gotowości lub uruchamia testy/skrypt
CMD ["python", "-m", "src.main"]