FROM python:3.12.9-bookworm AS base-arm64

FROM python:3.12.9-bookworm AS base-amd64

FROM base-${TARGETARCH}

ARG DEBIAN_FRONTEND=noninteractive

# install all of the tools we need.
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install nginx vim emacs libmariadb-dev-compat default-libmysqlclient-dev libssl-dev python3-opencv libgl1 tesseract-ocr nano nfs-common sudo iputils-ping hylafax-client pandoc texlive texlive-luatex texlive-latex-extra texlive-xetex postgresql ca-certificates curl libpq-dev wkhtmltopdf libcairo2-dev libgirepository1.0-dev pkg-config python3-dev weasyprint -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
# Install npm & nodes from https://github.com/nodesource/distributions instead of debian which lags
RUN curl -fsSL https://deb.nodesource.com/setup_20.x -o nodesource_setup.sh && \
    bash nodesource_setup.sh && \
    apt-get install -y nodejs && \
    rm nodesource_setup.sh
COPY /conf/nginx.default /etc/nginx/sites-available/default
RUN ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log

# copy source and install dependencies
RUN mkdir -p /opt/fighthealthinsurance
RUN chown -R www-data:www-data /opt/fighthealthinsurance
# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
# We hope requirements has not changed so we can use the cache
COPY *requirements.txt /opt/fighthealthinsurance/
RUN export PATH=/root/.local/bin:$PATH && \
    uv pip install --system -r /opt/fighthealthinsurance/requirements.txt && \
    uv pip install --system -r /opt/fighthealthinsurance/deploy-requirements.txt
RUN mkdir -p /external_data
# GeoIP city database (chat US-state hint + ASN tracking). Fetched and
# sha256-verified at build time from the digest pinned in the repo, so the
# bytes geoip2fast later unpickles are the ones we reviewed -- no runtime
# download, no writable path, root-owned and world-readable like the rest of
# the application code. A build with no pinned digest, an unreachable
# upstream, or a republished LATEST asset that no longer matches the pin
# still succeeds and simply ships without the database (unverified bytes are
# never installed); the app soft-fails and warns at startup, naming the path
# below. Add --require here to make shipping without it fail the build
# instead. Web-image only:
# both callers (websockets.py, rest_views.py) run in the web process, so the
# ray image does not need the database.
COPY scripts/fetch_geoip_db.sh scripts/geoip2fast-city-asn-ipv6.sha256 /opt/fighthealthinsurance/scripts/
RUN /opt/fighthealthinsurance/scripts/fetch_geoip_db.sh \
    --dest /opt/geoip/geoip2fast-city-asn-ipv6.dat.gz
ENV FHI_GEOIP_CITY_DB=/opt/geoip/geoip2fast-city-asn-ipv6.dat.gz
# We copy static early ish since it could also be cached nicely
# Remember to update the combined ray docker file for these copies
ADD --chown=www-data:www-data static /opt/fighthealthinsurance/static
ADD --chown=www-data:www-data fighthealthinsurance /opt/fighthealthinsurance/fighthealthinsurance
# We need to copy the generated blog posts file since it's not in git.
# COPY --chown=www-data:www-data fighthealthinsurance/static/blog_posts.json /opt/fighthealthinsurance/fighthealthinsurance/static/blog_posts.json
ADD --chown=www-data:www-data fhi_users /opt/fighthealthinsurance/fhi_users
ADD --chown=www-data:www-data charts /opt/fighthealthinsurance/charts
COPY --chown=www-data:www-data scripts/start-server.sh /opt/fighthealthinsurance/
COPY --chown=www-data:www-data *.py /opt/fighthealthinsurance/
ADD --chown=www-data:www-data conf /opt/fighthealthinsurance/conf
WORKDIR /opt/fighthealthinsurance/
# Generate the blog metadata inside the container now that all of the files
# are in the correct locations.
RUN ./manage.py generate_blog_metadata
RUN ./manage.py compress --force
RUN sudo -u www-data HOME=$(pwd) python initial.py
RUN mkdir -p ~www-data
RUN chown -R www-data ~www-data
RUN chown -R www-data /external_data
# The Dev configuration defaults storage to these repo-local paths; the
# root-run manage.py commands above bake them into the image root-owned, so
# without this chown a Dev-configured container (e.g. web-dev) hits
# PermissionError on every storage write — which CombinedStorage swallows,
# silently dropping uploads.
RUN mkdir -p /opt/fighthealthinsurance/external_data /opt/fighthealthinsurance/external_data_b \
    && chown -R www-data /opt/fighthealthinsurance/external_data /opt/fighthealthinsurance/external_data_b
# RUN chown -R www-data:www-data /opt/fighthealthinsurance
# start server
EXPOSE 80
STOPSIGNAL SIGTERM
RUN mkdir -p /var/lib/nginx/body && chown -R www-data /var/lib/nginx/body
ARG RELEASE=unknown
# Expose the image version to the app so the deployment-time model-backend
# health check can record which deployment it ran for (see
# fighthealthinsurance/ml/model_health_check.py deployment_id()).
ENV FHI_RELEASE=${RELEASE}
CMD ["/opt/fighthealthinsurance/start-server.sh"]
