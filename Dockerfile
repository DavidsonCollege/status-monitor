FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Default command runs the web server.
# The cron Container Apps Job overrides this with: python -m cron.run
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
