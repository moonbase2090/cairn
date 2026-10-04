FROM public.ecr.aws/lambda/python:3.13

WORKDIR /var/task
COPY pyproject.toml README.md /tmp/cairn/
COPY src/ /tmp/cairn/src/
RUN pip install --no-cache-dir "/tmp/cairn[aws]"
COPY aws/infra/lambda/sync_api.py ./sync_api.py
CMD ["sync_api.handler"]
