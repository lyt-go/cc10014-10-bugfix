"""ODS表版本清单服务：跨模块持久化贴源表在线/下线顺序、阶段与DWD父层引用。

设计要点：
- 清单输入只来自「已上线」贴源表，草稿/待校验表不进清单。
- 清单对外只投影在线与下线、顺序号、阶段、父层引用这些结果字段。
- 阶段（归档→恢复→切换）只允许单向推进，历史只追加，不回写旧阶段。
- 下线状态与 DWD 父层引用在同一调用内落库，避免明细刷新读到不可用表。
- 下线与 DWD 刷新冲突时同时兼容两种结果：
  下线优先（默认）：ODS 立即下线，DWD 记录「等待父层清单」；
  刷新优先：DWD 完成本次刷新后，ODS 状态才切换为已下线。
- 中断重试时贴源表回到完整版本快照，清单只续写未完成部分，已完成阶段不重放。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

ODS_MODULE = "ods_layer"
DWD_MODULE = "dwd_layer"

# 清单对外只暴露这些结果字段：在线/下线、顺序、阶段、父层引用
LIST_FIELDS = ["顺序号", "表编号", "表名称", "来源系统", "在线状态", "阶段", "父层引用"]
# 阶段固定先后，只允许单向推进
STAGE_ORDER = ["归档", "恢复", "切换"]
# 两种冲突处理策略
POLICY_OFFLINE_FIRST = "下线优先"
POLICY_REFRESH_FIRST = "刷新优先"
CONFLICT_POLICIES = [POLICY_OFFLINE_FIRST, POLICY_REFRESH_FIRST]
# 父层引用状态
REF_WAITING = "等待父层清单"
REF_SYNCED = "已同步下线"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class OdsManifestService:
    # ---- 读取 ----

    def list_entries(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """读取清单：先把已上线贴源表同步进清单，再按顺序号返回投影结果。"""
        self.sync_from_online_ods()
        rows = sorted(store.manifest_rows(), key=lambda r: int(r.get("顺序号", 0)))
        if status:
            rows = [row for row in rows if row.get("在线状态") == status]
        return [self._project(row) for row in rows]

    def get_entry(self, ods_code: str) -> dict[str, Any] | None:
        """清单详情：返回完整条目（含阶段历史、冲突记录、完整版本信息）。"""
        self.sync_from_online_ods()
        row = store.get_manifest_entry(ods_code)
        return dict(row) if row else None

    def get_entry_for_dwd(self, dwd_entry_id: int) -> dict[str, Any] | None:
        """供明细层管理详情页跨模块读取：按 DWD 的父层来源定位贴源表清单结果。"""
        dwd = store.find(DWD_MODULE, dwd_entry_id)
        if not dwd:
            return None
        parent_code = str(dwd.get("父层来源", ""))
        row = store.get_manifest_entry(parent_code)
        return self._project(row) if row else None

    # ---- 写入/同步 ----

    def sync_from_online_ods(self) -> int:
        """读取已上线贴源表并登记进清单；已登记的不重复建档（修复下游重复显示的根因）。"""
        created = 0
        for ods in store.rows(ODS_MODULE):
            if ods.get("status") != "已上线":
                continue
            ods_code = str(ods.get("表编号", ""))
            if not ods_code or store.get_manifest_entry(ods_code):
                continue
            self._register(ods)
            created += 1
        return created

    def ensure_entry(self, ods_code: str) -> dict[str, Any] | None:
        """按贴源表编号确保清单条目存在，返回条目；贴源表不存在时返回 None。"""
        ods = store.find_by_field(ODS_MODULE, "表编号", ods_code)
        if not ods:
            return None
        row = store.get_manifest_entry(ods_code)
        if row is None and ods.get("status") == "已上线":
            row = self._register(ods)
        return dict(row) if row else None

    def mark_online(self, ods_entry: dict[str, Any]) -> dict[str, Any] | None:
        """贴源表确认上线后把结果写入清单（输入数据来自已上线贴源表）。"""
        if ods_entry.get("status") != "已上线":
            return None
        ods_code = str(ods_entry.get("表编号", ""))
        existing = store.get_manifest_entry(ods_code)
        if existing:
            return existing
        return self._register(ods_entry)

    # ---- 下线与父层引用（同事务落库） ----

    def offline_with_parent_refs(
        self, ods_entry_id: int, conflict_policy: str = POLICY_OFFLINE_FIRST
    ) -> tuple[dict[str, Any] | None, str]:
        """下线贴源表，并把 DWD 父层引用在同一次调用里落库。

        冲突策略：
        - 下线优先（默认）：ODS 立即下线；正在刷新的 DWD 记录「等待父层清单」。
        - 刷新优先：若有 DWD 刷新在途，ODS 保持在线并登记待切换，刷新完成后再下线。
        """
        ods = store.find(ODS_MODULE, ods_entry_id)
        if ods is None:
            return None, f"贴源表 {ods_entry_id} 不存在或已归档"
        if ods.get("status") == "已下线":
            return None, "贴源表已是已下线状态，不能重复下线"
        if ods.get("status") != "已上线":
            return None, f"贴源表当前为「{ods.get('status')}」，只有已上线表允许标记下线"
        if conflict_policy not in CONFLICT_POLICIES:
            return None, f"冲突策略只能是 {CONFLICT_POLICIES[0]} 或 {CONFLICT_POLICIES[1]}"

        ods_code = str(ods.get("表编号", ""))
        row = self.ensure_entry(ods_code)
        if row is None:
            return None, f"贴源表 {ods_code} 未能写入ODS表版本清单"

        refresh_active = store.is_refresh_active(ods_code)
        self._record_conflict(row, conflict_policy, refresh_active)
        # 标记本次下线尚未完成，异常中断后重试据此只续写未完成部分
        row["待完成下线"] = True
        store.upsert_manifest_entry(ods_code, row)

        if refresh_active and conflict_policy == POLICY_REFRESH_FIRST:
            # 兼容既有清单的另一种处理：先完成 DWD 刷新，再切换 ODS 下线
            row["待切换"] = True
            row["切换策略"] = POLICY_REFRESH_FIRST
            store.upsert_manifest_entry(ods_code, row)
            return row, "检测到DWD刷新在途，按刷新优先策略：本次刷新完成后ODS自动切换为已下线"

        # 默认（下线优先）或无在途刷新：下线状态与父层引用同时落库
        self._apply_offline(ods, row, conflict_policy, refresh_active)
        return row, "贴源表已下线，DWD父层引用已同步写入ODS表版本清单"

    def resolve_pending_switch(self, ods_code: str) -> dict[str, Any] | None:
        """刷新优先策略下，DWD 刷新完成后把待切换的贴源表正式下线。"""
        row = store.get_manifest_entry(ods_code)
        if not row or not row.get("待切换"):
            return None
        ods = store.find_by_field(ODS_MODULE, "表编号", ods_code)
        if ods is None:
            return None
        self._apply_offline(ods, row, POLICY_REFRESH_FIRST, refresh_active=False)
        return store.get_manifest_entry(ods_code)

    # ---- DWD 刷新协同 ----

    def begin_dwd_refresh(self, dwd_entry: dict[str, Any]) -> str:
        """DWD 刷新开始：登记在途刷新，返回父层贴源表编号。"""
        ods_code = str(dwd_entry.get("父层来源", ""))
        store.mark_refresh_active(ods_code, str(dwd_entry.get("表编号", "")))
        return ods_code

    def finish_dwd_refresh(self, dwd_entry: dict[str, Any]) -> dict[str, Any] | None:
        """DWD 刷新结束：解除在途标记，并处理刷新优先策略遗留的待切换下线。"""
        ods_code = str(dwd_entry.get("父层来源", ""))
        dwd_code = str(dwd_entry.get("表编号", ""))
        store.mark_refresh_done(ods_code, dwd_code)
        return self.resolve_pending_switch(ods_code)

    def assert_parent_available(self, parent_code: str) -> str | None:
        """DWD 刷新/构建前校验父层贴源表可用；已下线则返回错误说明。"""
        ods = store.find_by_field(ODS_MODULE, "表编号", parent_code)
        if ods is None:
            return f"父层来源 {parent_code} 不存在"
        if ods.get("status") == "已下线":
            return f"父层贴源表 {parent_code} 已下线，明细刷新会读到不可用表，本次刷新已拒绝"
        return None

    # ---- 中断与重试 ----

    def simulate_interrupt(self, ods_code: str) -> tuple[dict[str, Any] | None, str]:
        """模拟清单写入过程中的异常中断：标记中断态，保留已完成阶段与完整版本。"""
        row = store.get_manifest_entry(ods_code)
        if row is None:
            return None, f"ODS表版本清单中不存在贴源表 {ods_code}"
        if row.get("阶段") == "切换":
            return None, "清单已完成切换，不存在未完成部分可中断"
        row["中断中"] = True
        store.upsert_manifest_entry(ods_code, row)
        return row, "清单写入异常中断，已保留完整版本与已完成阶段"

    def retry_after_interrupt(self, ods_code: str) -> tuple[dict[str, Any] | None, str]:
        """重试：贴源表回到完整版本，清单只续写未完成部分，已完成阶段不重放。"""
        row = store.get_manifest_entry(ods_code)
        if row is None:
            return None, f"ODS表版本清单中不存在贴源表 {ods_code}"
        if not row.get("中断中"):
            return None, "该贴源表没有待重试的中断记录"

        # 1. 贴源表回到完整版本快照
        ods = store.find_by_field(ODS_MODULE, "表编号", ods_code)
        snapshot = row.get("完整版本") or {}
        if ods is not None:
            for key, value in snapshot.items():
                ods[key] = value

        # 2. 恢复阶段只追加一次（单向推进，重复重试不会重复写）
        if row.get("阶段") != "恢复" and row.get("阶段") != "切换":
            self._append_stage(row, "恢复", "异常中断后重试：贴源表已回到完整版本")
        row["中断中"] = False

        # 3. 只续写未完成部分：
        #    刷新优先的待切换任务继续等待刷新结束；已中断的下线流程补做；其余仅恢复版本
        if row.get("待切换"):
            note = "重试完成，等待DWD本次刷新结束后再切换下线"
            store.upsert_manifest_entry(ods_code, row)
        elif row.get("待完成下线") and row.get("在线状态") != "已下线" and not store.is_refresh_active(ods_code):
            if ods is not None:
                self._apply_offline(ods, row, row.get("切换策略", POLICY_OFFLINE_FIRST), False)
                return store.get_manifest_entry(ods_code), "重试成功：已续写未完成的下线与父层引用"
            note = "重试完成，贴源表已恢复，未完成部分待下次下线续写"
        else:
            note = "重试完成，贴源表已回到完整版本，无未完成的下线需要续写"
            store.upsert_manifest_entry(ods_code, row)

        return store.get_manifest_entry(ods_code), note

    # ---- 内部方法 ----

    def _register(self, ods_entry: dict[str, Any]) -> dict[str, Any]:
        ods_code = str(ods_entry.get("表编号", ""))
        seq = store.next_manifest_seq()
        row = {
            "id": seq,
            "顺序号": seq,
            "表编号": ods_code,
            "表名称": ods_entry.get("表名称", ""),
            "来源系统": ods_entry.get("来源系统", ""),
            "在线状态": "在线",
            "阶段": "归档",
            "阶段历史": [],
            "父层引用": [],
            "冲突记录": [],
            "待切换": False,
            "待完成下线": False,
            "中断中": False,
            "创建时间": _now(),
            "完整版本": self._snapshot(ods_entry),
        }
        # 建档时归档是初始阶段，直接写入；后续阶段只能经 _append_stage 单向推进
        row["阶段历史"].append({"阶段": "归档", "时间": _now(), "说明": "已上线贴源表写入清单，归档当前完整版本"})
        store.upsert_manifest_entry(ods_code, row)
        return row

    def _apply_offline(
        self,
        ods: dict[str, Any],
        row: dict[str, Any],
        conflict_policy: str,
        refresh_active: bool,
    ) -> None:
        """同一次调用内完成：ODS 状态切换 + DWD 父层引用落库 + 清单阶段推进。"""
        ods_code = str(ods.get("表编号", ""))
        # 1. ODS 立即下线
        ods["status"] = "已下线"
        ods["pending"] = False
        store.save_version(ODS_MODULE, int(ods["id"]), ods)
        # 2. DWD 父层引用同时落库（在途刷新记等待，其余记已同步）
        refs = self._collect_parent_refs(ods_code, refresh_active)
        row["父层引用"] = refs
        # 3. 清单结果推进到切换（单向）
        row["在线状态"] = "已下线"
        row["待切换"] = False
        row["待完成下线"] = False
        row["切换策略"] = conflict_policy
        self._append_stage(row, "切换", f"按{conflict_policy}策略完成下线切换，父层引用同时落库")
        store.upsert_manifest_entry(ods_code, row)

    def _collect_parent_refs(self, ods_code: str, refresh_active: bool) -> list[dict[str, Any]]:
        waiting = set(store.active_refreshes(ods_code)) if refresh_active else set()
        refs: list[dict[str, Any]] = []
        for dwd in store.rows(DWD_MODULE):
            if str(dwd.get("父层来源", "")) != ods_code:
                continue
            dwd_code = str(dwd.get("表编号", ""))
            refs.append({
                "明细表编号": dwd.get("表编号", ""),
                "明细表名称": dwd_code,
                "引用状态": REF_WAITING if dwd_code in waiting else REF_SYNCED,
                "明细表状态": dwd.get("status", ""),
                "记录时间": _now(),
            })
        return refs

    def _record_conflict(self, row: dict[str, Any], policy: str, refresh_active: bool) -> None:
        if not refresh_active:
            return
        row.setdefault("冲突记录", []).append({
            "时间": _now(),
            "采用策略": policy,
            "结果一_下线优先": "ODS立即下线，DWD记录等待父层清单",
            "结果二_刷新优先": "DWD完成一次刷新后ODS状态变为已下线",
            "当前在途刷新": store.active_refreshes(str(row.get("表编号", ""))),
        })

    def _append_stage(self, row: dict[str, Any], stage: str, note: str) -> None:
        """阶段单向推进：只允许向 STAGE_ORDER 更后的位置移动，同级重复与回退都拒绝。"""
        if stage not in STAGE_ORDER:
            raise ValueError(f"未知清单阶段：{stage}")
        current = row.get("阶段")
        if current in STAGE_ORDER and STAGE_ORDER.index(stage) <= STAGE_ORDER.index(current):
            raise ValueError(f"清单阶段只能单向推进：{current} -> {stage} 不被允许")
        row["阶段"] = stage
        row.setdefault("阶段历史", []).append({"阶段": stage, "时间": _now(), "说明": note})

    def _snapshot(self, ods_entry: dict[str, Any]) -> dict[str, Any]:
        """完整版本快照：中断重试时据此恢复贴源表。"""
        return {key: value for key, value in ods_entry.items()}

    def _project(self, row: dict[str, Any]) -> dict[str, Any]:
        """清单只保留在线与下线顺序及父层引用数据结果。"""
        refs = row.get("父层引用", [])
        return {
            "顺序号": row.get("顺序号"),
            "表编号": row.get("表编号", ""),
            "表名称": row.get("表名称", ""),
            "来源系统": row.get("来源系统", ""),
            "在线状态": row.get("在线状态", ""),
            "阶段": row.get("阶段", ""),
            "待切换": row.get("待切换", False),
            "中断中": row.get("中断中", False),
            "父层引用": [
                {"明细表编号": r.get("明细表编号", ""), "明细表名称": r.get("明细表名称", ""),
                 "引用状态": r.get("引用状态", "")}
                for r in refs
            ],
        }


manifest_service = OdsManifestService()
