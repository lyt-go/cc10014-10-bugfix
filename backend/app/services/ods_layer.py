"""贴源层管理业务规则：支持从入库批次创建表结构，存储模拟表数据，并可被下游DWD消费。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.ods_manifest import POLICY_OFFLINE_FIRST, manifest_service

MODULE = "ods_layer"
LIST_FIELDS = ["表编号", "来源系统", "表名称", "分区字段", "存储格式", "保留天数", "压缩算法", "创建批次"]
REQUIRED_FIELDS = ["表编号", "来源系统", "表名称"]
STATUS_ORDER = ["草稿", "待校验", "已上线", "已下线"]
# 每个动作只允许从紧邻的上一个状态进入，状态只能按顺序单向推进
ACTION_RULES = {"提交校验": "待校验", "确认上线": "已上线", "标记下线": "已下线"}
NEGATIVE_ACTIONS = []


class OdsLayerService:
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

    def get_by_table_name(self, table_name: str) -> dict[str, Any] | None:
        return store.find_by_field(MODULE, "表名称", table_name)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        if values.get("字段列表"):
            entry["字段列表"] = values["字段列表"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str, conflict_policy: str = POLICY_OFFLINE_FIRST) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"贴源表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于贴源层管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 状态只能按 草稿→待校验→已上线→已下线 的顺序单向推进，禁止跳态/回退
        current = entry.get("status", STATUS_ORDER[0])
        if current not in STATUS_ORDER:
            return None, f"当前状态「{current}」不在允许的状态序列里"
        expected_current = STATUS_ORDER[STATUS_ORDER.index(target) - 1]
        if current != expected_current:
            if current == target:
                return None, f"贴源表已是「{target}」状态，不能重复{action}"
            return None, f"状态流转错误：贴源表当前为「{current}」，需先处于「{expected_current}」才能{action}，不能直接变更为「{target}」"

        # 下线动作：下线状态与 DWD 父层引用必须同时落库，交给清单服务统一处理
        if action == "标记下线":
            row, message = manifest_service.offline_with_parent_refs(entry_id, conflict_policy)
            if row is None:
                return None, message
            # 刷新优先且刷新在途时贴源表保持在线，清单登记待切换
            refreshed = store.find(MODULE, entry_id)
            return refreshed, message

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        # 确认上线后把已上线贴源表写入 ODS 表版本清单
        if action == "确认上线":
            manifest_service.mark_online(entry)
        return entry, f"贴源表已{action}"

    def retry_offline(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """「先完成当前刷新再提交下线」重试：中断后贴源表回到完整版本，清单只续写未完成部分。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"贴源表 {entry_id} 不存在或已归档"
        ods_code = str(entry.get("表编号", ""))
        row, message = manifest_service.retry_after_interrupt(ods_code)
        if row is None:
            return None, message
        return store.find(MODULE, entry_id), message

    def simulate_interrupt(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """模拟清单写入异常中断（测试/演练用）。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"贴源表 {entry_id} 不存在或已归档"
        return manifest_service.simulate_interrupt(str(entry.get("表编号", "")))

    def get_table_data(self, table_name: str) -> list[dict[str, Any]]:
        return store.table_rows(table_name)

    def get_batches_for_table(self, table_name: str) -> list[dict[str, Any]]:
        ods = self.get_by_table_name(table_name)
        if not ods:
            return []
        batch_no = ods.get("创建批次")
        if not batch_no:
            return []
        from app.services.raw_ingest import RawIngestService
        ri = RawIngestService()
        batch = ri.get_by_batch_no(batch_no)
        return [batch] if batch else []
