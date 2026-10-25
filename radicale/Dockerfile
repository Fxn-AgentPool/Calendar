FROM python:3.12-slim

ARG RADICALE_VERSION=3.8.3
ARG BCRYPT_VERSION=5.0.0

RUN pip install --no-cache-dir "radicale==${RADICALE_VERSION}" "bcrypt==${BCRYPT_VERSION}" \
 && useradd --system --uid 10001 --home-dir /var/lib/radicale --shell /usr/sbin/nologin radicale \
 && mkdir -p /var/lib/radicale/collections \
 && chown -R radicale:radicale /var/lib/radicale

COPY config /etc/radicale/config
COPY rights.template /etc/radicale/rights.template
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

USER radicale
EXPOSE 5232
VOLUME /var/lib/radicale/collections

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:5232/.web/', timeout=4); sys.exit(0)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["radicale", "--config", "/etc/radicale/config"]
