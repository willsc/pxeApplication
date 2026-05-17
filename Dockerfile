FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gcc libarchive-tools libffi-dev openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY templates ./templates
COPY pxe_templates ./pxe_templates
COPY ansible ./ansible
COPY scripts ./scripts
RUN chmod +x scripts/*.sh
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data /app/tftproot /app/ansible/playbooks

EXPOSE 8000
CMD ["pxe-app"]
