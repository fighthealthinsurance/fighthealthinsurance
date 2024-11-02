# FROM l4t-pytorch:r35.2.1-pth2.0-py3 as base-arm64

FROM python:3.11-bullseye as base-arm64

FROM python:3.11-bullseye as base-amd64

FROM base-${TARGETARCH}

ARG DEBIAN_FRONTEND=noninteractive

# install nginx
RUN apt-get update && apt-get upgrade -y && apt-get install nginx vim emacs libmariadb-dev-compat default-libmysqlclient-dev libssl-dev nodejs npm python3-opencv libgl1 tesseract-ocr nano nfs-common sudo iputils-ping hylafax-client pandoc texlive -y
COPY /conf/nginx.default /etc/nginx/sites-available/default
RUN ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log
# install mysqlclient seperately because it's only in prod not dev.
RUN pip install mysqlclient

# copy source and install dependencies
RUN mkdir -p /opt/fighthealthinsurance
RUN chown -R www-data:www-data /opt/fighthealthinsurance
# We hope requirements has not changed so we can use the cache
COPY requirements.txt /opt/fighthealthinsurance/
RUN pip install --upgrade pip && pip install -r /opt/fighthealthinsurance/requirements.txt
RUN mkdir -p /external_data
# We copy static early ish since it could also be cached nicely
ADD --chown=www-data:www-data static /opt/fighthealthinsurance/static
ADD --chown=www-data:www-data fighthealthinsurance /opt/fighthealthinsurance/fighthealthinsurance
COPY --chown=www-data:www-data scripts/start-server.sh /opt/fighthealthinsurance/
COPY --chown=www-data:www-data *.py /opt/fighthealthinsurance/
WORKDIR /opt/fighthealthinsurance/
RUN sudo -u www-data HOME=$(pwd) python initial.py
# RUN chown -R www-data:www-data /opt/fighthealthinsurance
# start server
EXPOSE 80
STOPSIGNAL SIGTERM
CMD ["/opt/fighthealthinsurance/start-server.sh"]
