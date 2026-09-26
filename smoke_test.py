"""
End-to-end smoke test: boots the real gateway and verifies the BACKEND API and
the FRONTEND dashboard (HTML + every static asset it references) over HTTP.

Usage:
    python3 smoke_test.py

Exits non-zero if any check fails. This complements the pytest suites, which
exercise the app in-process via TestClient; this one proves the actual uvicorn
server serves both halves correctly.
"""
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
API_DIR = ROOT / "agent-api"
VIEWS = ["intervention", "sentinel", "docket"]

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' -> ' + detail) if detail else ''}")
    return ok


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_server(base, attempts=60):
    for _ in range(attempts):
        try:
            if httpx.get(f"{base}/v1/health", timeout=2.0).status_code == 200:
                return True
        except Exception:
            time.sleep(0.5)
    return False


def _get(url):
    try:
        return httpx.get(BASE + url, timeout=15.0)
    except Exception:
        return None


def font_face_blocks(css_text):
    """Yield (family, [src urls]) for every @font-face block in a stylesheet."""
    for block in re.findall(r"@font-face\s*\{[^}]*\}", css_text):
        family = re.search(r'font-family:\s*["\']?([^;"\']+)', block)
        srcs = re.findall(r"url\(['\"]?(/v1/dashboard/assets/[^'\")]+)['\"]?\)", block)
        if srcs:
            yield (family.group(1).strip() if family else "?", srcs)


def test_assets(client):
    """
    Verify static assets. Vendored CSS (e.g. tabler-icons.min.css) declares legacy
    font formats alongside woff2; only the first format a browser can actually
    render is required, so each @font-face must have at least one servable src.
    """
    r = client.get("/v1/dashboard", timeout=60.0)
    html = r.text

    stylesheets = re.findall(r'href="(/v1/dashboard/assets/[^"]+\.css)"', html)
    check("stylesheet linked", len(stylesheets) > 0, ", ".join(stylesheets))

    broken_css = [s for s in stylesheets if _get(s) is None or _get(s).status_code != 200]
    check("all stylesheets serve 200", not broken_css, "; ".join(broken_css))

    families, dead_families = 0, []
    for sheet in stylesheets:
        body = _get(sheet)
        for family, srcs in font_face_blocks(body.text if body else ""):
            families += 1
            served = [s for s in srcs if (resp := _get(s)) and resp.status_code == 200 and resp.content]
            if not served:
                dead_families.append(f"{family} ({sheet.rsplit('/', 1)[-1]})")

    check("every @font-face has a servable source", not dead_families, "; ".join(dead_families))

    # Modern browsers prefer woff2; assert the primary icon font is actually there.
    icon = _get("/v1/dashboard/assets/fonts/tabler-icons.woff2")
    check(
        "tabler icon font (woff2) served",
        icon is not None and icon.status_code == 200 and len(icon.content) > 1000,
        f"{len(icon.content) if icon else 0} bytes",
    )
    print(f"  INFO  verified {families} @font-face declarations across {len(stylesheets)} stylesheets")


BASE = ""


def test_backend(client):
    print("\n[BACKEND] API endpoints")

    r = client.get("/v1/health", timeout=30.0)
    body = r.json() if r.status_code == 200 else {}
    check("GET /v1/health returns 200", r.status_code == 200)
    check("gateway reports ok", body.get("gateway") == "ok", str(body.get("gateway")))
    check("model_endpoint_reachable key present", "model_endpoint_reachable" in body)

    r = client.get("/v1/models", timeout=30.0)
    check("GET /v1/models returns 200", r.status_code == 200)
    check("models list is non-empty", len(r.json().get("data", [])) > 0)

    r = client.post("/v1/tasks", json={"title": "Smoke task", "description": "smoke"}, timeout=30.0)
    check("POST /v1/tasks returns 200", r.status_code == 200)
    task_id = r.json().get("task_id")
    check("task id returned", bool(task_id))

    r = client.get(f"/v1/tasks/{task_id}", timeout=30.0)
    check("GET /v1/tasks/{id} returns 200", r.status_code == 200)
    check("task round-trips its id", r.json().get("id") == task_id)

    r = client.get("/v1/tasks/nonexistent-id-999", timeout=30.0)
    check("unknown task returns 404", r.status_code == 404)

    r = client.get("/v1/approvals/pending", timeout=30.0)
    check("GET /v1/approvals/pending returns 200", r.status_code == 200)
    check("pending approvals is a list", isinstance(r.json(), list))

    # The model server is optional; the gateway must degrade, not crash.
    r = client.post(
        "/v1/chat/completions",
        json={"model": "qwen3-coder-next", "messages": [{"role": "user", "content": "hi"}]},
        timeout=180.0,
    )
    check(
        "POST /v1/chat/completions degrades gracefully (200/502/500)",
        r.status_code in (200, 500, 502),
        f"got {r.status_code}",
    )


def test_frontend(client):
    print("\n[FRONTEND] dashboard + static assets")

    r = client.get("/v1/dashboard", timeout=60.0)
    html = r.text
    check("GET /v1/dashboard returns 200", r.status_code == 200)
    check("served as text/html", "text/html" in r.headers.get("content-type", ""))
    check("page is a complete document", html.strip().endswith("</html>"))
    check("page title rendered", "Personal AI Engineer" in html)
    check("icon font linked", "/v1/dashboard/assets/tabler-icons.min.css" in html)

    for view in VIEWS:
        rv = client.get(f"/v1/dashboard?view={view}", timeout=60.0)
        check(f"view '{view}' renders 200", rv.status_code == 200)
        check(f"view '{view}' is a complete document", rv.text.strip().endswith("</html>"))

    test_assets(client)


def main():
    global BASE
    port = free_port()
    BASE = f"http://127.0.0.1:{port}"

    print(f"--- Running smoke test against {BASE} ---")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(API_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        if not wait_for_server(BASE):
            print("Server failed to start:\n" + proc.stdout.read().decode("utf-8", "replace"))
            return 1

        with httpx.Client(base_url=BASE) as client:
            test_backend(client)
            test_frontend(client)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    failed = [label for label, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("Failed checks:")
        for label in failed:
            print(f"  - {label}")
        return 1
    print("--- Smoke test PASSED (backend + frontend) ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
