# FROM l4t-pytorch:r35.2.1-pth2.0-py3 as base-arm64

FROM python:3.9-buster as base-arm64

FROM python:3.9-buster as base-amd64

FROM base-${TARGETARCH}

# install nginx
RUN apt-get update && apt-get install nginx vim emacs libmariadbclient-dev default-libmysqlclient-dev libssl-dev nodejs npm python3-opencv libgl1 tesseract-ocr -y
COPY /conf/nginx.default /etc/nginx/sites-available/default
RUN ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log
# install mysqlclient seperately because it's only in prod not dev.
RUN pip install mysqlclient

# copy source and install dependencies
RUN mkdir -p /opt/app
COPY requirements.txt /opt/app/
RUN pip install --upgrade pip && pip install -r /opt/app/requirements.txt
ADD fighthealthinsurance /opt/app/fighthealthinsurance
RUN ln -s /opt/app/fighthealthinsurance/static /opt/app/static
COPY scripts/start-server.sh /opt/app/
COPY *.py /opt/app/
WORKDIR /opt/app/
RUN ls
RUN chown -R www-data:www-data /opt/app
RUN ls
# start server
EXPOSE 80
STOPSIGNAL SIGTERM
CMD ["/opt/app/start-server.sh"]
