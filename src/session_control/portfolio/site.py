"""portfolio_site: find a repository's portfolio site settings and serve its theme assets.

One file, standard library only, vendored into each repository that serves web pages
(traction-control's ``scripts/portfolio_sites.py vendor`` writes it, with the fallback assets
and a manifest, so a repository still works with none of the portfolio tooling around it).

A page always asks for ``/_portfolio/theme.css``, ``theme.js``, ``chrome.js`` and ``nav.json``.
Behind a Caddy that serves that path, Caddy answers first. Otherwise the application answers
from ``Site.response``, which looks for each file in this order and serves only the four names:

1. the site directory's ``assets/`` (the central store, see below)
2. the vendored fallback directory shipped inside the repository (the last resort)

and for ``nav.json`` falls back to an empty navigation, so the top bar shows just its theme
control.

The site directory is found, in order, from
``PORTFOLIO_SITE_DIR``; the checkout's ``.portfolio`` symlink; the central store entry for the
repository's own origin slug (the "slug fallback", used when the link is missing). Site settings
are layered: built-in defaults, then the repository's tracked ``config/portfolio-site.example.toml``
(synthetic values only), then the store entry's ``site.local.toml``. The store is
``$PORTFOLIO_STORE`` or ``~/.config/traction-control/sites`` and is owner-only; a link or slug
entry that resolves outside it is ignored, and a served file must be a regular file inside the
directory it was looked up in, so a symlink cannot be used to read anything else.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

PREFIX = "/_portfolio/"
ASSETS = {
    "theme.css": "text/css; charset=utf-8",
    "theme.js": "text/javascript; charset=utf-8",
    "chrome.js": "text/javascript; charset=utf-8",
    "nav.json": "application/json",
}
EMPTY_NAV = b'{"version": 1, "items": []}\n'
FRONTING = ("wiring-harness-caddy", "repo-caddy", "direct")
DEFAULTS: dict[str, Any] = {"fronting": "direct"}
MAX_ASSET_BYTES = 2_000_000
SITE_ENV = "PORTFOLIO_SITE_DIR"
STORE_ENV = "PORTFOLIO_STORE"
ALLOWED_HOSTS_ENV = "PORTFOLIO_ALLOWED_HOSTS"
EXAMPLE_CONFIG = Path("config") / "portfolio-site.example.toml"


def default_store(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get(STORE_ENV):
        return Path(env[STORE_ENV])
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "traction-control" / "sites"


def host_name(value: object) -> str:
    """The lower-case hostname in a ``Host`` value: no port, brackets or trailing dot.

    Only the first of a comma separated list counts, so ``Host: a, b`` cannot smuggle ``b``.
    """
    text = str(value or "").split(",", 1)[0].strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end != -1 else ""
    if text.count(":") == 1:
        text = text.rsplit(":", 1)[0]
    return text.rstrip(".")


def is_loopback_name(name: str) -> bool:
    """True for ``localhost`` and any loopback address literal."""
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def host_allowed(host_header: object, allowed: Iterable[str] = ()) -> bool:
    """True when a request's own ``Host`` is a loopback name or one of ``allowed``.

    This is the DNS-rebinding guard. A page on an attacker's hostname that resolves to a
    loopback address sends that hostname as both ``Host`` and ``Origin``, so comparing the
    two cannot tell it from a genuine request; only a list of the names the service answers
    to can. Forwarded headers (``X-Forwarded-Host``) never authorise a request.
    """
    name = host_name(host_header)
    return bool(name) and (is_loopback_name(name) or name in {host_name(item) for item in allowed})


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [host_name(item) for item in value if isinstance(item, str) and host_name(item)]


def store_hostnames(store: Path | str | None = None) -> frozenset[str]:
    """Every site hostname in the central store (for a service shared by all sites)."""
    root = Path(store) if store is not None else default_store()
    names: set[str] = set()
    try:
        entries = [entry for entry in root.iterdir() if entry.is_dir() and not entry.name.startswith("_")]
    except OSError:
        return frozenset()
    for entry in entries:
        site = _read_toml(entry / "site.local.toml").get("site", {})
        if isinstance(site, dict):
            names.update(_names(site.get("hostname")))
            names.update(_names(site.get("allowed_hosts")))
    return frozenset(names)


def registry_hostnames(portfolio_root: Path | str) -> frozenset[str]:
    """Every service hostname in the wiring-harness registry (``services[.local].toml``)."""
    base = Path(portfolio_root) / "util-repos" / "wiring-harness"
    names: set[str] = set()
    for filename in ("services.local.toml", "services.toml"):
        services = _read_toml(base / filename).get("services", [])
        if not isinstance(services, list):
            continue
        for service in services:
            if isinstance(service, dict):
                names.update(_names(service.get("hostname")))
    return frozenset(names)


def slug_dirname(slug: str) -> str:
    """``owner/repo`` as one directory name."""
    return slug.strip().lower().replace("/", "__")


_REMOTE = re.compile(
    r"(?:git@|ssh://git@|https?://(?:[^@/]+@)?)github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?\s*$"
)


def origin_slug(repo_root: Path) -> str | None:
    """The ``owner/repo`` slug of ``remote.origin`` read from the git config, or None."""
    git = repo_root / ".git"
    try:
        if git.is_file():  # a linked worktree: follow it to the shared config
            gitdir = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (repo_root / gitdir).resolve()
            common = gitdir / "commondir"
            git = (gitdir / common.read_text(encoding="utf-8").strip()).resolve() if common.exists() else gitdir
        text = (git / "config").read_text(encoding="utf-8")
    except (OSError, IndexError, ValueError):
        return None
    in_origin = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = stripped.replace(" ", "").lower() == '[remote"origin"]'
        elif in_origin and stripped.startswith("url"):
            match = _REMOTE.search(stripped.split("=", 1)[1].strip())
            if match:
                return f"{match.group(1)}/{match.group(2)}".lower()
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_regular(path: Path, root: Path) -> bytes | None:
    """The file's bytes if it is a regular file whose real path stays inside ``root``."""
    try:
        real = path.resolve(strict=True)
        if not real.is_file() or not _inside(real, root.resolve(strict=True)):
            return None
        if real.stat().st_size > MAX_ASSET_BYTES:
            return None
        return real.read_bytes()
    except OSError:
        return None


