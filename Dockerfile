FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY app.py .
COPY index.html .
COPY dashboard.html .
COPY setup.html .
COPY profile.html .
COPY privacy.html .
COPY data-deletion.html .

# Create data directory
RUN mkdir -p /data

EXPOSE 8080

CMD ["gunicorn", "app:app", "--workers", "2", "--threads", "4", "--timeout", "120", "--bind", "0.0.0.0:8080"]
