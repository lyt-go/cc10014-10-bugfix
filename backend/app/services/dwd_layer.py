"""明细层管理业务规则：消费ODS层数据，应用清洗+去重，生成DWD明细表，版本化管理。"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import store
from app.services.ods_manifest import manifest_service

MODULE = "dwd_layer"
LIST_FIELDS = ["表编号", "父层来源", "表名称", "粒度说明", "分区字段", "关联键列", "刷新水位", "创建批次"]
REQUIRED_FIELDS = ["表编号", "父层来源", "表名称"]
STATUS_ORDER = ["草稿", "待校验", "已上线", "已下线"]
ACTION_RULES = {"提交校验": "待校验", "确认上线": "已上线", "标记下线": "已下线"}
NEGATIVE_ACTIONS = []


class DwdLayerService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("表编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def available_parent_ods(self) -> list[dict[str, Any]]:
        """新明细构建的可选父层：只返回在线贴源表，已下线表不再出现。"""
        return [dict(row) for row in store.rows("ods_layer") if row.get("status") != "已下线"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        # 已下线贴源表不能作为新明细的父层；在线父层允许按表编号或表名称关联
        parent_code = str(values.get("父层来源", ""))
        parent = None
        for row in store.rows("ods_layer"):
            if row.get("表编号") == parent_code or row.get("表名称") == parent_code:
                parent = row
                break
        if parent is not None and parent.get("status") == "已下线":
            return None, [f"父层贴源表 {parent_code} 已下线，不能用于新明细构建"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        if values.get("清洗策略"):
            entry["清洗策略"] = values["清洗策略"]
        if values.get("去重配置"):
            entry["去重配置"] = values["去重配置"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"明细表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于明细层管理可执行范围"
        target = ACTION_RULES[action]
        # 状态只能按 草稿→待校验→已上线→已下线 的顺序单向推进，禁止跳态/回退
        current = entry.get("status", STATUS_ORDER[0])
        if current not in STATUS_ORDER:
            return None, f"当前状态「{current}」不在允许的状态序列里"
        expected_current = STATUS_ORDER[STATUS_ORDER.index(target) - 1]
        if current != expected_current:
            if current == target:
                return None, f"明细表已是「{target}」状态，不能重复{action}"
            return None, f"状态流转错误：明细表当前为「{current}」，需先处于「{expected_current}」才能{action}，不能直接变更为「{target}」"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"明细表已{action}"

    def build_dwd_from_ods(self, entry_id: int) -> dict[str, Any]:
        """从关联的ODS表读取数据，应用清洗+去重，写入DWD模拟表。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "明细表不存在"}
        # 找到父层ODS表
        parent_code = entry.get("父层来源", "")
        ods_entry = None
        for row in store.rows("ods_layer"):
            if row.get("表编号") == parent_code or row.get("表名称") == parent_code:
                ods_entry = row
                break
        if not ods_entry:
            return {"ok": False, "message": f"父层来源 {parent_code} 不存在"}
        # 已下线贴源表不可用，明细刷新直接拒绝，避免读到不可用表
        parent_error = manifest_service.assert_parent_available(str(ods_entry.get("表编号", "")))
        if parent_error:
            return {"ok": False, "message": parent_error}
        # 登记本次 DWD 刷新在途，供下线请求做冲突判定；结束后解除并处理待切换
        manifest_service.begin_dwd_refresh(entry)
        try:
            result = self._do_build(entry, ods_entry)
        finally:
            manifest_service.finish_dwd_refresh(entry)
        return result

    def _do_build(self, entry: dict[str, Any], ods_entry: dict[str, Any]) -> dict[str, Any]:
        ods_table = ods_entry.get("表名称", "")
        ods_data = store.table_rows(ods_table)
        if not ods_data:
            # 如果ODS没有数据，用seed数据生成模拟数据
            ods_data = self._mock_ods_data(ods_table)
            store.set_table_rows(ods_table, ods_data)
        # 应用清洗
        cleanse_id = entry.get("清洗策略", "")
        if cleanse_id:
            from app.services.cleanse_rule import CleanseRuleService
            cs = CleanseRuleService()
            c_entry = store.find("cleanse_rule", self._resolve_id("cleanse_rule", cleanse_id))
            if c_entry:
                ods_data = cs.apply_cleanse(c_entry["id"], ods_data)
        # 应用去重
        dedup_id = entry.get("去重配置", "")
        if dedup_id:
            from app.services.dedup_config import DedupConfigService
            ds = DedupConfigService()
            d_entry = store.find("dedup_config", self._resolve_id("dedup_config", dedup_id))
            if d_entry:
                ods_data = ds.apply_dedup(d_entry["id"], ods_data)
        # 写入DWD模拟表
        dwd_table = entry.get("表名称", "")
        store.set_table_rows(dwd_table, ods_data)
        entry["刷新水位"] = date.today().isoformat()
        store.save_version(MODULE, entry["id"], entry)
        # 注册血缘
        store.add_lineage(ods_table, dwd_table, ods_entry.get("字段列表", ""),
                          transform_logic="清洗+去重", batch_id=entry.get("创建批次", ""))
        return {"ok": True, "message": f"DWD表 {dwd_table} 已生成，共{len(ods_data)}行"}

    def _resolve_id(self, module: str, code: str) -> int:
        for row in store.rows(module):
            if row.get("策略编号") == code or row.get("配置编号") == code:
                return int(row["id"])
        return 0

    def _mock_ods_data(self, table_name: str) -> list[dict[str, Any]]:
        if "order" in table_name:
            return [
                {"order_id": f"O{i:04d}", "order_amount": f"{i*10.5:.2f}", "order_status": "已完成" if i % 2 else "待支付", "dt": "2026-11-01"}
                for i in range(1, 6)
            ]
        elif "pay" in table_name:
            return [
                {"pay_id": f"P{i:04d}", "pay_amount": f"{i*8.3:.2f}", "pay_time": "2026-11-01T10:00:00", "dt": "2026-11-01"}
                for i in range(1, 6)
            ]
        elif "user" in table_name:
            return [
                {"user_id": f"U{i:04d}", "register_time": "2026-01-01T00:00:00", "user_level": "普通" if i % 2 else "VIP", "dt": "2026-11-01"}
                for i in range(1, 6)
            ]
        return [{"_id": i, "dt": "2026-11-01"} for i in range(1, 6)]

    def get_table_data(self, table_name: str) -> list[dict[str, Any]]:
        return store.table_rows(table_name)
