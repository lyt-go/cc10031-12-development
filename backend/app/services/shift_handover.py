"""班次交接领域服务：让交班双方在边缘终端上核对实收，并形成可复核的交接表。

交接表围绕原始采样时刻工作：

* 按班界（默认 08:00）把采样点归入旧班 / 新班，据此计算班次实收和缺点；
* 同一采样时刻可能先收到终端原件，随后又收到校准缓存，两份证据都原样留存，
  每次实收只采用其中一个观测，绝不覆盖已留的原值；
* 实际未齐的区间交给采集心跳汇总做常规缺点登记，连续超温区段单独派发
  值守复采，即使这些采样时刻已经齐全；
* 权限按角色收口：旧班负责人只能确认旧班，接班人只能确认新班，计量负责人
  只能签认校准缓存，观察者全程只读。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

SHIFT_BOUNDARY_HOUR = 8
DEFAULT_SAMPLE_INTERVAL_SECONDS = 60
DEFAULT_HIGH_TEMP_THRESHOLD = 81.5
DEFAULT_HIGH_TEMP_CONSECUTIVE = 3
OVERLAP_CHECK_MINUTES = 5

OLD_SHIFT = "旧班"
NEW_SHIFT = "新班"

ROLE_OUTGOING = "outgoing"
ROLE_INCOMING = "incoming"
ROLE_METROLOGY = "metrology"
ROLE_OBSERVER = "observer"

ROLE_LABELS = {
    ROLE_OUTGOING: "旧班负责人",
    ROLE_INCOMING: "接班人",
    ROLE_METROLOGY: "计量负责人",
    ROLE_OBSERVER: "观察者",
}
ROLE_ALIASES = {
    "old": ROLE_OUTGOING,
    "old_shift": ROLE_OUTGOING,
    "旧班": ROLE_OUTGOING,
    "旧班负责人": ROLE_OUTGOING,
    "交班人": ROLE_OUTGOING,
    "new": ROLE_INCOMING,
    "incoming_shift": ROLE_INCOMING,
    "新班": ROLE_INCOMING,
    "接班人": ROLE_INCOMING,
    "接班负责人": ROLE_INCOMING,
    "计量": ROLE_METROLOGY,
    "计量负责人": ROLE_METROLOGY,
    "viewer": ROLE_OBSERVER,
    "readonly": ROLE_OBSERVER,
    "观察者": ROLE_OBSERVER,
    "只读": ROLE_OBSERVER,
}

SOURCE_TERMINAL = "终端原件"
SOURCE_CACHE = "校准缓存"
SOURCE_REPLAY = "补采"
EVIDENCE_SOURCES = (SOURCE_TERMINAL, SOURCE_CACHE, SOURCE_REPLAY)

EVIDENCE_PENDING = "待核对"
EVIDENCE_CONFIRMED = "已确认"
EVIDENCE_SIGNED = "已签认"


def normalize_role(role: str | None) -> str:
    text = str(role or "").strip()
    return ROLE_ALIASES.get(text, text or ROLE_OBSERVER)


def parse_moment(value: Any) -> datetime:
    """解析采样时刻，统一成本地无时区时间。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        raise ValueError("采样时刻不能为空")
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    return parsed.replace(tzinfo=None)


