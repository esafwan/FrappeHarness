"""Package the fixed Vite build into a Frappe app-owned asset/page bundle."""

from __future__ import annotations

from pathlib import Path
import re


class FrontendDeploymentError(ValueError):
    pass


_ASSET = re.compile(r"^[A-Za-z0-9._-]+$")


def package_frontend_dist(app_slug: str, dist_dir: str | Path) -> dict[str, bytes]:
    """Return safe Frappe ``public``/``www`` artifacts from one Vite dist.

    The build is treated as an opaque fixed runtime: only regular files below
    ``dist`` and ``dist/assets`` are copied, and the page rewrites asset URLs
    to Frappe's immutable ``/assets/<app>/harness/...`` namespace.
    """
    if not isinstance(app_slug, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", app_slug):
        raise FrontendDeploymentError("app_slug must be a safe lower-case identifier")
    root = Path(dist_dir)
    index = root / "index.html"
    if not index.is_file():
        raise FrontendDeploymentError("Vite dist must contain index.html")
    artifacts: dict[str, bytes] = {}
    for source in sorted((root / "assets").glob("*")):
        if not source.is_file() or not _ASSET.fullmatch(source.name):
            raise FrontendDeploymentError("dist assets must be regular safe-named files")
        artifacts[f"{app_slug}/{app_slug}/public/harness/assets/{source.name}"] = source.read_bytes()
    html = index.read_text(encoding="utf-8")
    html = html.replace("/assets/", f"/assets/{app_slug}/harness/assets/")
    html = html.replace("<head>", f'<head><script>globalThis.frappe_harness_manifest_endpoint="/api/method/{app_slug}.api.frontend_manifest";</script>', 1)
    artifacts[f"{app_slug}/{app_slug}/www/{app_slug}.html"] = html.encode("utf-8")
    return artifacts
