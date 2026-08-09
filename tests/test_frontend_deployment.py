from pathlib import Path

import pytest

from frappe_harness.frontend_deployment import FrontendDeploymentError, package_frontend_dist


def test_packages_vite_dist_into_frappe_public_and_www(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-ab12.js").write_bytes(b"bundle")
    (tmp_path / "index.html").write_text('<html><head></head><body><script src="/assets/index-ab12.js"></script></body></html>', encoding="utf-8")
    artifacts = package_frontend_dist("task_tracker", tmp_path)
    assert artifacts["task_tracker/task_tracker/public/harness/assets/index-ab12.js"] == b"bundle"
    page = artifacts["task_tracker/task_tracker/www/task_tracker.html"].decode()
    assert "/assets/task_tracker/harness/assets/index-ab12.js" in page
    assert "/api/method/task_tracker.api.frontend_manifest" in page


def test_rejects_missing_or_unsafe_dist_assets(tmp_path):
    with pytest.raises(FrontendDeploymentError):
        package_frontend_dist("task_tracker", tmp_path)