def format_moment(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _as_number(value: Any, default: Any, caster: type) -> Any:
    try:
        return caster(value) if str(value or "").strip() else caster(default)
    except (TypeError, ValueError):
        return caster(default)


class ShiftHandoverError(Exception):
    """业务规则拒绝：携带可直接回给前端的说明。"""


class ShiftHandoverService(BaseModuleService):
    MODULE = "shift_handover"
    ENTITY = "交接记录"
    OPERATION_TERM = "核对"
    LIST_FIELDS = [
        "交接编号", "测点编号", "采集终端", "班次日期", "班界时刻",
        "旧班实收", "新班实收", "旧班缺点", "新班缺点", "采用温度",
    ]
    REQUIRED_FIELDS = ["交接编号", "测点编号", "采集终端"]
    STATUS_ORDER = ["草稿", "核对中", "待签发", "已签发", "已取消"]
    ACTION_RULES = {
        "启动交接": "核对中",
        "确认旧班": "待签发",
        "确认新班": "待签发",
        "签发交接": "已签发",
        "取消交接": "已取消",
    }
    NEGATIVE_ACTIONS = ["取消交接"]
    ROLE_AWARE = True

    # ------------------------------------------------------------------
    # 登记与查询
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        shift_date = str(values.get("班次日期") or format_moment(datetime.now())[:10])
        entry.update({
            "班次日期": shift_date,
            "班界时刻": str(values.get("班界时刻") or f"{shift_date}T08:00:00"),
            "旧班实收": 0,
            "新班实收": 0,
            "旧班缺点": 0,
            "新班缺点": 0,
            "采用温度": "—",
            "采样间隔秒": _as_number(values.get("采样间隔秒"), DEFAULT_SAMPLE_INTERVAL_SECONDS, int),
            "高温阈值": _as_number(values.get("高温阈值"), DEFAULT_HIGH_TEMP_THRESHOLD, float),
            "高温连续次数": _as_number(values.get("高温连续次数"), DEFAULT_HIGH_TEMP_CONSECUTIVE, int),
            "班次窗口": values.get("班次窗口") or {},
            "原始采样": values.get("原始采样") or [],
            "证据": [],
            "确认记录": {},
            "补采区段": [],
            "审计日志": [],
            "是否签发": False,
            "启动时缺点": 0,
        })
        return entry, []

    def sheet(self, handover_id: int, role: str | None = None) -> dict[str, Any]:
        """打开交接记录：重新按证据核对实收、缺点、高温区段与补采缺口。"""
        stored = self._require(handover_id)
        actor = normalize_role(role)
        # 观察者只读：在副本上重算视图，完全不改动交接记录
        entry = copy.deepcopy(stored) if actor == ROLE_OBSERVER else stored
        reconciliation = self._refresh_entry(entry)
        reconciliation["补采区段"] = self._replay_view(entry, reconciliation["缺点区段"])
        reconciliation["仍缺点数"] = sum(
            int(item["仍缺点数"]) for item in reconciliation["补采区段"])
        return {
            "handover": copy.deepcopy(entry),
            "role": actor,
            "role_label": ROLE_LABELS.get(actor, actor),
            "permissions": self._permissions(actor, entry),
            "reconciliation": reconciliation,
            "audit_log": copy.deepcopy(entry.get("审计日志", [])),
        }

    # ------------------------------------------------------------------
    # 证据登记与计量签认
    # ------------------------------------------------------------------
    def submit_evidence(
        self,
        handover_id: int,
        *,
        moment: Any,
        value: Any,
        source_type: str,
        role: str | None = None,
        submitter: str | None = None,
    ) -> dict[str, Any]:
        entry = self._require(handover_id)
        actor = normalize_role(role)
        self._ensure_editable(entry, actor)
        if actor == ROLE_OBSERVER:
            raise ShiftHandoverError("观察者只读，不能提交或改动交接证据")
        if actor == ROLE_METROLOGY:
            raise ShiftHandoverError("计量负责人只做签认，不能以提交方式改动证据")
        if actor not in (ROLE_OUTGOING, ROLE_INCOMING):
            raise ShiftHandoverError(f"角色 {actor} 无权提交交接证据")
        if source_type not in EVIDENCE_SOURCES:
            raise ShiftHandoverError(f"证据来源「{source_type}」不在允许范围内")
        try:
            sample_moment = parse_moment(moment)
        except ValueError as exc:
            raise ShiftHandoverError(str(exc)) from exc
        try:
            numeric_value = round(float(value), 3)
        except (TypeError, ValueError) as exc:
            raise ShiftHandoverError("证据数值必须是数字") from exc

        shift = self._shift_of(entry, sample_moment)
        expected = set(self._expected_moments(entry))
        if sample_moment not in expected:
            raise ShiftHandoverError("采样时刻不在本班次核对窗口内")
        if source_type == SOURCE_TERMINAL:
            if actor == ROLE_OUTGOING and shift != OLD_SHIFT:
                raise ShiftHandoverError("旧班负责人只能提交旧班采样时刻的终端原件")
            if actor == ROLE_INCOMING and shift != NEW_SHIFT:
                raise ShiftHandoverError("接班人只能提交新班采样时刻的终端原件")
        if source_type == SOURCE_CACHE and actor != ROLE_INCOMING:
            raise ShiftHandoverError("校准缓存由接班人提交，计量负责人另行签认")
        if source_type == SOURCE_CACHE and not self._in_overlap(entry, sample_moment):
            raise ShiftHandoverError("校准缓存只允许落在班界交叠段内")
        if source_type == SOURCE_REPLAY and sample_moment not in self._missing_moments(entry):
            raise ShiftHandoverError("补采证据只能补在仍然缺失的采样时刻上")

        evidence_list = entry.setdefault("证据", [])
        for item in evidence_list:
            if (
                parse_moment(item["采样时刻"]) == sample_moment
                and item["来源类型"] == source_type
                and round(float(item["数值"]), 3) == numeric_value
            ):
                return {
                    "handover": copy.deepcopy(entry),
                    "evidence": copy.deepcopy(item),
                    "duplicate": True,
                    "message": "同一时刻、同一来源的相同数值已存在，未重复登记",
                }

        sequence = len(evidence_list) + 1
        evidence = {
            "证据编号": f"EV-{entry['交接编号']}-{sequence:02d}",
            "采样时刻": format_moment(sample_moment),
            "数值": numeric_value,
            "来源类型": source_type,
            "归属班次": shift,
            "提交角色": actor,
            "提交人": submitter or ROLE_LABELS.get(actor, actor),
            "提交时刻": format_moment(datetime.now()),
            "接收顺序": sequence,
            "状态": EVIDENCE_PENDING,
            "计量签认": None,
        }
        evidence_list.append(evidence)
        if source_type == SOURCE_REPLAY:
            self._mark_replay_evidence(entry, sample_moment)
        reconciliation = self._refresh_entry(entry)
        self._audit(entry, actor, "提交证据", {
            "证据编号": evidence["证据编号"],
            "采样时刻": evidence["采样时刻"],
            "数值": evidence["数值"],
            "来源类型": evidence["来源类型"],
            "归属班次": evidence["归属班次"],
        })
        return {
            "handover": copy.deepcopy(entry),
            "evidence": copy.deepcopy(evidence),
            "duplicate": False,
            "reconciliation": reconciliation,
            "message": "证据已登记，原值继续保留，是否采用由核对规则决定",
        }

    def sign_off(
        self,
        handover_id: int,
        *,
        evidence_id: str,
        shift: str,
        role: str | None = None,
        signer: str | None = None,
    ) -> dict[str, Any]:
        entry = self._require(handover_id)
        actor = normalize_role(role)
        self._ensure_editable(entry, actor)
        if actor != ROLE_METROLOGY:
            raise ShiftHandoverError("只有计量负责人可以签认校准缓存")
        if shift not in (OLD_SHIFT, NEW_SHIFT):
            raise ShiftHandoverError("签认班次只能是旧班或新班")
        if actor == ROLE_OBSERVER:
            raise ShiftHandoverError("观察者只读，不能签认")
        target = next(
            (item for item in entry.get("证据", []) if item["证据编号"] == evidence_id),
            None,
        )
        if target is None:
            raise ShiftHandoverError(f"证据 {evidence_id} 不存在")
        if target["来源类型"] != SOURCE_CACHE:
            raise ShiftHandoverError("计量签认只针对校准缓存，终端原件无需签认")
        if target["归属班次"] != shift:
            raise ShiftHandoverError(
                f"证据归属{target['归属班次']}，不能签认为{shift}实收依据"
            )
        target["计量签认"] = {
            "签认人": signer or ROLE_LABELS[ROLE_METROLOGY],
            "签认角色": actor,
            "签认班次": shift,
            "签认时刻": format_moment(datetime.now()),
        }
        target["状态"] = EVIDENCE_SIGNED
        reconciliation = self._refresh_entry(entry)
        self._audit(entry, actor, "签认校准缓存", {
            "证据编号": evidence_id,
            "签认班次": shift,
            "数值": target["数值"],
        })
        return {
            "handover": copy.deepcopy(entry),
            "evidence": copy.deepcopy(target),
            "reconciliation": reconciliation,
            "message": f"已签认该缓存值可作为{shift}实收依据，原始终端值继续留档",
        }

    # ------------------------------------------------------------------
    # 动作（带角色）
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str, role: str | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"交接记录 {entry_id} 不存在或已归档"
        actor = normalize_role(role)
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        if actor not in ROLE_LABELS:
            return None, f"未登记的角色「{actor}」不能操作交接表"
        if actor == ROLE_OBSERVER:
            return None, "观察者只读，不能发起或确认交接"
        try:
            if action == "启动交接":
                self._start(entry, actor)
            elif action == "确认旧班":
                self._confirm(entry, actor, OLD_SHIFT)
            elif action == "确认新班":
                self._confirm(entry, actor, NEW_SHIFT)
            elif action == "签发交接":
                self._issue(entry, actor)
            elif action == "取消交接":
                self._cancel(entry, actor)
            else:
                return None, f"动作「{action}」尚未实现"
        except ShiftHandoverError as exc:
            return None, str(exc)
        return entry, f"{self.ENTITY}已{action}"

    # ------------------------------------------------------------------
    # 心跳汇总
    # ------------------------------------------------------------------
    def heartbeat_summary(self, handover_id: int, role: str | None = None) -> dict[str, Any]:
        """汇总班次实际落盘点，输出常规缺点、高温复采与补采缺口。

        常规缺点只统计实际未齐的采样时刻；高温复采按连续超温区段单独派发，
        不受采样时刻是否齐全影响。
        """
        stored = self._require(handover_id)
        actor = normalize_role(role)
        entry = copy.deepcopy(stored) if actor == ROLE_OBSERVER else stored
        reconciliation = self._refresh_entry(entry)
        missing_ranges = reconciliation["缺点区段"]
        live_ranges = self._replay_view(entry, missing_ranges)
        adopted = reconciliation["采用观测"]

        normal_defects = {
            "合计缺点": reconciliation["缺点合计"],
            "旧班缺点": reconciliation["旧班缺点"],
            "新班缺点": reconciliation["新班缺点"],
            "区段": missing_ranges,
        }
        hot_ranges = reconciliation["高温复采区段"]
        replay = {
            "区段": live_ranges,
            "仍缺区段": [item for item in live_ranges if item["是否仍缺"]],
            "仍缺点数": sum(int(item["仍缺点数"]) for item in live_ranges),
        }
        summary = {
            "汇总编号": f"HB-{entry['交接编号']}",
            "交接编号": entry["交接编号"],
            "采集终端": entry["采集终端"],
            "测点编号": entry["测点编号"],
            "班次日期": entry["班次日期"],
            "班界时刻": entry["班界时刻"],
            "关联落盘点": {
                "旧班": reconciliation["旧班实收"],
                "新班": reconciliation["新班实收"],
                "合计": reconciliation["旧班实收"] + reconciliation["新班实收"],
                "采样时刻": [item["采样时刻"] for item in adopted],
            },
            "常规缺点": normal_defects,
            "高温复采区段": copy.deepcopy(hot_ranges),
            "补采区段": replay,
            "交接状态": entry["status"],
            "是否签发": bool(entry.get("是否签发")),
            "生成角色": actor,
            "生成时刻": format_moment(datetime.now()),
        }
        if actor != ROLE_OBSERVER:
            entry["最近心跳汇总"] = copy.deepcopy(summary)
            self._audit(entry, actor, "生成心跳汇总", {
                "常规缺点": normal_defects["合计缺点"],
                "高温复采区段数": len(hot_ranges),
                "补采仍缺点数": replay["仍缺点数"],
            })
        return summary

    def record_replay(
        self,
        handover_id: int,
        *,
        samples: list[dict[str, Any]],
        role: str | None = None,
        submitter: str | None = None,
    ) -> dict[str, Any]:
        """登记补采数据：只允许补仍然缺失的时刻，补后仍缺的区段继续留给心跳汇总。"""
        entry = self._require(handover_id)
        actor = normalize_role(role)
        self._ensure_editable(entry, actor)
        if actor not in (ROLE_OUTGOING, ROLE_INCOMING):
            raise ShiftHandoverError("只有交班双方可以登记补采数据")
        accepted, rejected = [], []
        for sample in samples:
            try:
                result = self.submit_evidence(
                    handover_id,
                    moment=sample.get("采样时刻"),
                    value=sample.get("数值"),
                    source_type=SOURCE_REPLAY,
                    role=actor,
                    submitter=submitter,
                )
                accepted.append(result["evidence"]["证据编号"])
            except ShiftHandoverError as exc:
                rejected.append({"采样时刻": sample.get("采样时刻"), "原因": str(exc)})
        reconciliation = self._refresh_entry(entry)
        live_ranges = self._sync_replay_ranges(entry, reconciliation["缺点区段"], actor)
        return {
            "accepted": accepted,
            "rejected": rejected,
            "reconciliation": reconciliation,
            "补采区段": live_ranges,
        }

    # ------------------------------------------------------------------
    # 内部：核对与统计
    # ------------------------------------------------------------------
    def _require(self, handover_id: int) -> dict[str, Any]:
        entry = store.find(self.MODULE, handover_id)
        if entry is None:
            raise ShiftHandoverError(f"交接记录 {handover_id} 不存在或已归档")
        return entry

    def _ensure_editable(self, entry: dict[str, Any], actor: str) -> None:
        if entry.get("status") in ("已签发", "已取消"):
            raise ShiftHandoverError(f"交接已{entry['status'][1:]}，证据与确认记录已冻结")
        if normalize_role(actor) not in ROLE_LABELS:
            raise ShiftHandoverError(f"未登记的角色「{actor}」不能改动交接证据")

    def _boundary(self, entry: dict[str, Any]) -> datetime:
        raw = entry.get("班界时刻")
        if raw:
            return parse_moment(raw)
        day = parse_moment(f"{entry['班次日期']}T00:00:00")
        return day.replace(hour=SHIFT_BOUNDARY_HOUR)

    def _window(self, entry: dict[str, Any]) -> tuple[datetime, datetime]:
        boundary = self._boundary(entry)
        window = entry.get("班次窗口") or {}
        start = parse_moment(window.get("旧班起点")) if window.get("旧班起点") else boundary - timedelta(minutes=15)
        end = parse_moment(window.get("新班终点")) if window.get("新班终点") else boundary + timedelta(minutes=15)
        return start, end

    def _interval(self, entry: dict[str, Any]) -> int:
        return max(int(entry.get("采样间隔秒") or DEFAULT_SAMPLE_INTERVAL_SECONDS), 1)

    def _expected_moments(self, entry: dict[str, Any]) -> list[datetime]:
        start, end = self._window(entry)
        step = timedelta(seconds=self._interval(entry))
        moments, current = [], start
        while current < end:
            moments.append(current)
            current += step
        return moments

    def _shift_of(self, entry: dict[str, Any], moment: datetime) -> str:
        return OLD_SHIFT if moment < self._boundary(entry) else NEW_SHIFT

    def _in_overlap(self, entry: dict[str, Any], moment: datetime) -> bool:
        boundary = self._boundary(entry)
        return boundary - timedelta(minutes=OVERLAP_CHECK_MINUTES) <= moment <= boundary + timedelta(minutes=OVERLAP_CHECK_MINUTES)

    def _missing_moments(self, entry: dict[str, Any]) -> list[datetime]:
        observed = {
            parse_moment(item["采样时刻"])
            for item in self._adopt(entry)
            if item.get("采用证据")
        }
        return [moment for moment in self._expected_moments(entry) if moment not in observed]

    def _adopt(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """按采样时刻归并证据：每个时刻只采用一个观测，所有证据原样保留。"""
        groups: dict[datetime, list[dict[str, Any]]] = {}
        for item in entry.get("证据", []):
            groups.setdefault(parse_moment(item["采样时刻"]), []).append(item)

        adopted: list[dict[str, Any]] = []
        for moment in sorted(groups):
            items = sorted(groups[moment], key=lambda item: int(item.get("接收顺序", 0)))
            shift = self._shift_of(entry, moment)
            signed = [
                item for item in items
                if item.get("计量签认") and item["计量签认"].get("签认班次") == shift
            ]
            terminal = [item for item in items if item["来源类型"] == SOURCE_TERMINAL]
            replay = [item for item in items if item["来源类型"] == SOURCE_REPLAY and not item.get("计量签认")]
            pending_cache = [
                item for item in items
                if item["来源类型"] == SOURCE_CACHE
                and not (item.get("计量签认") and item["计量签认"].get("签认班次") == shift)
            ]
            if signed:
                chosen, basis = signed[-1], "校准缓存经计量签认"
            elif replay:
                chosen, basis = replay[-1], "补采登记"
            elif terminal:
                chosen, basis = terminal[0], "终端原件优先，缓存待计量签认"
            else:
                chosen = items[-1]
                basis = f"{chosen['来源类型']}单份证据"
            alternates = [item for item in items if item is not chosen]
            adopted.append({
                "采样时刻": format_moment(moment),
                "归属班次": shift,
                "采用证据": chosen["证据编号"],
                "采用数值": chosen["数值"],
                "采用来源": chosen["来源类型"],
                "采用依据": basis,
                "证据份数": len(items),
                "证据编号": [item["证据编号"] for item in items],
                "未采用证据": [item["证据编号"] for item in alternates],
                "未采用数值": [item["数值"] for item in alternates],
                "未采用来源": [item["来源类型"] for item in alternates],
                "待计量签认": [item["证据编号"] for item in pending_cache],
                "计量签认": copy.deepcopy(chosen.get("计量签认")),
            })
        return adopted

    def _recompute(self, entry: dict[str, Any]) -> dict[str, Any]:
        adopted = self._adopt(entry)
        by_moment = {parse_moment(item["采样时刻"]): item for item in adopted}
        expected = self._expected_moments(entry)
        missing = [moment for moment in expected if moment not in by_moment]

        old_points = [item for item in adopted if item["归属班次"] == OLD_SHIFT]
        new_points = [item for item in adopted if item["归属班次"] == NEW_SHIFT]
        missing_old = [moment for moment in missing if self._shift_of(entry, moment) == OLD_SHIFT]
        missing_new = [moment for moment in missing if self._shift_of(entry, moment) == NEW_SHIFT]

        entry["旧班实收"] = len(old_points)
        entry["新班实收"] = len(new_points)
        entry["旧班缺点"] = len(missing_old)
        entry["新班缺点"] = len(missing_new)

        boundary_temp = "—"
        old_ordered = sorted(old_points, key=lambda item: item["采样时刻"])
        if old_ordered:
            boundary_temp = old_ordered[-1]["采用数值"]
        entry["采用温度"] = boundary_temp
        entry["交叠段采用"] = [
            {
                "采样时刻": item["采样时刻"],
                "采用数值": item["采用数值"],
                "采用证据": item["采用证据"],
                "采用依据": item["采用依据"],
            }
            for item in adopted if self._in_overlap(entry, parse_moment(item["采样时刻"]))
        ]

        defect_ranges = self._group_ranges(entry, missing)
        hot_ranges = self._hot_ranges(entry, expected, by_moment)
        entry["缺点区段"] = copy.deepcopy(defect_ranges)
        entry["高温复采区段"] = copy.deepcopy(hot_ranges)
        entry["核对摘要"] = {
            "核对时刻": format_moment(datetime.now()),
            "缺点合计": len(missing),
            "证据份数": len(entry.get("证据", [])),
            "采用观测数": len(adopted),
        }
        return {
            "采用观测": adopted,
            "缺点时刻": [format_moment(moment) for moment in missing],
            "缺点区段": defect_ranges,
            "缺点合计": len(missing),
            "旧班缺点": len(missing_old),
            "新班缺点": len(missing_new),
            "旧班实收": len(old_points),
            "新班实收": len(new_points),
            "采用温度": boundary_temp,
            "交叠段采用": copy.deepcopy(entry["交叠段采用"]),
            "高温复采区段": hot_ranges,
            "同一时刻多份证据": [
                item for item in adopted if item["证据份数"] > 1
            ],
            "待计量签认": [
                {
                    "采样时刻": item["采样时刻"],
                    "采用数值": item["采用数值"],
                    "证据编号": item["待计量签认"],
                    "归属班次": item["归属班次"],
                }
                for item in adopted if item["待计量签认"]
            ],
        }

    def _group_ranges(self, entry: dict[str, Any], moments: list[datetime]) -> list[dict[str, Any]]:
        if not moments:
            return []
        step = timedelta(seconds=self._interval(entry))
        ranges: list[dict[str, Any]] = []
        start = previous = moments[0]
        for moment in moments[1:]:
            if moment - previous == step and self._shift_of(entry, moment) == self._shift_of(entry, start):
                previous = moment
                continue
            ranges.append(self._range_record(entry, start, previous))
            start = previous = moment
        ranges.append(self._range_record(entry, start, previous))
        return ranges

    def _range_record(self, entry: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
        step = timedelta(seconds=self._interval(entry))
        count = int((end - start) / step) + 1
        return {
            "区段编号": f"GAP-{entry['交接编号']}-{format_moment(start)[11:16].replace(':', '')}",
            "班次": self._shift_of(entry, start),
            "起点": format_moment(start),
            "终点": format_moment(end),
            "缺失点数": count,
            "采样时刻": [format_moment(start + step * index) for index in range(count)],
        }

    def _hot_ranges(
        self,
        entry: dict[str, Any],
        expected: list[datetime],
        by_moment: dict[datetime, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        threshold = float(entry.get("高温阈值") or DEFAULT_HIGH_TEMP_THRESHOLD)
        needed = max(int(entry.get("高温连续次数") or DEFAULT_HIGH_TEMP_CONSECUTIVE), 2)
        step = timedelta(seconds=self._interval(entry))
        ranges: list[dict[str, Any]] = []
        run: list[dict[str, Any]] = []

        def flush() -> None:
            nonlocal run
            if len(run) >= needed:
                values = [item["采用数值"] for item in run]
                ranges.append({
                    "区段编号": f"HOT-{entry['交接编号']}-{run[0]['采样时刻'][11:16].replace(':', '')}",
                    "班次": run[0]["归属班次"],
                    "起点": run[0]["采样时刻"],
                    "终点": run[-1]["采样时刻"],
                    "连续点数": len(run),
                    "峰值": max(values),
                    "阈值": threshold,
                    "触发规则": f"连续{needed}次超过{threshold}",
                    "数据是否齐全": True,
                    "派发": "值守复采",
                    "采用证据": [item["采用证据"] for item in run],
                })
            run = []

        previous_moment: datetime | None = None
        for moment in expected:
            item = by_moment.get(moment)
            continuous = previous_moment is not None and moment - previous_moment == step
            if not continuous:
                flush()
            if item is None:
                flush()
                previous_moment = moment
                continue
            if float(item["采用数值"]) > threshold:
                if not continuous:
                    flush()
                run.append(item)
            else:
                flush()
            previous_moment = moment
        flush()
        return ranges

    def _refresh_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """重算并写回交接表的派生字段，保证列表与交接表口径一致。"""
        reconciliation = self._recompute(entry)
        entry["旧班实收"] = reconciliation["旧班实收"]
        entry["新班实收"] = reconciliation["新班实收"]
        entry["旧班缺点"] = reconciliation["旧班缺点"]
        entry["新班缺点"] = reconciliation["新班缺点"]
        entry["采用温度"] = reconciliation["采用温度"]
        entry["abnormal"] = reconciliation["缺点合计"] > 0
        entry["pending"] = entry.get("status") not in ("已签发", "已取消")
        return reconciliation

    def _replay_view(
        self,
        entry: dict[str, Any],
        defect_ranges: list[dict[str, Any]],
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        """合并已登记补采区段与当前缺陷，输出仍缺点数的只读视图。"""
        records = copy.deepcopy(entry.get("补采区段", []))
        known_ranges = {(record["起点"], record["终点"]) for record in records}
        covered = {
            moment_text
            for record in records
            for moment_text in record.get("采样时刻", [])
        }
        for defect in defect_ranges:
            if (defect["起点"], defect["终点"]) in known_ranges:
                continue
            moments = list(defect["采样时刻"])
            if moments and all(moment in covered for moment in moments):
                continue
            records.append({
                "区段编号": defect["区段编号"].replace("GAP-", "RPL-"),
                "班次": defect["班次"],
                "起点": defect["起点"],
                "终点": defect["终点"],
                "缺失点数": defect["缺失点数"],
                "采样时刻": moments,
                "登记时刻": format_moment(datetime.now()) if actor else "—",
                "登记角色": actor or "—",
            })
            known_ranges.add((defect["起点"], defect["终点"]))
            covered.update(moments)
        self._fill_replay_progress(entry, records)
        return records

    def _sync_replay_ranges(
        self,
        entry: dict[str, Any],
        defect_ranges: list[dict[str, Any]],
        actor: str,
    ) -> list[dict[str, Any]]:
        """把实际未齐的区间正式登记进补采区段，原区段边界保持不变。"""
        entries = self._replay_view(entry, defect_ranges, actor)
        entry["补采区段"] = entries
        return copy.deepcopy(entries)

    def _fill_replay_progress(
        self,
        entry: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> None:
        observed = {
            parse_moment(item["采样时刻"])
            for item in self._adopt(entry)
            if item.get("采用证据")
        }
        for record in records:
            moments = record.get("采样时刻") or []
            remaining = [text for text in moments if parse_moment(text) not in observed]
            record["仍缺采样时刻"] = remaining
            record["仍缺点数"] = len(remaining)
            record["是否仍缺"] = bool(remaining)
            record["是否补采完毕"] = not remaining
            record["补回点数"] = int(record.get("缺失点数", 0)) - len(remaining)

    def _mark_replay_evidence(self, entry: dict[str, Any], moment: datetime) -> None:
        """补采登记后刷新已登记的补采区段，避免展示旧的仍缺状态。"""
        self._fill_replay_progress(entry, entry.setdefault("补采区段", []))

    # ------------------------------------------------------------------
    # 内部：动作与权限
    # ------------------------------------------------------------------
    def _permissions(self, actor: str, entry: dict[str, Any]) -> dict[str, Any]:
        editable = entry.get("status") not in ("已签发", "已取消")
        return {
            "可查看": True,
            "只读": actor == ROLE_OBSERVER,
            "可提交证据": editable and actor in (ROLE_OUTGOING, ROLE_INCOMING),
            "可签认缓存": editable and actor == ROLE_METROLOGY,
            "可确认班次": editable and (
                OLD_SHIFT if actor == ROLE_OUTGOING else NEW_SHIFT if actor == ROLE_INCOMING else ""
            ),
            "可签发": editable and actor in (ROLE_OUTGOING, ROLE_INCOMING),
            "可启动": editable and actor in (ROLE_OUTGOING, ROLE_INCOMING),
        }

    def _start(self, entry: dict[str, Any], actor: str) -> None:
        if actor not in (ROLE_OUTGOING, ROLE_INCOMING):
            raise ShiftHandoverError("只有交班双方可以启动交接，计量负责人只做签认")
        if entry["status"] != "草稿":
            raise ShiftHandoverError("只有草稿状态的交接记录可以启动")
        reconciliation = self._refresh_entry(entry)
        entry["启动时缺点"] = reconciliation["缺点合计"]
        entry["status"] = "核对中"
        entry["pending"] = True
        self._sync_replay_ranges(entry, reconciliation["缺点区段"], actor)
        self._audit(entry, actor, "启动交接", {
            "启动时缺点": reconciliation["缺点合计"],
            "缺点区段": [
                {"班次": item["班次"], "起点": item["起点"], "终点": item["终点"], "缺失点数": item["缺失点数"]}
                for item in reconciliation["缺点区段"]
            ],
        })

    def _confirm(self, entry: dict[str, Any], actor: str, shift: str) -> None:
        if entry["status"] not in ("核对中", "待签发"):
            raise ShiftHandoverError("交接尚未启动或已经结束，不能确认班次")
        expected_role = ROLE_OUTGOING if shift == OLD_SHIFT else ROLE_INCOMING
        if actor != expected_role:
            raise ShiftHandoverError(
                f"{ROLE_LABELS.get(actor, actor)}不能确认{shift}，{shift}只能由{ROLE_LABELS[expected_role]}确认"
            )
        self._recompute(entry)
        entry.setdefault("确认记录", {})[shift] = {
            "确认人": ROLE_LABELS[actor],
            "确认角色": actor,
            "确认时刻": format_moment(datetime.now()),
            "核对范围": self._overlap_label(entry),
            "旧班实收": entry["旧班实收"],
            "新班实收": entry["新班实收"],
            "旧班缺点": entry["旧班缺点"],
            "新班缺点": entry["新班缺点"],
        }
        self._audit(entry, actor, f"确认{shift}", {
            "交叠段": self._overlap_label(entry),
            "实收": {"旧班": entry["旧班实收"], "新班": entry["新班实收"]},
            "缺点": {"旧班": entry["旧班缺点"], "新班": entry["新班缺点"]},
        })
        confirmations = entry["确认记录"]
        entry["status"] = "待签发" if {OLD_SHIFT, NEW_SHIFT} <= set(confirmations) else "核对中"
        entry["pending"] = entry["status"] != "已签发"
        entry["abnormal"] = entry["旧班缺点"] + entry["新班缺点"] > 0

    def _issue(self, entry: dict[str, Any], actor: str) -> None:
        if actor not in (ROLE_OUTGOING, ROLE_INCOMING):
            raise ShiftHandoverError("只有交班双方可以签发交接")
        if entry["status"] != "待签发":
            raise ShiftHandoverError("交班双方都确认后才能签发")
        pending = [
            item for item in self._adopt(entry)
            if item["待计量签认"]
        ]
        if pending:
            moments = "、".join(item["采样时刻"] for item in pending)
            raise ShiftHandoverError(
                f"{moments} 存在待计量签认的校准缓存，先签认或明确不采用后再签发"
            )
        entry["是否签发"] = True
        entry["status"] = "已签发"
        entry["pending"] = False
        entry["abnormal"] = entry["旧班缺点"] + entry["新班缺点"] > 0
        self._audit(entry, actor, "签发交接", {
            "采用温度": entry["采用温度"],
            "旧班实收": entry["旧班实收"],
            "新班实收": entry["新班实收"],
        })

    def _cancel(self, entry: dict[str, Any], actor: str) -> None:
        if actor == ROLE_METROLOGY:
            raise ShiftHandoverError("计量负责人不能取消交接")
        if entry["status"] == "已签发":
            raise ShiftHandoverError("已签发的交接不能取消")
        entry["status"] = "已取消"
        entry["pending"] = False
        entry["是否签发"] = False
        self._audit(entry, actor, "取消交接", {})

    def _overlap_label(self, entry: dict[str, Any]) -> dict[str, str]:
        boundary = self._boundary(entry)
        return {
            "旧班": format_moment(boundary - timedelta(minutes=OVERLAP_CHECK_MINUTES)),
            "班界": format_moment(boundary),
            "新班": format_moment(boundary + timedelta(minutes=OVERLAP_CHECK_MINUTES)),
        }

    def _audit(self, entry: dict[str, Any], actor: str, action: str, detail: dict[str, Any]) -> None:
        entry.setdefault("审计日志", []).append({
            "时刻": format_moment(datetime.now()),
            "角色": actor,
            "操作人": ROLE_LABELS.get(actor, actor),
            "动作": action,
            "详情": copy.deepcopy(detail),
        })

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        latest = {}
        for row in store.rows(self.MODULE):
            latest = row
        if latest:
            base["latest"] = {
                "交接编号": latest.get("交接编号"),
                "状态": latest.get("status"),
                "旧班实收": latest.get("旧班实收"),
                "新班实收": latest.get("新班实收"),
                "旧班缺点": latest.get("旧班缺点"),
                "新班缺点": latest.get("新班缺点"),
                "采用温度": latest.get("采用温度"),
            }
        return base
