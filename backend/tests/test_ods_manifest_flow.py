"""贴源表状态流转修复 + ODS表版本清单专项测试。

覆盖：
- 状态只能按 草稿→待校验→已上线→已下线 单向推进，待校验不能直接下线
- 下线状态与 DWD 父层引用在同一调用落库
- 清单只收录已上线贴源表，只投影在线/下线、顺序、阶段、父层引用
- 阶段（归档→恢复→切换）单向推进
- 下线/刷新冲突的两种结果（下线优先、刷新优先）
- 中断重试：回到完整版本，只续写未完成部分
- 已下线贴源表不能作为新明细父层，刷新读到下线父层被拒
"""
from __future__ import annotations

from app.store import store
from app.services.ods_layer import OdsLayerService
from app.services.dwd_layer import DwdLayerService
from app.services.ods_manifest import (
    OdsManifestService,
    POLICY_OFFLINE_FIRST,
    POLICY_REFRESH_FIRST,
)


def _reset():
    from app.seed import SEED_ROWS
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    store.clear_manifest()


class TestOdsStatusFlow:
    def setup_method(self):
        _reset()
        self.ods = OdsLayerService()

    def test_draft_cannot_go_offline_directly(self):
        entry, _ = self.ods.create_entry(
            {"表编号": "ODS_T_001", "来源系统": "测试", "表名称": "ods_t1"})
        result, message = self.ods.run_action(entry["id"], "标记下线")
        assert result is None
        assert "状态流转错误" in message

    def test_pending_cannot_go_offline_directly(self):
        entry, _ = self.ods.create_entry(
            {"表编号": "ODS_T_002", "来源系统": "测试", "表名称": "ods_t2"})
        self.ods.run_action(entry["id"], "提交校验")
        result, message = self.ods.run_action(entry["id"], "标记下线")
        assert result is None
        assert "已上线" in message

    def test_status_advances_only_in_order(self):
        entry, _ = self.ods.create_entry(
            {"表编号": "ODS_T_003", "来源系统": "测试", "表名称": "ods_t3"})
        # 草稿不能跳到已上线
        result, _ = self.ods.run_action(entry["id"], "确认上线")
        assert result is None
        # 正常单向推进
        self.ods.run_action(entry["id"], "提交校验")
        self.ods.run_action(entry["id"], "确认上线")
        assert store.find("ods_layer", entry["id"])["status"] == "已上线"
        # 已上线不能回退到待校验
        result, message = self.ods.run_action(entry["id"], "提交校验")
        assert result is None and "状态流转错误" in message
        # 已下线是终态
        self.ods.run_action(entry["id"], "标记下线")
        result, message = self.ods.run_action(entry["id"], "确认上线")
        assert result is None

    def test_seed_online_tables_can_offline(self):
        entry = store.find_by_field("ods_layer", "表编号", "ODS_ORD_001")
        result, _ = self.ods.run_action(entry["id"], "标记下线")
        assert result is not None and result["status"] == "已下线"


