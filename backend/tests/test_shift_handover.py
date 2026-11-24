"""班次交接领域测试：班次归属、双份证据采用、权限收口与心跳汇总。"""
from __future__ import annotations

import pytest

from app.services.shift_handover import (
    OLD_SHIFT,
    ShiftHandoverError,
    ShiftHandoverService,
)
from app.store import store


@pytest.fixture()
def service() -> ShiftHandoverService:
    # 每个用例重置交接记录，避免用例之间互相污染
    store.rows("shift_handover").clear()
    store._tables["shift_handover"] = [
        {**row} for row in _seed_rows()
    ]
    return ShiftHandoverService()


def _seed_rows():
    from app.seed import _build_shift_handover_seed

    return _build_shift_handover_seed()


def _sheet(service, role="接班人"):
    return service.sheet(1, role=role)["reconciliation"]


def test_shift_split_uses_raw_sample_moments(service):
    """班界 08:00：07:45-07:59 计旧班，08:00 起计新班；缺采样时刻计缺点。"""
    sheet = _sheet(service)
    assert sheet["旧班实收"] == 12
    assert sheet["新班实收"] == 14
    assert sheet["旧班缺点"] == 3
    assert sheet["新班缺点"] == 1
    assert sheet["缺点合计"] == 4
    ranges = [(item["起点"], item["终点"], item["缺失点数"]) for item in sheet["缺点区段"]]
    assert ranges == [
        ("2026-11-24T07:52:00", "2026-11-24T07:54:00", 3),
        ("2026-11-24T08:05:00", "2026-11-24T08:05:00", 1),
    ]


def test_terminal_original_kept_until_metrology_signs_cache(service):
    """07:59 终端原件 81 与校准缓存 82 并存：未签认用原件，签认后采用 82。"""
    service.run_action(1, "启动交接", role="旧班负责人")
    submitted = service.submit_evidence(
        1, moment="2026-11-24T07:59:00", value=82,
        source_type="校准缓存", role="接班人",
    )
    evidence_id = submitted["evidence"]["证据编号"]

    sheet = _sheet(service)
    assert sheet["采用温度"] == 81.0
    assert sheet["待计量签认"][0]["采样时刻"] == "2026-11-24T07:59:00"
    dual = sheet["同一时刻多份证据"][0]
    assert dual["采用数值"] == 81.0
    assert dual["未采用数值"] == [82.0]
    # 两份证据都在，原值没有被覆盖
    evidence = store.find("shift_handover", 1)["证据"]
    values = {(item["来源类型"], item["数值"]) for item in evidence if item["采样时刻"] == "2026-11-24T07:59:00"}
    assert values == {("终端原件", 81.0), ("校准缓存", 82.0)}

    service.sign_off(1, evidence_id=evidence_id, shift=OLD_SHIFT, role="计量负责人")
    sheet = _sheet(service)
    assert sheet["采用温度"] == 82.0
    dual = sheet["同一时刻多份证据"][0]
    assert dual["采用数值"] == 82.0
    assert dual["未采用数值"] == [81.0]
    assert dual["采用来源"] == "校准缓存"
    # 实收只计一个观测
    assert sheet["旧班实收"] == 12


def test_sign_off_must_match_evidence_shift(service):
    service.run_action(1, "启动交接", role="旧班负责人")
    submitted = service.submit_evidence(
        1, moment="2026-11-24T07:59:00", value=82,
        source_type="校准缓存", role="接班人",
    )
    with pytest.raises(ShiftHandoverError):
        service.sign_off(
            1, evidence_id=submitted["evidence"]["证据编号"],
            shift="新班", role="计量负责人",
        )
    # 终端原件无需计量签认
    with pytest.raises(ShiftHandoverError):
        service.sign_off(
            1, evidence_id="EV-HAND-20261124-01-12",
            shift=OLD_SHIFT, role="计量负责人",
        )


def test_roles_are_scoped_and_observer_read_only(service):
    entry, message = service.run_action(1, "启动交接", role="观察者")
    assert entry is None and "只读" in message
    with pytest.raises(ShiftHandoverError):
        service.submit_evidence(
            1, moment="2026-11-24T07:59:00", value=82,
            source_type="校准缓存", role="观察者",
        )
    service.run_action(1, "启动交接", role="旧班负责人")
    # 接班人不能替旧班确认，旧班负责人不能签认新班
    assert service.run_action(1, "确认旧班", role="接班人")[0] is None
    assert service.run_action(1, "确认新班", role="旧班负责人")[0] is None
    assert service.run_action(1, "确认旧班", role="旧班负责人")[0] is not None
    assert service.run_action(1, "确认新班", role="接班人")[0] is not None
    assert store.find("shift_handover", 1)["status"] == "待签发"
    observer = service.sheet(1, role="观察者")
    assert observer["permissions"]["只读"] is True
    assert observer["permissions"]["可提交证据"] is False


