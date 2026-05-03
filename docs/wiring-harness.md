# Private Phone Access

`session-control` should stay bound to `127.0.0.1`. For desktop and phone
access, expose it through the portfolio ingress layer instead of binding Flask
directly to the LAN.

Recommended shape:

```text
phone or desktop browser
  -> trusted WireGuard or LAN route
  -> wiring-harness Caddy with mTLS
  -> http://127.0.0.1:5420
```

Set `SESSION_CONTROL_PUBLIC_ORIGIN` to the external HTTPS origin used by Caddy
so CSRF checks accept proxied state-changing form posts.

Do not publish the raw Flask service on `0.0.0.0` unless a separate access
control layer is already in place and `SESSION_CONTROL_ALLOW_REMOTE=1` is set
intentionally.