class TestOdsManifest:
    def setup_method(self):
        _reset()
        self.ods = OdsLayerService()
        self.dwd = DwdLayerService()
        self.manifest = OdsManifestService()

    def test_manifest_only_contains_online_tables_and_projected_fields(self):
        # 草稿表不进清单
        entry, _ = self.ods.create_entry(
            {"表编号": "ODS_DRAFT", "来源系统": "测试", "表名称": "ods_draft"})
        self.manifest.sync_from_online_ods()
        codes = {row["表编号"] for row in self.manifest.list_entries()}
        assert "ODS_DRAFT" not in codes
        # seed 的三张已上线表进入清单
        assert {"ODS_ORD_001", "ODS_PAY_001", "ODS_USR_001"} <= codes
        # 列表只保留投影字段
        first = self.manifest.list_entries()[0]
        assert set(first.keys()) == {
            "顺序号", "表编号", "表名称", "来源系统", "在线状态", "阶段",
            "待切换", "中断中", "父层引用",
        }
        # 重复同步不重复建档（修复下游重复显示根因）
        self.manifest.sync_from_online_ods()
        assert len(self.manifest.list_entries()) == 3

    def test_empty_manifest_when_no_online_tables(self):
        for row in store.rows("ods_layer"):
            row["status"] = "草稿"
        assert self.manifest.list_entries() == []

    def test_offline_persists_status_and_parent_refs_together(self):
        entry = store.find_by_field("ods_layer", "表编号", "ODS_ORD_001")
        result, message = self.ods.run_action(entry["id"], "标记下线")
        assert result["status"] == "已下线"
        manifest_entry = self.manifest.get_entry("ODS_ORD_001")
        assert manifest_entry["在线状态"] == "已下线"
        assert manifest_entry["阶段"] == "切换"
        refs = manifest_entry["父层引用"]
        assert any(r["明细表编号"] == "DWD_ORD_001" and r["引用状态"] == "已同步下线" for r in refs)

    def test_stages_advance_one_way(self):
        self.manifest.sync_from_online_ods()
        entry = self.manifest.get_entry("ODS_ORD_001")
        assert entry["阶段"] == "归档"
        import pytest
        # 同一阶段重复追加不允许（阶段不能原地/回退）
        with pytest.raises(ValueError):
            self.manifest._append_stage(entry, "归档", "不允许重复归档")
        self.manifest._append_stage(entry, "恢复", "重试恢复")
        store.upsert_manifest_entry("ODS_ORD_001", entry)
        entry = self.manifest.get_entry("ODS_ORD_001")
        self.manifest._append_stage(entry, "切换", "下线切换")
        assert [s["阶段"] for s in entry["阶段历史"]] == ["归档", "恢复", "切换"]
        # 切换后不能再回到归档/恢复
        with pytest.raises(ValueError):
            self.manifest._append_stage(entry, "归档", "不允许回退")

    def test_conflict_offline_first_marks_dwd_waiting(self):
        self.manifest.sync_from_online_ods()
        dwd_entry = store.find_by_field("dwd_layer", "表编号", "DWD_PAY_001")
        self.manifest.begin_dwd_refresh(dwd_entry)
        ods_entry = store.find_by_field("ods_layer", "表编号", "ODS_PAY_001")
        result, _ = self.ods.run_action(ods_entry["id"], "标记下线", POLICY_OFFLINE_FIRST)
        # ODS 立即下线
        assert result["status"] == "已下线"
        manifest_entry = self.manifest.get_entry("ODS_PAY_001")
        # DWD 记录等待父层清单
        assert any(r["引用状态"] == "等待父层清单" for r in manifest_entry["父层引用"])
        assert len(manifest_entry["冲突记录"]) == 1
        self.manifest.finish_dwd_refresh(dwd_entry)

    def test_conflict_refresh_first_offlines_after_refresh(self):
        self.manifest.sync_from_online_ods()
        dwd_entry = store.find_by_field("dwd_layer", "表编号", "DWD_PAY_001")
        self.manifest.begin_dwd_refresh(dwd_entry)
        ods_entry = store.find_by_field("ods_layer", "表编号", "ODS_PAY_001")
        result, _ = self.ods.run_action(ods_entry["id"], "标记下线", POLICY_REFRESH_FIRST)
        # 刷新在途时 ODS 保持在线
        assert result["status"] == "已上线"
        manifest_entry = self.manifest.get_entry("ODS_PAY_001")
        assert manifest_entry["待切换"] is True
        # 刷新完成后 ODS 才变为已下线
        self.manifest.finish_dwd_refresh(dwd_entry)
        assert store.find_by_field("ods_layer", "表名称", "ods_payments")["status"] == "已下线"
        manifest_entry = self.manifest.get_entry("ODS_PAY_001")
        assert manifest_entry["在线状态"] == "已下线" and manifest_entry["阶段"] == "切换"

    def test_interrupt_retry_restores_full_version_without_extra_offline(self):
        self.manifest.sync_from_online_ods()
        ods_entry = store.find_by_field("ods_layer", "表编号", "ODS_USR_001")
        # 中断污染了贴源表状态
        ods_entry["status"] = "待校验"
        self.manifest.simulate_interrupt("ODS_USR_001")
        row, message = self.manifest.retry_after_interrupt("ODS_USR_001")
        assert row is not None
        # 贴源表回到完整版本（仍在线，没有被擅自下线）
        assert store.find_by_field("ods_layer", "表名称", "ods_users")["status"] == "已上线"
        assert [s["阶段"] for s in row["阶段历史"]] == ["归档", "恢复"]
        # 没有中断记录时不能重复重试
        again, _ = self.manifest.retry_after_interrupt("ODS_USR_001")
        assert again is None

    def test_interrupt_retry_continues_unfinished_offline(self):
        self.manifest.sync_from_online_ods()
        # 构造「下线已登记、落库前中断」状态
        row = self.manifest.ensure_entry("ODS_ORD_001")
        row["待完成下线"] = True
        store.upsert_manifest_entry("ODS_ORD_001", row)
        self.manifest.simulate_interrupt("ODS_ORD_001")
        row, message = self.manifest.retry_after_interrupt("ODS_ORD_001")
        assert row["在线状态"] == "已下线"
        assert row["待完成下线"] is False and row["中断中"] is False
        assert store.find_by_field("ods_layer", "表名称", "ods_orders")["status"] == "已下线"
        assert any(r["明细表编号"] == "DWD_ORD_001" for r in row["父层引用"])
        assert [s["阶段"] for s in row["阶段历史"]] == ["归档", "恢复", "切换"]