@dataclass
class Site:
    repo_root: Path
    site_dir: Path | None
    source: str  # env | link | slug | none
    settings: dict[str, Any]
    vendored: Path | None = None
    store: Path = field(default_factory=default_store)

    def asset(self, name: str) -> tuple[bytes, str] | None:
        """One allowlisted asset and its content type, or None."""
        ctype = ASSETS.get(name)
        if ctype is None:
            return None
        roots = []
        if self.site_dir is not None:
            roots.append(self.site_dir / "assets")
        if self.vendored is not None:
            roots.append(self.vendored)
        for root in roots:
            data = _read_regular(root / name, root)
            if data is not None:
                return data, ctype
        if name == "nav.json":
            return EMPTY_NAV, ctype
        return None

    def allowed_hosts(
        self, extra: Iterable[str] = (), env: Mapping[str, str] | None = None
    ) -> frozenset[str]:
        """The non-loopback names this site answers to.

        Its registered ``hostname`` and optional ``allowed_hosts`` (from the owner-only store
        entry, so private names never appear in a tracked file), ``PORTFOLIO_ALLOWED_HOSTS``
        (comma separated), and ``extra``.
        """
        env = os.environ if env is None else env
        names = _names(self.settings.get("hostname")) + _names(self.settings.get("allowed_hosts"))
        names += _names(str(env.get(ALLOWED_HOSTS_ENV, "")).split(","))
        names += _names([str(item) for item in extra])
        return frozenset(names)

    def host_allowed(
        self,
        host_header: object,
        extra: Iterable[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> bool:
        """``host_allowed`` for this site's own names."""
        return host_allowed(host_header, self.allowed_hosts(extra, env))

    def response(self, request_path: str) -> tuple[bytes, str] | None:
        """The answer for ``/_portfolio/<name>`` (query string ignored), or None if not ours."""
        path = request_path.split("?", 1)[0].split("#", 1)[0]
        if not path.startswith(PREFIX):
            return None
        return self.asset(path[len(PREFIX) :])

    def describe(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "site_dir": None if self.site_dir is None else str(self.site_dir),
            "fronting": self.settings.get("fronting"),
            "vendored": None if self.vendored is None else str(self.vendored),
        }


def resolve(
    repo_root: Path | str,
    *,
    vendored: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> Site:
    """Resolve the site for a checkout. Never raises: missing pieces fall through to defaults."""
    env = os.environ if env is None else env
    root = Path(repo_root)
    store = default_store(env)
    site_dir: Path | None = None
    source = "none"
    candidates: list[tuple[str, Path | None]] = [
        ("env", Path(env[SITE_ENV]) if env.get(SITE_ENV) else None),
        ("link", root / ".portfolio"),
    ]
    slug = origin_slug(root)
    candidates.append(("slug", store / slug_dirname(slug) if slug else None))
    for name, candidate in candidates:
        if candidate is None or not candidate.is_dir():
            continue
        real = candidate.resolve()
        try:
            if name != "env" and not _inside(real, store.resolve()):
                continue
        except OSError:
            continue
        site_dir, source = real, name
        break
    settings = dict(DEFAULTS)
    settings.update(_read_toml(root / EXAMPLE_CONFIG).get("site", {}))
    if site_dir is not None:
        settings.update(_read_toml(site_dir / "site.local.toml").get("site", {}))
    if settings.get("fronting") not in FRONTING:
        settings["fronting"] = DEFAULTS["fronting"]
    return Site(root, site_dir, source, settings, Path(vendored) if vendored else None, store)
