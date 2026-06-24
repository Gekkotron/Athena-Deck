"""
Athena Deck — self-hosted service dashboard with auto-discovery.

Scans the configured host (default: host.docker.internal) for open TCP ports,
probes which ones speak HTTP/HTTPS, captures Playwright screenshots, and
serves the result as JSON + thumbnail images to the static frontend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image
from playwright.async_api import async_playwright

log = logging.getLogger("athena_deck")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

APP_PORT = int(os.environ.get("APP_PORT", "8888"))
SCAN_HOST = os.environ.get("SCAN_HOST", "host.docker.internal")
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/data"))
THUMBS_DIR = CACHE_DIR / "thumbs"
SERVICES_FILE = CACHE_DIR / "services.json"

# Concurrency / timing for port scanning.
SCAN_CONCURRENCY = int(os.environ.get("SCAN_CONCURRENCY", "500"))
SCAN_CONNECT_TIMEOUT = float(os.environ.get("SCAN_CONNECT_TIMEOUT", "0.3"))

# Screenshot timing.
# - TIMEOUT_MS: page-navigation timeout (networkidle / load).
# - SETTLE_MS: extra wait *after* navigation so JS-rendered UIs (SPAs, charts,
#   late-loading fonts/images) have time to paint before we screenshot them.
# - CONCURRENCY: how many Chromium contexts to run in parallel. One browser
#   instance shared across all of them; each context is ~30–80 MB of RAM in
#   practice, so 3 keeps a Geekom-class host comfortable.
SCREENSHOT_TIMEOUT_MS = int(os.environ.get("SCREENSHOT_TIMEOUT_MS", "12000"))
SCREENSHOT_SETTLE_MS = int(os.environ.get("SCREENSHOT_SETTLE_MS", "2500"))
SCREENSHOT_CONCURRENCY = int(os.environ.get("SCREENSHOT_CONCURRENCY", "3"))
# Reported `prefers-color-scheme` for the headless browser. Most self-hosted
# UIs honour this and render in their dark theme — matching the dashboard.
# Values: "dark" | "light" | "no-preference".
_COLOR_SCHEME_RAW = os.environ.get("SCREENSHOT_COLOR_SCHEME", "dark").lower()
SCREENSHOT_COLOR_SCHEME = (
    _COLOR_SCHEME_RAW if _COLOR_SCHEME_RAW in ("dark", "light", "no-preference") else "dark"
)
# Chromium's experimental "auto dark" — applies a content-level dark inversion
# to sites that DON'T respect prefers-color-scheme. Catches the long tail of
# hardcoded-light dashboards. Set to "false" to disable if the inversion makes
# specific apps look worse than their native light theme.
SCREENSHOT_FORCE_DARK = os.environ.get("SCREENSHOT_FORCE_DARK", "true").lower() in (
    "1", "true", "yes", "on"
)
THUMB_WIDTH = 400

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Hosts that resolve to "this machine" — used to decide whether to skip our
# own port from the discovered service list.
SELF_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"}

# Curated list of common web/admin ports for the "fast" scan. Kept as a
# constant at the top of the file so it's easy to tweak.
COMMON_PORTS: list[int] = sorted(
    set(
        [
            80, 81, 88, 443,
            1880,                                    # Node-RED
            2375, 2376,                              # Docker API
            3000, 3001, 3002, 3030,                  # Grafana, Uptime Kuma, misc
            4000, 4040, 4200,                        # ngrok/spark, Angular dev
            5000, 5001, 5050, 5173, 5555, 5601, 5678,  # n8n, Kibana, Vite, Flower
            5800, 5900,                              # qBittorrent web, VNC
            6379, 6443, 6767, 6789,                  # Redis, k8s, Bazarr
            7000, 7474, 7575, 7777, 7878,            # Neo4j, Homarr, Radarr
            8000, 8008, 8010,
            8080, 8081, 8086, 8096, 8112, 8123,      # InfluxDB, Jellyfin, Deluge, HA
            8181, 8200, 8384, 8443, 8444,            # Tautulli, Vault, Syncthing
            8686, 8688, 8787, 8800, 8888, 8989,      # Lidarr, Readarr, Sonarr
            9000, 9001, 9090, 9091, 9100, 9117,      # Portainer, Prometheus, Transmission, Jackett
            9200, 9443, 9696,                        # Elasticsearch, Portainer SSL, Prowlarr
            32400, 32469,                            # Plex
            51820,                                   # WireGuard UI
        ]
    )
)


# --------------------------------------------------------------------------- #
# Shared scan state
# --------------------------------------------------------------------------- #

SCAN_STATE: dict[str, Any] = {
    "running": False,
    "kind": None,           # "fast" | "full" | None
    "host": None,
    "total": 0,
    "checked": 0,
    "open_count": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "phase": "idle",        # "idle" | "ports" | "probe" | "screenshot" | "done"
    # Ports whose screenshot finished during the current screenshot phase.
    # The frontend polls this to drive per-tile shimmer state.
    "screenshots_done": [],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Port scanning
# --------------------------------------------------------------------------- #

async def _check_port(host: str, port: int, timeout: float) -> bool:
    """Return True if a TCP connection to host:port opens within `timeout`."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (asyncio.TimeoutError, OSError):
        return False
    try:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            # wait_closed can raise on aborted peers; we don't care.
            pass
    except Exception:
        pass
    return True