def test_pending_cache_blocks_issue_until_signed(service):
    service.run_action(1, "启动交接", role="旧班负责人")
    submitted = service.submit_evidence(
        1, moment="2026-11-24T07:59:00", value=82,
        source_type="校准缓存", role="接班人",
    )
    service.run_action(1, "确认旧班", role="旧班负责人")
    service.run_action(1, "确认新班", role="接班人")
    entry, message = service.run_action(1, "签发交接", role="旧班负责人")
    assert entry is None and "待计量签认" in message
    service.sign_off(
        1, evidence_id=submitted["evidence"]["证据编号"],
        shift=OLD_SHIFT, role="计量负责人",
    )
    entry, _ = service.run_action(1, "签发交接", role="旧班负责人")
    assert entry["status"] == "已签发"
    # 签发后证据冻结，观察者依旧只读
    with pytest.raises(ShiftHandoverError):
        service.submit_evidence(
            1, moment="2026-11-24T07:59:00", value=83,
            source_type="校准缓存", role="接班人",
        )


def test_heartbeat_summary_splits_normal_defects_and_hot_ranges(service):
    service.run_action(1, "启动交接", role="旧班负责人")
    submitted = service.submit_evidence(
        1, moment="2026-11-24T07:59:00", value=82,
        source_type="校准缓存", role="接班人",
    )
    service.sign_off(
        1, evidence_id=submitted["evidence"]["证据编号"],
        shift=OLD_SHIFT, role="计量负责人",
    )
    summary = service.heartbeat_summary(1, role="接班人")
    assert summary["关联落盘点"]["旧班"] == 12
    assert summary["关联落盘点"]["新班"] == 14
    # 常规缺点只统计实际未齐的采样时刻
    assert summary["常规缺点"]["合计缺点"] == 4
    assert [(item["起点"], item["缺失点数"]) for item in summary["常规缺点"]["区段"]] == [
        ("2026-11-24T07:52:00", 3),
        ("2026-11-24T08:05:00", 1),
    ]
    # 07:57-07:59 连续三次超过 81.5，即便时刻齐全也单独派发复采
    hot = summary["高温复采区段"]
    assert len(hot) == 1
    assert (hot[0]["起点"], hot[0]["终点"], hot[0]["连续点数"]) == (
        "2026-11-24T07:57:00", "2026-11-24T07:59:00", 3,
    )
    assert hot[0]["派发"] == "值守复采"
    assert summary["补采区段"]["仍缺点数"] == 4


def test_hot_ranges_need_three_consecutive_and_ignore_missing_breaks(service):
    """缺采样时刻会打断连续段；只有连续三次超温才派发复采。"""
    service.run_action(1, "启动交接", role="旧班负责人")
    summary = service.heartbeat_summary(1, role="接班人")
    # 正常口径下 07:57/07:58 两次超温，07:59 是 81，08:05 缺失，不构成连续三次
    assert summary["高温复采区段"] == []


def test_replay_only_fills_missing_moments_and_keeps_gaps_visible(service):
    service.run_action(1, "启动交接", role="旧班负责人")
    result = service.record_replay(
        1,
        samples=[
            {"采样时刻": "2026-11-24T07:52:00", "数值": 81.1},
            {"采样时刻": "2026-11-24T07:45:00", "数值": 81.0},
        ],
        role="接班人",
    )
    assert len(result["accepted"]) == 1
    assert result["rejected"][0]["采样时刻"] == "2026-11-24T07:45:00"
    gaps = {(item["起点"], item["仍缺点数"]) for item in result["补采区段"]}
    # 07:52 补回后原区段仍保留，仍缺 2 个时刻
    assert ("2026-11-24T07:52:00", 2) in gaps
    assert result["reconciliation"]["缺点合计"] == 3
    summary = service.heartbeat_summary(1, role="接班人")
    assert summary["补采区段"]["仍缺点数"] == 3
