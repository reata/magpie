FROM python:3.10-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Run the image as a non-root user
RUN adduser --quiet magpie
USER magpie

COPY magpie /mnt/magpie
WORKDIR /mnt/

# $PORT environment variable will be passed with --env in docker run command
CMD uvicorn magpie.main:app --host 0.0.0.0 --port $PORT
