FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
RUN useradd -r -u 10001 aigw
COPY --from=build /install /usr/local
COPY deploy/compose/bootstrap.yaml /etc/aigw/bootstrap.yaml
USER aigw
ENV AIGW_ROLE=gateway
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"
CMD ["aigw", "serve", "--port", "8080"]
