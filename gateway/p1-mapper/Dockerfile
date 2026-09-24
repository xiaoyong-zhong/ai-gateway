FROM python:3.11-slim-bookworm@sha256:e2d3af735aff6eeee600b1933bedd99da6645fedf572cc12ef4cc1331f2ceebe

RUN groupadd --system mapper && useradd --system --gid mapper mapper
WORKDIR /app
COPY app.py test_app.py /app/
USER mapper
EXPOSE 8081
ENTRYPOINT ["python", "/app/app.py"]
