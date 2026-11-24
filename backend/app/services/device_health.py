"""采集心跳领域服务：维护采集终端心跳上报间隔、丢包次数与缓存积压的健康判定。
检测采集终端心跳数据生成健康状态判定并写入心跳健康日志数据集。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DeviceHealthService(BaseModuleService):
    MODULE = "device_health"
    ENTITY = "心跳记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["心跳编号", "采集终端", "上报时刻", "间隔秒数", "丢包次数", "缓存积压", "判定阈值", "心跳状态"]
    # 数据采集操作：检查采集终端心跳数据进行健康评估判定写入心跳健康日志文件
    REQUIRED_FIELDS = ["心跳编号", "采集终端", "上报时刻"]
    STATUS_ORDER = ["草稿", "正常", "异常", "已失联"]
    ACTION_RULES = {"标记正常": "正常", "标记异常": "异常", "标记失联": "已失联"}
    NEGATIVE_ACTIONS = ["标记异常", "标记失联"]

    def check_health(self, heartbeat_id: int) -> dict[str, Any]:
        """检查采集终端心跳数据进行健康评估判定，写入心跳健康状态日志文件。"""
        hb = store.find(self.MODULE, heartbeat_id)
        if hb is None:
            return {"error": f"心跳记录 {heartbeat_id} 不存在"}
        terminal = hb["采集终端"]
        interval = int(hb.get("间隔秒数", "30"))
        loss = int(hb.get("丢包次数", "0"))
        backlog = int(hb.get("缓存积压", "0"))
        threshold = int(hb.get("判定阈值", "120"))

        if interval > threshold:
            self.run_action(heartbeat_id, "标记失联")
            # 数据校验输出：汇总健康诊断写入采集终端监控数据文件
            status = "已失联"
        elif loss > 5 or backlog > 1000:
            self.run_action(heartbeat_id, "标记异常")
            status = "异常"
        else:
            self.run_action(heartbeat_id, "标记正常")
            status = "正常"

        # 写入心跳健康诊断数据文件
        self._write_jsonl(
            self._data_dir("heartbeat_logs") / f"{terminal}_health.jsonl",
            [{"terminal": terminal, "interval_s": interval, "packet_loss": loss,
              "buffer_backlog": backlog, "threshold_s": threshold, "status": status,
              "checked_at": time.time()}]
        )
        return {"terminal": terminal, "interval_s": interval, "packet_loss": loss,
                "backlog": backlog, "health_status": status}

    def summarize_shift(self, handover_id: int, role: str | None = None) -> dict[str, Any]:
        """心跳汇总关联班次实际落盘点：输出常规缺点、高温复采区段与补采缺口。"""
        handover_svc = self._safe_import(
            "app.services.shift_handover", "ShiftHandoverService")()
        summary = handover_svc.heartbeat_summary(handover_id, role=role)
        # 心跳汇总落盘，便于值守按终端复核落盘点与缺点区段
        self._write_jsonl(
            self._data_dir("heartbeat_logs") / f"{summary['采集终端']}_shift_summary.jsonl",
            [{"summary_id": summary["汇总编号"],
              "handover_id": summary["交接编号"],
              "terminal": summary["采集终端"],
              "point": summary["测点编号"],
              "old_shift_received": summary["关联落盘点"]["旧班"],
              "new_shift_received": summary["关联落盘点"]["新班"],
              "normal_defects": summary["常规缺点"]["合计缺点"],
              "normal_defect_ranges": summary["常规缺点"]["区段"],
              "high_temp_ranges": summary["高温复采区段"],
              "replay_ranges": summary["补采区段"]["区段"],
              "unissued": not summary["是否签发"],
              "generated_at": summary["生成时刻"]}]
        )
        return summary

    def summarize_all_shifts(self, role: str | None = None) -> dict[str, Any]:
        """汇总所有仍在交接中的班次落盘点，供值守统一复采。"""
        handovers = store.rows("shift_handover")
        summaries = []
        for row in handovers:
            if row.get("status") in ("已取消",):
                continue
            try:
                summaries.append(self.summarize_shift(row["id"], role=role))
            except Exception as exc:  # 交接表未就绪时不影响心跳主流程
                summaries.append({"handover_id": row.get("交接编号"), "error": str(exc)})
        return {
            "total": len(summaries),
            "normal_defects": sum(
                int(item.get("常规缺点", {}).get("合计缺点", 0)) for item in summaries),
            "high_temp_ranges": sum(
                len(item.get("高温复采区段", [])) for item in summaries),
            "summaries": summaries,
        }

    def scan_all_terminals(self) -> dict[str, Any]:
        """批量扫描所有采集终端的心跳记录进行健康状态汇总诊断。"""
        all_hbs = store.rows(self.MODULE)
        results = []
        for hb in all_hbs:
            r = self.check_health(hb["id"])
            results.append(r)
        normal = sum(1 for r in results if r.get("health_status") == "正常")
        abnormal = sum(1 for r in results if r.get("health_status") == "异常")
        lost = sum(1 for r in results if r.get("health_status") == "已失联")
        # 汇总健康诊断写入采集终端健康监控文件
        self._write_jsonl(
            self._data_dir("heartbeat_logs") / "scan_summary.jsonl",
            [{"terminals_total": len(results), "normal": normal,
              "abnormal": abnormal, "lost": lost, "scanned_at": time.time()}]
        )
        return {"total": len(results), "normal": normal,
                "abnormal": abnormal, "lost_contact": lost}