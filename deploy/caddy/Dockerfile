FROM caddy:2.11.4-builder AS builder
RUN xcaddy build v2.11.4 --with github.com/caddy-dns/cloudflare@v0.2.4

FROM caddy:2.11.4-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile.cloudflare
COPY entrypoint.sh /usr/local/bin/bloodraven-caddy
RUN chmod 755 /usr/local/bin/bloodraven-caddy
ENTRYPOINT ["/usr/local/bin/bloodraven-caddy"]
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile.cloudflare", "--adapter", "caddyfile"]
