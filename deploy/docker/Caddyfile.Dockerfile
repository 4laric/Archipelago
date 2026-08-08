# Caddy, with the rate-limit module compiled in.
#
# WHY THIS EXISTS AT ALL. Stock `caddy:2` has no rate limiting, and /generate needs one: the
# per-generation limits in webgui/generator.py bound a single request and say nothing about how many
# requests arrive. So Caddy is rebuilt with github.com/mholt/caddy-ratelimit via xcaddy.
#
# 🛑 THE TRADE-OFF, STATED RATHER THAN BURIED. This replaces an official image with a locally
# compiled binary carrying a THIRD-PARTY module -- the module's own README says it is "not an
# official repository of the Caddy Web Server organization". That is a supply-chain step down from
# `image: caddy:2`, and it is on the box that terminates TLS. It is taken deliberately, because an
# unlimited /generate is a worse and more certain problem than a well-regarded Apache-2.0 module
# from Caddy's own author. The version below is PINNED for that reason: a floating `@latest` on a
# component in this position would be the actual mistake.
ARG CADDY_VERSION=2.10.2
FROM caddy:${CADDY_VERSION}-builder AS builder
ARG RATELIMIT_VERSION=v0.1.0
RUN xcaddy build --with github.com/mholt/caddy-ratelimit@${RATELIMIT_VERSION}

FROM caddy:${CADDY_VERSION}
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
