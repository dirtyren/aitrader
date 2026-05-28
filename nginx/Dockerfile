FROM nginx:alpine

RUN apk add --no-cache apache2-utils openssl

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Remove the default catch-all server so our conf.d/default.conf is the only one
RUN rm -f /etc/nginx/nginx.conf.default

ENTRYPOINT ["/entrypoint.sh"]