async def _scan_ports(host: str, ports: list[int]) -> list[int]:
    """Scan `ports` on `host` with bounded concurrency, updating SCAN_STATE."""
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    open_ports: list[int] = []
    SCAN_STATE["total"] = len(ports)
    SCAN_STATE["checked"] = 0
    SCAN_STATE["open_count"] = 0

    async def worker(p: int) -> None:
        async with sem:
            ok = await _check_port(host, p, SCAN_CONNECT_TIMEOUT)
        # Update counters outside the semaphore to keep slots flowing.
        SCAN_STATE["checked"] += 1
        if ok:
            open_ports.append(p)
            SCAN_STATE["open_count"] = len(open_ports)

    await asyncio.gather(*(worker(p) for p in ports))
    return sorted(open_ports)


# --------------------------------------------------------------------------- #
# HTTP probing
# --------------------------------------------------------------------------- #

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


async def _probe_http(host: str, port: int) -> Optional[dict]:
    """Try HTTP then HTTPS. Return {scheme, title} on first response, else None.

    The URL itself is intentionally NOT stored: the host we used (e.g.
    `host.docker.internal`) is container-internal and the browser can't resolve
    it. The frontend rebuilds the URL using the user's actual hostname.
    """
    for scheme in ("http", "https"):
        url = f"{scheme}://{host}:{port}"
        try:
            async with httpx.AsyncClient(
                verify=False, timeout=5.0, follow_redirects=True
            ) as client:
                r = await client.get(url)
        except Exception:
            continue
        # We accept any HTTP status — even 401/403 means there *is* a web UI.
        title: Optional[str] = None
        try:
            text = r.text or ""
            m = _TITLE_RE.search(text)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
        except Exception:
            title = None
        return {"scheme": scheme, "title": title or f"port {port}"}
    return None


# --------------------------------------------------------------------------- #
# Screenshots
# --------------------------------------------------------------------------- #

async def _screenshot_one(browser, svc: dict) -> bool:
    """Capture a thumbnail PNG for one service; return True on success."""
    url = f"{svc['scheme']}://{svc['scan_host']}:{svc['port']}"
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
        color_scheme=SCREENSHOT_COLOR_SCHEME,
    )
    try:
        page = await ctx.new_page()
        # Try networkidle first (better for SPAs), fall back to load.
        try:
            await page.goto(url, wait_until="networkidle", timeout=SCREENSHOT_TIMEOUT_MS)
        except Exception:
            try:
                await page.goto(url, wait_until="load", timeout=SCREENSHOT_TIMEOUT_MS)
            except Exception:
                # Even the load event timed out — screenshot whatever is there.
                pass
        # Let JS-heavy UIs finish painting before snapping.
        if SCREENSHOT_SETTLE_MS > 0:
            try:
                await page.wait_for_timeout(SCREENSHOT_SETTLE_MS)
            except Exception:
                pass
        png_bytes = await page.screenshot(type="png", full_page=False)
        img = Image.open(BytesIO(png_bytes))
        ratio = THUMB_WIDTH / img.width
        thumb = img.resize((THUMB_WIDTH, max(1, int(img.height * ratio))))
        thumb.save(THUMBS_DIR / f"{svc['port']}.png", "PNG", optimize=True)
        return True
    except Exception as e:
        log.warning("screenshot failed for %s: %s", url, e)
        return False
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def _screenshot_all(services: list[dict]) -> None:
    if not services:
        return
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, SCREENSHOT_CONCURRENCY))
    launch_args = ["--no-sandbox"]
    if SCREENSHOT_FORCE_DARK:
        # Chromium experimental flags: enable the WebContentsForceDark
        # feature *and* opt every tab into dark-mode rendering so pages with
        # hardcoded light themes get auto-inverted.
        launch_args += [
            "--enable-features=WebContentsForceDark",
            "--force-dark-mode",
        ]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=launch_args)
        try:
            async def worker(svc: dict) -> None:
                async with sem:
                    await _screenshot_one(browser, svc)
                # Mark this port done regardless of capture success — the
                # frontend tells "captured" apart from "failed" by whether
                # /api/thumb/<port> returns a PNG or a 404.
                SCAN_STATE["screenshots_done"].append(svc["port"])
            await asyncio.gather(*(worker(s) for s in services))
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Scan orchestration
# --------------------------------------------------------------------------- #

