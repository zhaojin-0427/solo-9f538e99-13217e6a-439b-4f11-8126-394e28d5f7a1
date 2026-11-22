"""端到端自测：仪器电压读数比对。

覆盖：
- 共测样品匹配、共测数/超差数/最大相差样本（含并列取最小序号）；
- 无共测样品直接提示；容差边界（abs_diff == tolerance 不算超差）；
- 容差查询只读统计索引（不访问 reading 表）；
- 草稿不可比对；发布失败回滚、版本保持草稿、可重试；
- 更正后新版统计变化，旧版统计仍可查；
- 约束：整数微伏、|值|<=1e6、每批 10 万条、6 台仪器；同键异值整份拒绝；
- 网页渲染与下拉数据。
运行：python -I -m unittest -v tests.test_app
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest

from app import create_app
from app.db import MAX_DEVICES_PER_BATCH, MAX_READINGS_PER_BATCH

HEADER = "batch,device,sample_seq,microvolt"


def csv_text(rows: list[tuple], header: str = HEADER) -> str:
    body = "\n".join(",".join(str(c) for c in r) for r in rows)
    return header + "\n" + body + "\n"


class VoltTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app(db_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, rows, simulate_fail=False, raw_text=None):
        data = {
            "file": (
                io.BytesIO((raw_text or csv_text(rows)).encode()),
                "m.csv",
            ),
        }
        url = "/api/uploads" + ("?simulate_fail=1" if simulate_fail else "")
        return self.client.post(
            url, data=data, content_type="multipart/form-data"
        )

    def publish(self, batch, version=None, simulate_fail=False):
        url = f"/api/batches/{batch}/publish"
        params = []
        if version is not None:
            params.append(f"version={version}")
        if simulate_fail:
            params.append("simulate_fail=1")
        if params:
            url += "?" + "&".join(params)
        return self.client.post(url)

    def compare(self, batch, version, dx, dy, tol):
        return self.client.get(
            "/api/compare",
            query_string={
                "batch_name": batch,
                "version": version,
                "device_x": dx,
                "device_y": dy,
                "tolerance": tol,
            },
        )


# --------------------------------------------------------------------------- #
# 比对统计正确性
# --------------------------------------------------------------------------- #


class TestComparisonStats(VoltTestBase):
    # D1/D2 共测样本 1..5；D3 只测样本 7、8（与 D1/D2 无共测）
    ROWS = [
        ("B1", "D1", 1, 100),
        ("B1", "D1", 2, 100),
        ("B1", "D1", 3, 100),
        ("B1", "D1", 4, 100),
        ("B1", "D1", 5, 100),
        ("B1", "D2", 1, 100),   # diff 0
        ("B1", "D2", 2, 105),   # diff 5
        ("B1", "D2", 3, 110),   # diff 10
        ("B1", "D2", 4, 110),   # diff 10（与样本3并列最大）
        ("B1", "D2", 5, 90),    # diff 10（第三个并列）
        ("B1", "D2", 6, 50),    # D1 未测 → 不参与
        ("B1", "D3", 7, 10),
        ("B1", "D3", 8, 20),
    ]

    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload(self.ROWS).status_code, 201)
        self.assertEqual(self.publish("B1").status_code, 200)

    def test_common_count_and_histogram_tolerance(self):
        # 共测 5 个（样本 1..5）
        r = self.compare("B1", 1, "D1", "D2", 0).get_json()
        self.assertEqual(r["common_count"], 5)
        self.assertEqual(r["over_tolerance_count"], 4)  # diff 5,10,10,10
        self.assertEqual(r["max_abs_diff"], 10)
        # 最大差并列 → 取最小样本序号 3
        self.assertEqual(r["max_diff_seq"], 3)
        self.assertTrue(r["has_common"])

        # 容差 5：严格“超过”才算，diff==5 不计 → 3 个
        r5 = self.compare("B1", 1, "D1", "D2", 5).get_json()
        self.assertEqual(r5["over_tolerance_count"], 3)
        # 容差 10：没有超过 10 的
        r10 = self.compare("B1", 1, "D1", "D2", 10).get_json()
        self.assertEqual(r10["over_tolerance_count"], 0)
        # 容差足够大
        r_big = self.compare("B1", 1, "D2", "D1", 1_000_000).get_json()
        self.assertEqual(r_big["over_tolerance_count"], 0)
        # 仪器顺序互换结果一致（规范化），且仍是同一对
        self.assertEqual(r_big["device_a"], "D1")
        self.assertEqual(r_big["device_b"], "D2")

    def test_no_common_samples_message(self):
        r = self.compare("B1", 1, "D1", "D3", 0).get_json()
        self.assertEqual(r["common_count"], 0)
        self.assertFalse(r["has_common"])
        self.assertEqual(r["over_tolerance_count"], 0)
        self.assertIsNone(r["max_diff_seq"])
        self.assertIn("没有共测样品", r["message"])
        # 网页端同样渲染提示
        page = self.client.get(
            "/compare",
            query_string={
                "batch_name": "B1", "version": 1,
                "device_x": "D1", "device_y": "D3", "tolerance": 0,
            },
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("没有共测样品", page.get_data(as_text=True))

    def test_negative_abs_diff_is_absolute(self):
        rows = [
            ("B2", "A", 1, -500),
            ("B2", "A", 2, 500),
            ("B2", "B", 1, -480),   # |diff|=20
            ("B2", "B", 2, 470),    # |diff|=30
        ]
        self.upload(rows)
        self.publish("B2")
        r = self.compare("B2", 1, "A", "B", 25).get_json()
        self.assertEqual(r["common_count"], 2)
        self.assertEqual(r["over_tolerance_count"], 1)
        self.assertEqual(r["max_abs_diff"], 30)
        self.assertEqual(r["max_diff_seq"], 2)

    def test_tolerance_query_does_not_touch_reading_table(self):
        """核心约束：换容差只查统计索引，SQL 层面不出现 reading 表。"""
        with self.app.app_context():
            from app.db import get_db
            from app import services as svc

            conn = get_db()
            plans = []
            for tol in (0, 5, 10, 999):
                # 用 EXPLAIN QUERY PLAN 抓取该容差查询实际访问的表
                pair = conn.execute(
                    "SELECT id FROM pair_stat WHERE batch_name=? AND version=? "
                    "AND device_a=? AND device_b=?",
                    ("B1", 1, "D1", "D2"),
                ).fetchone()
                plan = conn.execute(
                    "EXPLAIN QUERY PLAN SELECT COALESCE(SUM(freq),0) "
                    "FROM diff_histogram WHERE pair_stat_id=? AND abs_diff>?",
                    (pair["id"], tol),
                ).fetchall()
                plans.extend([p["detail"] for p in plan])

            joined = " ".join(plans)
            self.assertIn("diff_histogram", joined)
            self.assertNotIn("reading", joined.lower())

            # 再用 trace 兜底：执行服务层查询期间记录所有被读的表
            accessed: set[str] = set()

            def tracer(_conn, _cur, statement):
                low = statement.lower()
                if low.startswith("select") and "reading" in low:
                    accessed.add("reading")

            conn.set_trace_callback(tracer)
            for tol in range(0, 12):
                svc.compare_devices("B1", 1, "D1", "D2", tol)
            conn.set_trace_callback(None)
            self.assertNotIn("reading", accessed)

    def test_same_device_rejected(self):
        resp = self.compare("B1", 1, "D1", "D1", 0)
        self.assertEqual(resp.status_code, 400)

    def test_unknown_pair_404(self):
        resp = self.compare("B1", 1, "D1", "D9", 0)
        self.assertEqual(resp.status_code, 404)

    def test_negative_tolerance_rejected(self):
        resp = self.client.get(
            "/api/compare",
            query_string={
                "batch_name": "B1", "version": 1,
                "device_x": "D1", "device_y": "D2", "tolerance": -1,
            },
        )
        self.assertEqual(resp.status_code, 400)


# --------------------------------------------------------------------------- #
# 草稿 / 发布 / 版本固化
# --------------------------------------------------------------------------- #


class TestPublishingAndVersioning(VoltTestBase):
    def test_draft_cannot_be_compared(self):
        self.upload([("B1", "D1", 1, 100), ("B1", "D2", 1, 101)])
        resp = self.compare("B1", 1, "D1", "D2", 0)
        self.assertEqual(resp.status_code, 404)
        # 发布后可查
        self.assertEqual(self.publish("B1").status_code, 200)
        self.assertEqual(self.compare("B1", 1, "D1", "D2", 0).status_code, 200)

    def test_published_immutable_and_no_duplicate_publish(self):
        self.upload([("B1", "D1", 1, 100)])
        self.publish("B1")
        self.assertEqual(self.publish("B1").status_code, 409)

    def test_correction_keeps_old_version_stats(self):
        v1 = [
            ("B1", "D1", 1, 100), ("B1", "D1", 2, 100),
            ("B1", "D2", 1, 110), ("B1", "D2", 2, 120),
        ]
        self.upload(v1)
        self.publish("B1")
        old = self.compare("B1", 1, "D1", "D2", 5).get_json()
        self.assertEqual(old["common_count"], 2)
        self.assertEqual(old["over_tolerance_count"], 2)
        self.assertEqual(old["max_abs_diff"], 20)
        self.assertEqual(old["max_diff_seq"], 2)

        # 更正：v2 把 D2 拉齐，差值变小
        v2 = [
            ("B1", "D1", 1, 100), ("B1", "D1", 2, 100),
            ("B1", "D2", 1, 101), ("B1", "D2", 2, 102),
        ]
        resp = self.upload(v2)
        self.assertEqual(resp.get_json()["created"][0]["version"], 2)
        # v2 还是草稿 → 比对 v2 被拒，v1 结论不变
        self.assertEqual(self.compare("B1", 2, "D1", "D2", 5).status_code, 404)
        old_again = self.compare("B1", 1, "D1", "D2", 5).get_json()
        self.assertEqual(old_again["max_abs_diff"], 20)

        self.publish("B1", version=2)
        new = self.compare("B1", 2, "D1", "D2", 5).get_json()
        self.assertEqual(new["over_tolerance_count"], 0)
        self.assertEqual(new["max_abs_diff"], 2)
        # 旧版统计仍可查
        old_still = self.compare("B1", 1, "D1", "D2", 5).get_json()
        self.assertEqual(old_still["version"], 1)
        self.assertEqual(old_still["max_abs_diff"], 20)

    def test_publish_failure_rolls_back_and_draft_retryable(self):
        rows = [("B1", "D1", 1, 100), ("B1", "D2", 1, 105)]
        self.upload(rows)
        # 模拟发布失败：状态必须仍是 draft，且没有任何统计残留
        resp = self.publish("B1", simulate_fail=True)
        self.assertEqual(resp.status_code, 500)
        batches = self.client.get("/api/batches").get_json()["batches"]
        self.assertEqual(batches[0]["status"], "draft")

        with self.app.app_context():
            from app.db import get_db
            conn = get_db()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM pair_stat").fetchone()["n"],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM diff_histogram"
                ).fetchone()["n"],
                0,
            )

        # 草稿可重试：正常发布成功
        self.assertEqual(self.publish("B1").status_code, 200)
        r = self.compare("B1", 1, "D1", "D2", 0).get_json()
        self.assertEqual(r["common_count"], 1)
        self.assertEqual(r["over_tolerance_count"], 1)

    def test_upload_failure_creates_no_version(self):
        before = len(self.client.get("/api/batches").get_json()["batches"])
        resp = self.upload(
            [("B1", "D1", 1, 100)], simulate_fail=True
        )
        self.assertEqual(resp.status_code, 500)
        after = len(self.client.get("/api/batches").get_json()["batches"])
        self.assertEqual(before, after)


# --------------------------------------------------------------------------- #
# 输入校验与约束
# --------------------------------------------------------------------------- #


class TestValidation(VoltTestBase):
    def test_non_integer_reading_rejected(self):
        resp = self.upload(
            [], raw_text=HEADER + "\nB1,D1,1,12.5\n"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("整数", resp.get_json()["error"])

    def test_out_of_range_reading_rejected(self):
        resp = self.upload([("B1", "D1", 1, 1_000_001)])
        self.assertEqual(resp.status_code, 400)
        resp2 = self.upload([("B1", "D1", 1, -1_000_001)])
        self.assertEqual(resp2.status_code, 400)
        # 边界值合法
        ok = self.upload(
            [("B1", "D1", 1, 1_000_000), ("B1", "D1", 2, -1_000_000)]
        )
        self.assertEqual(ok.status_code, 201)

    def test_conflicting_same_key_rejects_entire_file(self):
        rows = [
            ("B1", "D1", 1, 100),
            ("B1", "D1", 1, 101),  # 同键异值
            ("B1", "D2", 1, 50),
        ]
        resp = self.upload(rows)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            self.client.get("/api/batches").get_json()["batches"], []
        )

    def test_exact_duplicates_deduped(self):
        rows = [
            ("B1", "D1", 1, 100),
            ("B1", "D1", 1, 100),  # 完全重复
            ("B1", "D1", 2, 101),
        ]
        resp = self.upload(rows)
        self.assertEqual(resp.status_code, 201)
        created = resp.get_json()["created"][0]
        self.assertEqual(created["row_count"], 2)
        self.assertEqual(created["duplicate_rows_skipped"], 1)

    def test_missing_column_rejected(self):
        resp = self.upload(
            [], raw_text="batch,device,sample_seq\nB1,D1,1\n"
        )
        self.assertEqual(resp.status_code, 400)

    def test_too_many_devices_rejected(self):
        rows = [
            ("B1", f"D{i}", 1, 100)
            for i in range(MAX_DEVICES_PER_BATCH + 1)
        ]
        resp = self.upload(rows)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("最多 6 台", resp.get_json()["error"])

    def test_too_many_readings_rejected(self):
        # 100001 条，分散在 2 台仪器（避免触发仪器数限制）
        rows = []
        for i in range(MAX_READINGS_PER_BATCH + 1):
            rows.append(("B1", "D1" if i % 2 else "D2", i, 0))
        resp = self.upload(rows)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("100000", resp.get_json()["error"])

    def test_limit_boundary_accepted(self):
        # 恰好 100000 条、6 台仪器应通过
        rows = []
        for i in range(MAX_READINGS_PER_BATCH):
            rows.append(("B1", f"D{i % 6}", i, 0))
        resp = self.upload(rows)
        self.assertEqual(resp.status_code, 201)
        created = resp.get_json()["created"][0]
        self.assertEqual(created["row_count"], MAX_READINGS_PER_BATCH)
        self.assertEqual(created["device_count"], 6)

    def test_chinese_header_supported(self):
        text = "批次,仪器,样本序号,电压读数\nB1,D1,1,-250\nB1,D2,1,-240\n"
        resp = self.upload([], raw_text=text)
        self.assertEqual(resp.status_code, 201)
        self.publish("B1")
        r = self.compare("B1", 1, "D1", "D2", 5).get_json()
        self.assertEqual(r["over_tolerance_count"], 1)
        self.assertEqual(r["max_abs_diff"], 10)

    def test_multi_batch_file(self):
        rows = [
            ("B1", "D1", 1, 100), ("B1", "D2", 1, 101),
            ("B2", "D1", 1, 200), ("B2", "D2", 1, 203),
        ]
        resp = self.upload(rows)
        self.assertEqual(resp.status_code, 201)
        names = {c["batch_name"] for c in resp.get_json()["created"]}
        self.assertEqual(names, {"B1", "B2"})
        self.publish("B1")
        self.publish("B2")
        r1 = self.compare("B1", 1, "D1", "D2", 0).get_json()
        r2 = self.compare("B2", 1, "D1", "D2", 2).get_json()
        self.assertEqual(r1["max_abs_diff"], 1)
        self.assertEqual(r2["over_tolerance_count"], 1)  # diff 3 > 2


# --------------------------------------------------------------------------- #
# 网页与 API
# --------------------------------------------------------------------------- #


class TestWebUI(VoltTestBase):
    def _seed(self):
        rows = [
            ("B1", "D1", 1, 100), ("B1", "D1", 2, 100),
            ("B1", "D2", 1, 110), ("B1", "D2", 2, 100),
            ("B1", "D3", 9, 5),
        ]
        self.upload(rows)
        self.publish("B1")

    def test_pages_render(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/compare").status_code, 200)
        self._seed()
        page = self.client.get("/compare").get_data(as_text=True)
        self.assertIn("B1", page)
        # 下拉数据 JSON 中包含三台仪器
        self.assertIn("D1", page)
        self.assertIn("D3", page)

    def test_form_publish_and_compare(self):
        self.upload([
            ("B1", "D1", 1, 100),
            ("B1", "D2", 1, 108),
        ])
        resp = self.client.post("/publish", data={"batch_name": "B1"})
        self.assertEqual(resp.status_code, 302)
        resp = self.client.get(
            "/compare",
            query_string={
                "batch_name": "B1", "version": 1,
                "device_x": "D1", "device_y": "D2", "tolerance": 5,
            },
        )
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        self.assertIn("共测样品数", text)
        self.assertIn("1", text)  # 共测 1，超差 1

    def test_devices_api(self):
        self._seed()
        resp = self.client.get("/api/batches/B1/versions/1/devices")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["devices"], ["D1", "D2", "D3"])
        # 不存在的版号 404
        self.assertEqual(
            self.client.get("/api/batches/B1/versions/9/devices").status_code,
            404,
        )

    def test_pair_stats_api(self):
        self._seed()
        resp = self.client.get("/api/batches/B1/versions/1/pair-stats")
        self.assertEqual(resp.status_code, 200)
        stats = resp.get_json()["pair_stats"]
        # 3 台仪器 → 3 对
        self.assertEqual(len(stats), 3)
        d1d2 = next(
            s for s in stats if s["device_a"] == "D1" and s["device_b"] == "D2"
        )
        self.assertEqual(d1d2["common_count"], 2)
        self.assertEqual(d1d2["max_abs_diff"], 10)
        self.assertEqual(d1d2["max_diff_seq"], 1)
        d1d3 = next(
            s for s in stats
            if s["device_a"] == "D1" and s["device_b"] == "D3"
        )
        self.assertEqual(d1d3["common_count"], 0)
        self.assertIsNone(d1d3["max_diff_seq"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
