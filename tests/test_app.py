"""端到端自测：基线污染、断段规则、失败提交、版本化重算、冲突拒绝。

直接使用 Flask test client 走完整 HTTP 栈；每个用例独立临时数据库。
运行：PYTHONPATH=/tmp/pylibs:. python3 -I -m unittest -v tests.test_app
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest

from app import create_app


def csv_text(rows: list[tuple], header="batch,device,sample_seq,utc_ts,metric_value"):
    body = "\n".join(",".join(str(c) for c in r) for r in rows)
    return header + "\n" + body + "\n"


class DriftTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app(db_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, rows, simulate_fail=False):
        data = {
            "file": (io.BytesIO(csv_text(rows).encode()), "m.csv"),
        }
        url = "/api/uploads" + ("?simulate_fail=1" if simulate_fail else "")
        return self.client.post(
            url, data=data, content_type="multipart/form-data"
        )

    def publish(self, batch):
        return self.client.post(f"/api/batches/{batch}/publish")

    def check(self, baseline, target, multiplier=3.0):
        resp = self.client.post(
            "/api/checks",
            json={
                "baseline_batch": baseline,
                "target_batch": target,
                "mad_multiplier": multiplier,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        return resp.get_json()

    def segments(self, **params):
        resp = self.client.get("/api/segments", query_string=params)
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["segments"]


class TestBaselinePollution(DriftTestBase):
    """核查只允许使用已发布批次；草稿与后续上传都不能污染基线结论。"""

    BASE = [
        ("B1", "D1", i, f"2026-11-01T00:{i:02d}:00Z", 10.0 + (i % 2) * 0.1)
        for i in range(1, 9)
    ]

    def test_draft_cannot_be_used_and_publish_immutable(self):
        self.upload(self.BASE)
        # 未发布 → 核查必须 404
        resp = self.client.post(
            "/api/checks",
            json={"baseline_batch": "B1", "target_batch": "B1",
                  "mad_multiplier": 3},
        )
        self.assertEqual(resp.status_code, 404)

        self.assertEqual(self.publish("B1").status_code, 200)
        # 重复发布被拒
        self.assertEqual(self.publish("B1").status_code, 409)

    def test_new_upload_does_not_change_published_baseline(self):
        # 基线：围绕 10.0 小幅波动
        self.upload(self.BASE)
        self.publish("B1")
        # 待检：3 个连续高值，偏离 10 超过 3*MAD（MAD=0.1 → 带 9.7~10.3）
        target = [
            ("B2", "D1", 1, "2026-11-08T00:01:00Z", 10.0),
            ("B2", "D1", 2, "2026-11-08T00:02:00Z", 11.5),
            ("B2", "D1", 3, "2026-11-08T00:03:00Z", 11.6),
            ("B2", "D1", 4, "2026-11-08T00:04:00Z", 11.7),
        ]
        self.upload(target)
        self.publish("B2")
        result = self.check("B1", "B2")
        check_id = result["check_id"]

        # 之后向基线批次上传“污染版”（中位数被推到 20），仅草稿
        polluted = [
            ("B1", "D1", i, f"2026-11-09T00:{i:02d}:00Z", 20.0)
            for i in range(1, 9)
        ]
        resp = self.upload(polluted)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["created"][0]["version"], 2)

        # 历史核查读取的仍是 v1 版号与原结论
        detail = self.client.get(f"/api/checks/{check_id}").get_json()
        self.assertEqual(detail["check"]["baseline_version"], 1)
        med = next(d for d in detail["devices"] if d["device_id"] == "D1")
        # 8 个点 10.0/10.1 各四个 → 中位数 10.05，MAD 0.05
        self.assertAlmostEqual(med["baseline_median"], 10.05, places=6)
        self.assertAlmostEqual(med["baseline_mad"], 0.05, places=6)
        self.assertEqual(med["segment_count"], 1)
        seg = detail["segments"][0]
        self.assertEqual((seg["start_seq"], seg["end_seq"]), (2, 4))
        self.assertEqual(seg["direction"], "high")

        # 重算：v2 仍是草稿，系统只认最新“已发布”版 v1，
        # 因此受影响设备为空、基线版号仍为 1，旧结论不被污染
        resp = self.client.post(f"/api/checks/{check_id}/rerun")
        self.assertEqual(resp.status_code, 201)
        rerun = resp.get_json()
        self.assertEqual(rerun["affected_devices"], [])
        new_detail = self.client.get(
            f"/api/checks/{rerun['check_id']}"
        ).get_json()
        self.assertEqual(new_detail["check"]["baseline_version"], 1)
        self.assertEqual(
            new_detail["devices"][0]["baseline_median"], 10.05
        )
        # 发布污染版后再重算，才会切换到 v2 并改变设备结论
        self.publish("B1")
        resp2 = self.client.post(f"/api/checks/{check_id}/rerun")
        self.assertEqual(resp2.status_code, 201)
        polluted_detail = self.client.get(
            f"/api/checks/{resp2.get_json()['check_id']}"
        ).get_json()
        self.assertEqual(polluted_detail["check"]["baseline_version"], 2)
        self.assertEqual(
            polluted_detail["devices"][0]["baseline_median"], 20.0
        )
        # 最初的核查结论仍然原样可查
        old_again = self.client.get(f"/api/checks/{check_id}").get_json()
        self.assertEqual(old_again["check"]["baseline_version"], 1)
        self.assertEqual(len(old_again["segments"]), 1)


class TestSegmentBreaking(DriftTestBase):
    """断段：回到带内、缺测（序号不连续）、UTC 不递增、设备切换。"""

    def setUp(self):
        super().setUp()
        # MAD=0.1，3 倍带为 [9.7, 10.3]
        baseline = [
            ("B1", "D1", i, f"2026-11-01T00:{i:02d}:00Z",
             10.0 + (i % 2) * 0.1)
            for i in range(1, 9)
        ] + [
            ("B1", "D2", i, f"2026-11-01T00:{i:02d}:00Z",
             5.0 + (i % 2) * 0.1)
            for i in range(1, 9)
        ]
        self.upload(baseline)
        self.publish("B1")

    def _publish_target(self, rows):
        self.upload(rows)
        self.publish("B2")

    def test_return_to_band_breaks_and_min_three_samples(self):
        rows = [
            # 高值只有 2 个 → 不成段
            ("B2", "D1", 1, "2026-11-08T00:01:00Z", 11.0),
            ("B2", "D1", 2, "2026-11-08T00:02:00Z", 11.1),
            ("B2", "D1", 3, "2026-11-08T00:03:00Z", 10.0),  # 回带内
            # 连续 3 个高值 → 成段 4-6
            ("B2", "D1", 4, "2026-11-08T00:04:00Z", 11.2),
            ("B2", "D1", 5, "2026-11-08T00:05:00Z", 11.3),
            ("B2", "D1", 6, "2026-11-08T00:06:00Z", 11.4),
            ("B2", "D1", 7, "2026-11-08T00:07:00Z", 10.1),  # 断开
            # 再 3 个低值 → 第二段 8-10
            ("B2", "D1", 8, "2026-11-08T00:08:00Z", 8.0),
            ("B2", "D1", 9, "2026-11-08T00:09:00Z", 7.9),
            ("B2", "D1", 10, "2026-11-08T00:10:00Z", 7.8),
        ]
        self._publish_target(rows)
        result = self.check("B1", "B2")
        detail = self.client.get(
            f"/api/checks/{result['check_id']}"
        ).get_json()
        segs = detail["segments"]
        self.assertEqual(len(segs), 2)
        self.assertEqual(
            [(s["start_seq"], s["end_seq"], s["direction"]) for s in segs],
            [(4, 6, "high"), (8, 10, "low")],
        )
        self.assertTrue(all(s["sample_count"] >= 3 for s in segs))

    def test_missing_sample_breaks_run(self):
        rows = [
            ("B2", "D1", 1, "2026-11-08T00:01:00Z", 11.0),
            ("B2", "D1", 2, "2026-11-08T00:02:00Z", 11.1),
            # 序号 3 缺测
            ("B2", "D1", 4, "2026-11-08T00:04:00Z", 11.2),
            ("B2", "D1", 5, "2026-11-08T00:05:00Z", 11.3),
            ("B2", "D1", 6, "2026-11-08T00:06:00Z", 11.4),
        ]
        self._publish_target(rows)
        result = self.check("B1", "B2")
        detail = self.client.get(
            f"/api/checks/{result['check_id']}"
        ).get_json()
        # 前 2 个不成段；4-6 是缺测后的新连续段，仍应单独成段
        segs = detail["segments"]
        self.assertEqual(len(segs), 1)
        self.assertEqual((segs[0]["start_seq"], segs[0]["end_seq"]), (4, 6))

    def test_utc_non_increasing_breaks_run(self):
        rows = [
            ("B2", "D1", 1, "2026-11-08T00:03:00Z", 11.0),
            ("B2", "D1", 2, "2026-11-08T00:02:00Z", 11.1),  # 时刻倒退
            ("B2", "D1", 3, "2026-11-08T00:03:00Z", 11.2),  # 时刻持平
            ("B2", "D1", 4, "2026-11-08T00:04:00Z", 11.3),
            ("B2", "D1", 5, "2026-11-08T00:05:00Z", 11.4),
        ]
        self._publish_target(rows)
        result = self.check("B1", "B2")
        segs = self.client.get(
            f"/api/checks/{result['check_id']}"
        ).get_json()["segments"]
        # 断两次后只剩 3-5 连续（3 与 2 时刻相同而断开，3->4->5 严格递增）
        self.assertEqual(len(segs), 1)
        self.assertEqual((segs[0]["start_seq"], segs[0]["end_seq"]), (3, 5))

    def test_device_switch_separates_runs(self):
        rows = [
            ("B2", "D1", 1, "2026-11-08T00:01:00Z", 11.0),
            ("B2", "D1", 2, "2026-11-08T00:02:00Z", 11.1),
            ("B2", "D2", 1, "2026-11-08T00:01:00Z", 6.5),
            ("B2", "D2", 2, "2026-11-08T00:02:00Z", 6.6),
            ("B2", "D2", 3, "2026-11-08T00:03:00Z", 6.7),
        ]
        self._publish_target(rows)
        result = self.check("B1", "B2")
        detail = self.client.get(
            f"/api/checks/{result['check_id']}"
        ).get_json()
        # D1 只有 2 点不成段；D2 三点成段，设备互不串联
        d1 = next(d for d in detail["devices"] if d["device_id"] == "D1")
        d2 = next(d for d in detail["devices"] if d["device_id"] == "D2")
        self.assertEqual(d1["segment_count"], 0)
        self.assertEqual(d2["segment_count"], 1)
        self.assertEqual(detail["segments"][0]["device_id"], "D2")

    def test_zero_mad_baseline(self):
        # 常量基线：MAD=0，阈值带退化为中位数点
        baseline = [
            ("B3", "D9", i, f"2026-11-01T00:{i:02d}:00Z", 7.0)
            for i in range(1, 7)
        ]
        target = [
            ("B4", "D9", 1, "2026-11-08T00:01:00Z", 7.0),
            ("B4", "D9", 2, "2026-11-08T00:02:00Z", 7.01),
            ("B4", "D9", 3, "2026-11-08T00:03:00Z", 7.02),
            ("B4", "D9", 4, "2026-11-08T00:04:00Z", 7.03),
        ]
        self.upload(baseline)
        self.publish("B3")
        self.upload(target)
        self.publish("B4")
        result = self.check("B3", "B4")
        detail = self.client.get(
            f"/api/checks/{result['check_id']}"
        ).get_json()
        d9 = next(d for d in detail["devices"] if d["device_id"] == "D9")
        self.assertEqual(d9["baseline_mad"], 0.0)
        self.assertEqual(d9["segment_count"], 1)


class TestFailedCommitAndConflicts(DriftTestBase):
    def setUp(self):
        super().setUp()
        self.rows = [
            ("B1", "D1", i, f"2026-11-01T00:{i:02d}:00Z", 10.0 + i * 0.01)
            for i in range(1, 6)
        ]
        self.upload(self.rows)
        self.publish("B1")

    def test_simulated_commit_failure_changes_nothing(self):
        before_batches = self.client.get("/api/batches").get_json()
        before_count = len(before_batches["batches"])

        resp = self.upload(
            [("B1", "D1", 9, "2026-11-09T00:09:00Z", 99.0)],
            simulate_fail=True,
        )
        self.assertEqual(resp.status_code, 500)

        after_batches = self.client.get("/api/batches").get_json()
        self.assertEqual(len(after_batches["batches"]), before_count)
        # 当前已发布核查能力不受影响：仍可正常核查且无 99 这个值
        target = [
            ("B2", "D1", 1, "2026-11-08T00:01:00Z", 10.0),
            ("B2", "D1", 2, "2026-11-08T00:02:00Z", 10.0),
            ("B2", "D1", 3, "2026-11-08T00:03:00Z", 10.0),
        ]
        self.upload(target)
        self.publish("B2")
        result = self.check("B1", "B2")
        detail = self.client.get(
            f"/api/checks/{result['check_id']}"
        ).get_json()
        med = detail["devices"][0]["baseline_median"]
        self.assertNotAlmostEqual(med, 99.0)

    def test_same_key_conflicting_value_rejected_entirely(self):
        before = self.client.get("/api/batches").get_json()["batches"]
        conflicting = [
            ("B1", "D1", 1, "2026-11-09T00:01:00Z", 50.0),
            ("B1", "D1", 1, "2026-11-09T00:02:00Z", 51.0),  # 同键异值
        ]
        resp = self.upload(conflicting)
        self.assertEqual(resp.status_code, 409)
        after = self.client.get("/api/batches").get_json()["batches"]
        self.assertEqual(len(before), len(after))  # 没有产生新版本

    def test_exact_duplicate_rows_deduped(self):
        dup = [
            ("B5", "D1", 1, "2026-11-09T00:01:00Z", 1.0),
            ("B5", "D1", 1, "2026-11-09T00:01:00Z", 1.0),  # 完全重复
            ("B5", "D1", 2, "2026-11-09T00:02:00Z", 1.1),
        ]
        resp = self.upload(dup)
        self.assertEqual(resp.status_code, 201)
        created = resp.get_json()["created"][0]
        self.assertEqual(created["row_count"], 2)
        self.assertEqual(created["duplicate_rows_skipped"], 1)

    def test_duplicate_count_is_per_batch_in_multi_batch_file(self):
        rows = [
            ("B6", "D1", 1, "2026-11-09T00:01:00Z", 1.0),
            ("B6", "D1", 1, "2026-11-09T00:01:00Z", 1.0),  # B6 重复 1 行
            ("B7", "D1", 1, "2026-11-09T00:01:00Z", 2.0),
            ("B7", "D1", 2, "2026-11-09T00:02:00Z", 2.1),
            ("B7", "D1", 2, "2026-11-09T00:02:00Z", 2.1),  # B7 重复 1 行
            ("B7", "D1", 3, "2026-11-09T00:03:00Z", 2.2),
        ]
        resp = self.upload(rows)
        self.assertEqual(resp.status_code, 201)
        stats = {c["batch_name"]: c for c in resp.get_json()["created"]}
        self.assertEqual(stats["B6"]["row_count"], 1)
        self.assertEqual(stats["B6"]["duplicate_rows_skipped"], 1)
        self.assertEqual(stats["B7"]["row_count"], 3)
        self.assertEqual(stats["B7"]["duplicate_rows_skipped"], 1)

    def test_missing_column_rejected(self):
        resp = self.client.post(
            "/api/uploads",
            data={"file": (io.BytesIO(b"batch,device,sample_seq,utc_ts\n"
                                      b"X,D,1,2026-11-01T00:00:00Z\n"),
                           "bad.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)


class TestVersioningAndRerun(DriftTestBase):
    def _baseline(self, value=10.0):
        return [
            ("B1", "D1", i, f"2026-11-01T00:{i:02d}:00Z",
             value + (i % 2) * 0.1)
            for i in range(1, 9)
        ]

    def test_rerun_uses_new_versions_and_keeps_old_conclusion(self):
        self.upload(self._baseline())
        self.publish("B1")
        target = [
            ("B2", "D1", 1, "2026-11-08T00:01:00Z", 10.0),
            ("B2", "D1", 2, "2026-11-08T00:02:00Z", 11.5),
            ("B2", "D1", 3, "2026-11-08T00:03:00Z", 11.6),
            ("B2", "D1", 4, "2026-11-08T00:04:00Z", 11.7),
        ]
        self.upload(target)
        self.publish("B2")
        first = self.check("B1", "B2")
        old_id = first["check_id"]
        old_detail = self.client.get(f"/api/checks/{old_id}").get_json()
        self.assertEqual(old_detail["check"]["target_version"], 1)
        self.assertEqual(len(old_detail["segments"]), 1)

        # 更正待检：v2 中异常点被改回正常值
        corrected = [
            ("B2", "D1", 1, "2026-11-08T00:01:00Z", 10.0),
            ("B2", "D1", 2, "2026-11-08T00:02:00Z", 10.05),
            ("B2", "D1", 3, "2026-11-08T00:03:00Z", 10.06),
            ("B2", "D1", 4, "2026-11-08T00:04:00Z", 10.07),
        ]
        self.upload(corrected)
        self.publish("B2")

        rerun = self.client.post(f"/api/checks/{old_id}/rerun").get_json()
        new_id = rerun["check_id"]
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(rerun["supersedes"], old_id)
        self.assertIn("D1", rerun["affected_devices"])

        new_detail = self.client.get(f"/api/checks/{new_id}").get_json()
        self.assertEqual(new_detail["check"]["target_version"], 2)
        self.assertEqual(len(new_detail["segments"]), 0)

        # 旧结论原样可查
        old_again = self.client.get(f"/api/checks/{old_id}").get_json()
        self.assertEqual(old_again["check"]["target_version"], 1)
        self.assertEqual(len(old_again["segments"]), 1)

        # 无数据变化再重算：受影响设备为空，版号不变
        third = self.client.post(f"/api/checks/{new_id}/rerun").get_json()
        self.assertEqual(third["affected_devices"], [])

    def test_rerun_with_baseline_correction(self):
        self.upload(self._baseline(10.0))
        self.publish("B1")
        target = [
            ("B2", "D1", 1, "2026-11-08T00:01:00Z", 10.0),
            ("B2", "D1", 2, "2026-11-08T00:02:00Z", 11.5),
            ("B2", "D1", 3, "2026-11-08T00:03:00Z", 11.6),
            ("B2", "D1", 4, "2026-11-08T00:04:00Z", 11.7),
        ]
        self.upload(target)
        self.publish("B2")
        first = self.check("B1", "B2")["check_id"]

        # 基线更正：波动变大，MAD 变大，阈值带变宽
        new_base = [
            ("B1", "D1", i, f"2026-11-01T00:{i:02d}:00Z",
             10.0 + (i % 2) * 0.8)
            for i in range(1, 9)
        ]
        self.upload(new_base)
        self.publish("B1")
        rerun = self.client.post(f"/api/checks/{first}/rerun").get_json()
        detail = self.client.get(
            f"/api/checks/{rerun['check_id']}"
        ).get_json()
        self.assertEqual(detail["check"]["baseline_version"], 2)
        d1 = detail["devices"][0]
        self.assertGreater(d1["baseline_mad"], 0.1)
        # 宽阈值带下 11.5~11.7 不再构成偏离
        self.assertEqual(d1["segment_count"], 0)

    def test_segment_query_filters(self):
        # D1、D2 都给基线数据，避免“基线无该设备”被跳过
        baseline = [
            ("B1", dev, i, f"2026-11-01T00:{i:02d}:00Z",
             10.0 + (i % 2) * 0.1)
            for dev in ("D1", "D2") for i in range(1, 9)
        ]
        self.upload(baseline)
        self.publish("B1")
        target = [
            ("B2", "D1", 1, "2026-11-08T00:01:00Z", 11.5),
            ("B2", "D1", 2, "2026-11-08T00:02:00Z", 11.6),
            ("B2", "D1", 3, "2026-11-08T00:03:00Z", 11.7),
            ("B2", "D2", 1, "2026-11-08T00:01:00Z", 8.0),
            ("B2", "D2", 2, "2026-11-08T00:02:00Z", 7.9),
            ("B2", "D2", 3, "2026-11-08T00:03:00Z", 7.8),
            ("B2", "D2", 4, "2026-11-08T00:04:00Z", 7.7),
        ]
        self.upload(target)
        self.publish("B2")
        self.check("B1", "B2")

        self.assertEqual(len(self.segments()), 2)
        self.assertEqual(len(self.segments(device_id="D1")), 1)
        self.assertEqual(len(self.segments(direction="low")), 1)
        self.assertEqual(
            len(self.segments(min_samples=4)), 1
        )
        self.assertEqual(
            self.segments(batch_name="B2")[0]["target_batch"], "B2"
        )


class TestWebUI(DriftTestBase):
    def test_pages_render(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/segments").status_code, 200)
        rows = [
            ("B1", "D1", i, f"2026-11-01T00:{i:02d}:00Z", 10.0)
            for i in range(1, 5)
        ]
        self.upload(rows)
        # 表单发布 + 表单核查
        resp = self.client.post("/publish", data={"batch_name": "B1"})
        self.assertEqual(resp.status_code, 302)
        resp = self.client.post(
            "/checks/new",
            data={"baseline_batch": "B1", "target_batch": "B1",
                  "mad_multiplier": "3"},
        )
        self.assertEqual(resp.status_code, 302)
        check_id = self.client.get("/api/checks").get_json()["checks"][0]["id"]
        self.assertEqual(
            self.client.get(f"/checks/{check_id}").status_code, 200
        )
        resp = self.client.post(f"/checks/{check_id}/rerun")
        self.assertEqual(resp.status_code, 302)


if __name__ == "__main__":
    unittest.main(verbosity=2)
