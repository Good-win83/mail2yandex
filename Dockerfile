FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mail2yandex.py web_admin.py imap_collector.py accounts.py mailbox_status.py delivery_history.py totp_auth.py ./
USER 65534:65534
EXPOSE 8025 8080
CMD ["python", "mail2yandex.py"]
