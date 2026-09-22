"""FastAPI 端到端测试。

需要 fastapi / httpx（见 requirements.txt）；环境缺依赖时自动跳过。
使用独立临时数据库，不影响 /data。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")

try:
    from fastapi.testclient import TestClient  # noqa: F401
except Exception:  # pragma: no cover - CI 无依赖环境
    TestClient = None

if TestClient is not None:
    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp.close()
    os.environ["SUB_DB_PATH"] = _tmp.name

    from app.main import app
    from app import database as db

    db.init_db()
    client = TestClient(app)


@unittest.skipIf(TestClient is None, "未安装 fastapi/httpx，跳过 API 测试")
class TestAPI(unittest.TestCase):
    def test_index(self):
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("字幕断句校验台", r.text)

    def test_full_flow_zh(self):
        with open(os.path.join(SAMPLE_DIR, "sample_zh.srt"), "rb") as f:
            r = client.post(
                "/api/analyze",
                files={"file": ("sample_zh.srt", f.read(), "text/plain")},
                data={"max_chars": "21", "min_dwell_ms": "1000", "cps_max": "17"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        summary = r.json()
        self.assertEqual(summary["language"], "zh")
        self.assertIn("export_url", summary)
        aid = summary["analysis_id"]

        detail = client.get(f"/api/analyses/{aid}").json()
        self.assertGreater(len(detail["cues"]), 0)
        for cue in detail["cues"]:
            self.assertIn("text", cue)
            self.assertLessEqual(cue["start_ms"], cue["end_ms"])

        vtt = client.get(f"/api/export/{aid}.vtt")
        self.assertEqual(vtt.status_code, 200)
        self.assertTrue(vtt.text.startswith("WEBVTT\n"))
        self.assertIn("attachment", vtt.headers["content-disposition"])

        issues = client.get("/api/issues?language=zh").json()["issues"]
        self.assertTrue(all(i["language"] == "zh" for i in issues))

        langs = client.get("/api/languages").json()["languages"]
        self.assertTrue(any(f["language"] == "zh" for f in langs))

    def test_full_flow_en(self):
        with open(os.path.join(SAMPLE_DIR, "sample_en.vtt"), "rb") as f:
            r = client.post(
                "/api/analyze",
                files={"file": ("sample_en.vtt", f.read(), "text/vtt")},
                data={"max_chars": "42", "min_dwell_ms": "1000", "cps_max": "17"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["language"], "en")

    def test_bad_file(self):
        r = client.post(
            "/api/analyze",
            files={"file": ("x.txt", b"nothing useful here", "text/plain")},
        )
        self.assertEqual(r.status_code, 400)

    def test_filter_type(self):
        with open(os.path.join(SAMPLE_DIR, "sample_zh.srt"), "rb") as f:
            client.post(
                "/api/analyze",
                files={"file": ("s.srt", f.read(), "text/plain")},
            )
        r = client.get("/api/issues?type=speed").json()["issues"]
        self.assertTrue(all(i["type"] == "speed" for i in r))

    def test_missing_analysis(self):
        self.assertEqual(client.get("/api/analyses/999999").status_code, 404)
        self.assertEqual(client.get("/api/export/999999.vtt").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
