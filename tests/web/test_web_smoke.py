"""Web smoke tests — routes respond, security headers are present,
static assets are served locally (offline-safe), and no page opens a
file download endpoint.
"""

from __future__ import annotations

import re
import uuid

from tests.web.conftest import CLOSE_PERIOD_FIXTURE


def test_root_redirects_to_period_close(web_client):
    response = web_client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/period-close"


def test_period_close_page_200(web_client):
    response = web_client.get(f"/period-close?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 200
    assert "月结工作台" in response.text


def test_period_close_default_period(web_client):
    response = web_client.get("/period-close")
    assert response.status_code == 200


def test_period_close_bad_period_is_400(web_client):
    response = web_client.get("/period-close?period=2031-13")
    assert response.status_code == 400
    response = web_client.get("/period-close?period=not-a-period")
    assert response.status_code == 400


def test_contract_360_page_200(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    response = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 200
    assert "合同360°" in response.text


def test_contract_360_missing_contract_is_404(web_client):
    response = web_client.get(f"/contracts/{uuid.uuid4()}?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 404


def test_contract_search_exact_single_match_redirects(web_client):
    response = web_client.get("/contracts/search?no=PO-CLOSE-001", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/contracts/")


def test_contract_search_no_match(web_client):
    response = web_client.get("/contracts/search?no=PO-NOT-FOUND-999")
    assert response.status_code == 200
    assert "没有找到" in response.text


def test_security_headers_on_html(web_client):
    response = web_client.get(f"/period-close?period={CLOSE_PERIOD_FIXTURE}")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp


def test_security_headers_on_json(web_client):
    response = web_client.post(
        "/api/invoice-item-allocations",
        json={"invoice_external_key": "DIGITAL-CLOSE-006", "line_no": 1, "contract_id": "bad", "source_item_key": "ITEM-A", "quantity": "50", "net_amount": "950.00"},
    )
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_static_assets_served_locally(web_client):
    css = web_client.get("/static/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    js = web_client.get("/static/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]


def test_no_inline_script_or_style_in_pages(web_client, contract_id_by_no):
    """CSP forbids inline script/style — the rendered pages must not
    contain any <script> or style="" markup."""
    pages = [
        f"/period-close?period={CLOSE_PERIOD_FIXTURE}",
        f"/contracts/{contract_id_by_no['PO-CLOSE-001']}?period={CLOSE_PERIOD_FIXTURE}",
        "/contracts/search?no=PO-CLOSE-001",
    ]
    for page in pages:
        html = web_client.get(page).text
        inline_script = re.search(r"<script(?![^>]*src)", html)
        assert inline_script is None, f"inline <script> found on {page}"
        assert "style=" not in html.lower(), f"inline style found on {page}"


def test_no_file_download_endpoint(web_client):
    """Phase 2C deliberately never exposes an arbitrary file download."""
    for path in ["/files", "/download", "/api/files", "/files?path=/etc/passwd"]:
        response = web_client.get(path)
        assert response.status_code in (404, 405), f"{path} must not be a file endpoint"
