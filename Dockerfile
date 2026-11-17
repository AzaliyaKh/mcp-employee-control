FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 PYTHONPATH=/app EMPLOYEES_DB=/data/private/registry.sqlite
WORKDIR /app
COPY employees/ /app/employees/
COPY privacy/ /app/privacy/
COPY worklog/ /app/worklog/
COPY integrations/ /app/integrations/
COPY analyzer/ /app/analyzer/
COPY examples/ /app/examples/
COPY docker/employees /usr/local/bin/employees
RUN chmod 755 /usr/local/bin/employees
CMD ["python", "-c", "from threading import Event; Event().wait()"]