class TestDwdParentGuard:
    def setup_method(self):
        _reset()
        self.ods = OdsLayerService()
        self.dwd = DwdLayerService()
        self.manifest = OdsManifestService()

    def test_offline_parent_cannot_build_new_dwd(self):
        ods_entry = store.find_by_field("ods_layer", "表编号", "ODS_ORD_001")
        self.ods.run_action(ods_entry["id"], "标记下线")
        entry, errors = self.dwd.create_entry(
            {"表编号": "DWD_NEW", "父层来源": "ODS_ORD_001", "表名称": "dwd_new"})
        assert entry is None
        assert any("已下线" in text for text in errors)

    def test_offline_parent_excluded_from_options(self):
        ods_entry = store.find_by_field("ods_layer", "表编号", "ODS_ORD_001")
        self.ods.run_action(ods_entry["id"], "标记下线")
        codes = {row["表编号"] for row in self.dwd.available_parent_ods()}
        assert "ODS_ORD_001" not in codes
        assert "ODS_PAY_001" in codes

    def test_refresh_rejected_when_parent_offline(self):
        ods_entry = store.find_by_field("ods_layer", "表编号", "ODS_ORD_001")
        self.ods.run_action(ods_entry["id"], "标记下线")
        result = self.dwd.build_dwd_from_ods(1)
        assert result["ok"] is False
        assert "已下线" in result["message"]

    def test_refresh_allowed_when_parent_online(self):
        self.manifest.sync_from_online_ods()
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "order_amount": "100.50", "order_status": "已完成", "dt": "2026-11-01"},
        ])
        result = self.dwd.build_dwd_from_ods(1)
        assert result["ok"] is True

    def test_dwd_status_advances_only_in_order(self):
        entry = store.find("dwd_layer", 3)  # 草稿态 DWD_USR_001
        result, _ = self.dwd.run_action(entry["id"], "标记下线")
        assert result is None
        self.dwd.run_action(entry["id"], "提交校验")
        result, _ = self.dwd.run_action(entry["id"], "标记下线")
        assert result is None
        self.dwd.run_action(entry["id"], "确认上线")
        result, _ = self.dwd.run_action(entry["id"], "标记下线")
        assert result is not None and result["status"] == "已下线"