def _ports_for(kind: str) -> list[int]:
    if kind == "fast":
        return list(COMMON_PORTS)
    return list(range(1, 65536))


def _should_skip_self(host: str, port: int) -> bool:
    return host in SELF_HOSTS and port == APP_PORT


async def _run_scan(host: str, kind: str) -> None:
    SCAN_STATE.update(
        {
            "running": True,
            "kind": kind,
            "host": host,
            "total": 0,
            "checked": 0,
            "open_count": 0,
            "started_at": _now_iso(),
            "finished_at": None,
            "error": None,
            "phase": "ports",
            "screenshots_done": [],
        }
    )
    try:
        ports = _ports_for(kind)
        open_ports = await _scan_ports(host, ports)

        SCAN_STATE["phase"] = "probe"
        services: list[dict] = []
        for p in open_ports:
            if _should_skip_self(host, p):
                continue
            info = await _probe_http(host, p)
            if info is None:
                continue
            services.append(
                {
                    "port": p,
                    "scheme": info["scheme"],
                    "title": info["title"],
                    "scan_host": host,
                    "thumb": f"/api/thumb/{p}",
                    "last_seen": _now_iso(),
                }
            )

        # Persist the JSON before screenshots so the UI can render cards
        # immediately even if screenshotting takes a while.
        SERVICES_FILE.write_text(json.dumps(services, indent=2))

        SCAN_STATE["phase"] = "screenshot"
        try:
            await _screenshot_all(services)
        except Exception as e:
            log.exception("screenshot phase failed")
            SCAN_STATE["error"] = f"screenshot phase failed: {e}"

        SCAN_STATE["phase"] = "done"
    except Exception as e:
        log.exception("scan failed")
        SCAN_STATE["error"] = str(e)
    finally:
        SCAN_STATE["running"] = False
        SCAN_STATE["finished_at"] = _now_iso()


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    if not SERVICES_FILE.exists():
        SERVICES_FILE.write_text("[]")
    log.info(
        "athena-deck up on port %s (SCAN_HOST=%s, cache=%s)",
        APP_PORT, SCAN_HOST, CACHE_DIR,
    )
    yield


app = FastAPI(title="Athena Deck", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>index.html missing</h1>", status_code=500)
    return HTMLResponse(index.read_text(encoding="utf-8"))


@app.get("/favicon.svg")
async def favicon_svg() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/services")
async def list_services() -> JSONResponse:
    if not SERVICES_FILE.exists():
        return JSONResponse([])
    try:
        data = json.loads(SERVICES_FILE.read_text() or "[]")
    except json.JSONDecodeError:
        data = []
    return JSONResponse(data)


@app.get("/api/scan/status")
async def scan_status() -> JSONResponse:
    return JSONResponse(SCAN_STATE)


async def _start_scan(kind: str, host: Optional[str]) -> dict:
    if SCAN_STATE["running"]:
        raise HTTPException(status_code=409, detail="scan already running")
    target = (host or "").strip() or SCAN_HOST
    asyncio.create_task(_run_scan(target, kind))
    return {"ok": True, "kind": kind, "host": target}


@app.post("/api/scan/fast")
async def scan_fast(host: Optional[str] = None) -> dict:
    return await _start_scan("fast", host)


@app.post("/api/scan/full")
async def scan_full(host: Optional[str] = None) -> dict:
    return await _start_scan("full", host)


@app.get("/api/thumb/{port}")
async def thumb(port: int) -> Response:
    """Serve the cached thumbnail or 404 — the frontend uses a 404 as the
    'no screenshot available' signal to swap shimmer → static placeholder."""
    path = THUMBS_DIR / f"{port}.png"
    if path.exists():
        return FileResponse(path, media_type="image/png")
    raise HTTPException(status_code=404, detail="no thumbnail")
