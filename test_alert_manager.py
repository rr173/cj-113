import pytest
from datetime import datetime, timedelta

from alert_manager import (
    AlertManager, INFO, WARNING, CRITICAL,
    NOTIFICATION_STATUS_SENT, NOTIFICATION_STATUS_UNATTENDED,
    NOTIFICATION_STATUS_CANCELLED,
)


@pytest.fixture
def am():
    return AlertManager()


class TestAlertLevelAndRepeatEscalation:
    def test_new_alert_is_info_level(self, am):
        alert = am.report_alert("LOAD_SHEDDING", "发生甩负荷事件")
        assert alert.level == INFO
        assert alert.occurrence_count == 1
        assert alert.acknowledged is False

    def test_repeat_alert_within_window_escalates_to_warning(self, am):
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            now = base_time + timedelta(minutes=i * 2)
            am.report_alert("LOAD_SHEDDING", "发生甩负荷事件", now=now)
        alerts = am.get_active_alerts()
        assert len(alerts) == 1
        assert alerts[0].level == WARNING
        assert alerts[0].occurrence_count == 3
        assert len(alerts[0].escalation_history) == 1
        assert alerts[0].escalation_history[0].to_level == WARNING

    def test_repeat_alert_outside_window_stays_info(self, am):
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            now = base_time + timedelta(minutes=i * 3)
            am.report_alert("LOAD_SHEDDING", "发生甩负荷事件", now=now)
        alerts = am.get_active_alerts()
        assert len(alerts) == 1
        assert alerts[0].level == INFO

    def test_escalation_stops_after_acknowledge(self, am):
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        alert = am.report_alert("LOAD_SHEDDING", "发生甩负荷事件", now=base_time)
        alert_id = alert.alert_id
        am.acknowledge_alert(alert_id, acknowledged_by="张三", now=base_time + timedelta(minutes=1))
        for i in range(5):
            now = base_time + timedelta(minutes=2 + i)
            am.report_alert("LOAD_SHEDDING", "发生甩负荷事件", now=now)
        acknowledged = am.get_alert(alert_id)
        assert acknowledged.acknowledged is True
        active = am.get_active_alerts()
        assert len(active) == 1
        assert active[0].alert_id != alert_id
        assert active[0].level == WARNING

    def test_same_type_alert_merges_into_existing(self, am):
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        a1 = am.report_alert("LOAD_SHEDDING", "甩负荷A", now=base_time)
        a2 = am.report_alert("LOAD_SHEDDING", "甩负荷B", now=base_time + timedelta(minutes=1),
                             data={"kw": 100})
        assert a1.alert_id == a2.alert_id
        assert a2.occurrence_count == 2
        assert a2.data["kw"] == 100

    def test_different_type_alerts_are_separate(self, am):
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        a1 = am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time)
        a2 = am.report_alert("BATTERY_LIFE_WARNING", "电池寿命", now=base_time)
        assert a1.alert_id != a2.alert_id
        assert len(am.get_active_alerts()) == 2


class TestTimeBasedEscalationWarningToCritical:
    def test_warning_unacknowledged_10min_escalates_to_critical(self, am):
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        alerts = am.get_active_alerts()
        assert alerts[0].level == WARNING
        warning_time = alerts[0].escalation_history[0].timestamp
        escalated = am.check_time_based_escalation(now=warning_time + timedelta(minutes=10))
        assert len(escalated) == 1
        assert escalated[0].level == CRITICAL
        assert len(escalated[0].escalation_history) == 2
        assert escalated[0].escalation_history[1].to_level == CRITICAL

    def test_warning_acknowledged_does_not_escalate(self, am):
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            alert = am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        am.acknowledge_alert(alert.alert_id, acknowledged_by="李四", now=base_time + timedelta(minutes=4))
        escalated = am.check_time_based_escalation(now=base_time + timedelta(minutes=20))
        assert len(escalated) == 0

    def test_warning_under_10min_does_not_escalate(self, am):
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        escalated = am.check_time_based_escalation(now=base_time + timedelta(minutes=9))
        assert len(escalated) == 0


class TestCriticalAlertTriggersNotification:
    def test_critical_creates_notification(self, am):
        am.add_duty_staff("张三", "13800000001", 0, 23)
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            alert = am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        escalated = am.check_time_based_escalation(now=base_time + timedelta(minutes=15))
        assert len(escalated) == 1
        notifications = am.get_notifications()
        assert len(notifications) == 1
        assert notifications[0].alert_id == alert.alert_id
        assert "[紧急]" in notifications[0].alert_summary
        assert "普通 → 警告 → 紧急" in notifications[0].escalation_path
        assert notifications[0].status == NOTIFICATION_STATUS_SENT
        assert notifications[0].sent_to == "张三"

    def test_critical_no_on_duty_marks_unattended(self, am):
        am.add_duty_staff("张三", "13800000001", 0, 8)
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        escalated = am.check_time_based_escalation(now=base_time + timedelta(minutes=15))
        notifications = am.get_notifications()
        assert len(notifications) == 1
        assert notifications[0].status == NOTIFICATION_STATUS_UNATTENDED
        assert notifications[0].retry_at is not None
        assert notifications[0].sent_to is None

    def test_suggested_action_included(self, am):
        am.add_duty_staff("张三", "13800000001", 0, 23)
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            alert = am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        am.check_time_based_escalation(now=base_time + timedelta(minutes=15))
        notifications = am.get_notifications()
        assert "检查负荷配置" in notifications[0].suggested_action


class TestDutyStaffCRUD:
    def test_add_duty_staff(self, am):
        staff = am.add_duty_staff("张三", "13800000001", 8, 20)
        assert staff.staff_id.startswith("DS-")
        assert staff.name == "张三"
        assert staff.start_hour == 8
        assert staff.end_hour == 20

    def test_add_staff_invalid_hour_raises(self, am):
        with pytest.raises(ValueError):
            am.add_duty_staff("张三", "138", 24, 8)
        with pytest.raises(ValueError):
            am.add_duty_staff("张三", "138", 0, 24)

    def test_list_duty_staff(self, am):
        am.add_duty_staff("张三", "13800000001", 8, 20)
        am.add_duty_staff("李四", "13800000002", 20, 8)
        staff_list = am.list_duty_staff()
        assert len(staff_list) == 2

    def test_get_duty_staff_detail(self, am):
        s = am.add_duty_staff("张三", "13800000001", 8, 20)
        fetched = am.get_duty_staff(s.staff_id)
        assert fetched is not None
        assert fetched.name == "张三"
        assert am.get_duty_staff("INVALID") is None

    def test_update_duty_staff(self, am):
        s = am.add_duty_staff("张三", "13800000001", 8, 20)
        success = am.update_duty_staff(s.staff_id, name="张三改", start_hour=9)
        assert success is True
        updated = am.get_duty_staff(s.staff_id)
        assert updated.name == "张三改"
        assert updated.start_hour == 9
        assert updated.end_hour == 20
        assert am.update_duty_staff("INVALID", name="X") is False

    def test_update_invalid_hour_raises(self, am):
        s = am.add_duty_staff("张三", "138", 8, 20)
        with pytest.raises(ValueError):
            am.update_duty_staff(s.staff_id, start_hour=24)

    def test_delete_duty_staff(self, am):
        s = am.add_duty_staff("张三", "138", 8, 20)
        assert am.delete_duty_staff(s.staff_id) is True
        assert len(am.list_duty_staff()) == 0
        assert am.delete_duty_staff("INVALID") is False


class TestOnDutyJudgement:
    def test_normal_shift(self, am):
        s = am.add_duty_staff("张三", "138", 8, 20)
        t1 = datetime(2024, 6, 20, 12, 0, 0)
        assert am.is_on_duty(s, t1) is True
        t2 = datetime(2024, 6, 20, 7, 59, 0)
        assert am.is_on_duty(s, t2) is False
        t3 = datetime(2024, 6, 20, 20, 0, 0)
        assert am.is_on_duty(s, t3) is False

    def test_cross_midnight_shift(self, am):
        s = am.add_duty_staff("李四", "139", 20, 8)
        t1 = datetime(2024, 6, 20, 22, 0, 0)
        assert am.is_on_duty(s, t1) is True
        t2 = datetime(2024, 6, 20, 2, 0, 0)
        assert am.is_on_duty(s, t2) is True
        t3 = datetime(2024, 6, 20, 12, 0, 0)
        assert am.is_on_duty(s, t3) is False

    def test_get_on_duty_staff(self, am):
        am.add_duty_staff("白班", "111", 8, 20)
        am.add_duty_staff("夜班", "222", 20, 8)
        t_day = datetime(2024, 6, 20, 12, 0, 0)
        on_duty = am.get_on_duty_staff(t_day)
        assert len(on_duty) == 1
        assert on_duty[0].name == "白班"
        t_night = datetime(2024, 6, 20, 23, 0, 0)
        on_duty = am.get_on_duty_staff(t_night)
        assert len(on_duty) == 1
        assert on_duty[0].name == "夜班"

    def test_get_next_on_duty_time(self, am):
        am.add_duty_staff("白班", "111", 8, 20)
        t_early = datetime(2024, 6, 20, 5, 0, 0)
        next_t = am.get_next_on_duty_time(t_early)
        assert next_t.hour == 8
        assert next_t.day == 20

        t_late = datetime(2024, 6, 20, 21, 0, 0)
        next_t = am.get_next_on_duty_time(t_late)
        assert next_t.hour == 8
        assert next_t.day == 21

        t_within = datetime(2024, 6, 20, 12, 0, 0)
        assert am.get_next_on_duty_time(t_within) == t_within


class TestAcknowledgement:
    def test_acknowledge_single_alert(self, am):
        alert = am.report_alert("X", "测试")
        alert_id = alert.alert_id
        success = am.acknowledge_alert(alert_id, acknowledged_by="操作员A")
        assert success is True
        a = am.get_alert(alert_id)
        assert a.acknowledged is True
        assert a.acknowledged_by == "操作员A"
        assert a.acknowledged_at is not None
        active = am.get_active_alerts()
        assert len(active) == 0

    def test_acknowledge_nonexistent_fails(self, am):
        assert am.acknowledge_alert("INVALID") is False

    def test_acknowledge_already_acknowledged_fails(self, am):
        alert = am.report_alert("X", "测试")
        am.acknowledge_alert(alert.alert_id)
        assert am.acknowledge_alert(alert.alert_id) is False

    def test_acknowledge_by_type(self, am):
        am.report_alert("X", "x1")
        am.report_alert("X", "x2")
        am.report_alert("Y", "y1")
        count = am.acknowledge_alerts_by_type("X", acknowledged_by="批量操作")
        assert count == 1
        active = am.get_active_alerts()
        assert len(active) == 1
        assert active[0].alert_type == "Y"


class TestNotificationRetry:
    def test_unattended_retries_when_on_duty_available(self, am):
        am.add_duty_staff("白班", "111", 8, 20)
        base_time = datetime(2024, 6, 20, 2, 0, 0)
        for i in range(3):
            am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        escalated = am.check_time_based_escalation(now=base_time + timedelta(minutes=15))
        notifications = am.get_notifications()
        assert notifications[0].status == NOTIFICATION_STATUS_UNATTENDED

        retry_time = datetime(2024, 6, 20, 9, 0, 0)
        am.process_pending_notifications(retry_time)
        notifications = am.get_notifications()
        assert notifications[0].status == NOTIFICATION_STATUS_SENT
        assert notifications[0].sent_to == "白班"


class TestAcknowledgementCancelsNotifications:
    def test_acknowledge_alert_cancels_unattended_notification(self, am):
        am.add_duty_staff("白班", "111", 8, 20)
        base_time = datetime(2024, 6, 20, 2, 0, 0)
        for i in range(3):
            alert = am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        am.check_time_based_escalation(now=base_time + timedelta(minutes=15))
        notifications = am.get_notifications()
        assert len(notifications) == 1
        assert notifications[0].status == NOTIFICATION_STATUS_UNATTENDED

        am.acknowledge_alert(alert.alert_id, acknowledged_by="操作员", now=base_time + timedelta(minutes=30))

        notifications = am.get_notifications()
        assert notifications[0].status == NOTIFICATION_STATUS_CANCELLED
        assert notifications[0].cancelled_by == "操作员"
        assert "告警已被确认" in notifications[0].cancel_reason
        assert notifications[0].retry_at is None
        assert notifications[0].sent_to is None

    def test_acknowledge_after_time_process_no_send(self, am):
        am.add_duty_staff("白班", "111", 8, 20)
        base_time = datetime(2024, 6, 20, 2, 0, 0)
        for i in range(3):
            alert = am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        am.check_time_based_escalation(now=base_time + timedelta(minutes=15))

        am.acknowledge_alert(alert.alert_id, acknowledged_by="操作员", now=base_time + timedelta(minutes=30))

        retry_time = datetime(2024, 6, 20, 9, 0, 0)
        am.process_pending_notifications(retry_time)

        notifications = am.get_notifications()
        assert len(notifications) == 1
        assert notifications[0].status == NOTIFICATION_STATUS_CANCELLED
        assert notifications[0].sent_to is None

    def test_acknowledge_by_type_cancels_notifications(self, am):
        am.add_duty_staff("白班", "111", 8, 20)
        base_time = datetime(2024, 6, 20, 2, 0, 0)
        for i in range(3):
            am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        am.check_time_based_escalation(now=base_time + timedelta(minutes=15))
        notifications_before = am.get_notifications()
        assert notifications_before[0].status == NOTIFICATION_STATUS_UNATTENDED

        am.acknowledge_alerts_by_type("LOAD_SHEDDING", acknowledged_by="批量操作",
                                       now=base_time + timedelta(minutes=30))

        notifications_after = am.get_notifications()
        assert notifications_after[0].status == NOTIFICATION_STATUS_CANCELLED
        assert notifications_after[0].cancelled_by == "批量操作"

    def test_process_pending_notifications_detects_acknowledged_alert(self, am):
        am.add_duty_staff("白班", "111", 8, 20)
        base_time = datetime(2024, 6, 20, 2, 0, 0)
        for i in range(3):
            alert = am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        am.check_time_based_escalation(now=base_time + timedelta(minutes=15))
        notifications_before = am.get_notifications()
        assert notifications_before[0].status == NOTIFICATION_STATUS_UNATTENDED

        alert.acknowledged = True
        alert.acknowledged_at = base_time + timedelta(minutes=30)
        alert.acknowledged_by = "手动直接改"

        retry_time = datetime(2024, 6, 20, 9, 0, 0)
        am.process_pending_notifications(retry_time)

        notifications = am.get_notifications()
        assert notifications[0].status == NOTIFICATION_STATUS_CANCELLED
        assert "重试发送时发现" in notifications[0].cancel_reason
        assert notifications[0].sent_to is None

    def test_already_sent_notification_not_cancelled(self, am):
        am.add_duty_staff("白班", "111", 0, 23)
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            alert = am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        am.check_time_based_escalation(now=base_time + timedelta(minutes=15))
        notifications_before = am.get_notifications()
        assert notifications_before[0].status == NOTIFICATION_STATUS_SENT
        assert notifications_before[0].sent_to == "白班"

        am.acknowledge_alert(alert.alert_id, acknowledged_by="操作员")

        notifications_after = am.get_notifications()
        assert notifications_after[0].status == NOTIFICATION_STATUS_SENT
        assert notifications_after[0].sent_to == "白班"
        assert notifications_after[0].cancelled_at is None


class TestQueryInterfaces:
    def test_get_active_alerts_filter_by_level(self, am):
        am.report_alert("A", "a")
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            am.report_alert("B", "b", now=base_time + timedelta(minutes=i))
        info_alerts = am.get_active_alerts(level=INFO)
        assert len(info_alerts) == 1
        warning_alerts = am.get_active_alerts(level=WARNING)
        assert len(warning_alerts) == 1

    def test_get_alert_escalation_history_timeline(self, am):
        am.add_duty_staff("张三", "138", 0, 23)
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        for i in range(3):
            alert = am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        am.check_time_based_escalation(now=base_time + timedelta(minutes=15))
        timeline = am.get_alert_escalation_history(alert.alert_id)
        assert timeline is not None
        events = [e["event"] for e in timeline]
        assert "告警产生" in events
        assert "级别升级" in events
        assert len([e for e in timeline if e["event"] == "级别升级"]) == 2

        am.acknowledge_alert(alert.alert_id, acknowledged_by="王五")
        timeline = am.get_alert_escalation_history(alert.alert_id)
        events = [e["event"] for e in timeline]
        assert "告警确认" in events

    def test_get_notifications_filter_by_status(self, am):
        am.add_duty_staff("白班", "111", 8, 20)
        base_time = datetime(2024, 6, 20, 2, 0, 0)
        for i in range(3):
            am.report_alert("LOAD_SHEDDING", "甩负荷", now=base_time + timedelta(minutes=i))
        am.check_time_based_escalation(now=base_time + timedelta(minutes=15))

        base_time2 = datetime(2024, 6, 20, 10, 0, 0)
        am.acknowledge_alerts_by_type("LOAD_SHEDDING", now=base_time2)
        for i in range(3):
            am.report_alert("LOAD_SHEDDING", "甩负荷B", now=base_time2 + timedelta(minutes=i))
        am.check_time_based_escalation(now=base_time2 + timedelta(minutes=15))

        all_not = am.get_notifications()
        assert len(all_not) == 2
        sent = am.get_notifications(status=NOTIFICATION_STATUS_SENT)
        assert len(sent) == 1
        cancelled = am.get_notifications(status=NOTIFICATION_STATUS_CANCELLED)
        assert len(cancelled) == 1

    def test_alert_statistics(self, am):
        base_time = datetime(2024, 6, 20, 10, 0, 0)
        am.report_alert("A", "a1", now=base_time)
        am.report_alert("A", "a2", now=base_time + timedelta(minutes=10))
        am.report_alert("A", "a3", now=base_time + timedelta(minutes=20))
        for i in range(3):
            am.report_alert("B", "b", now=base_time + timedelta(minutes=i))
        stats = am.get_alert_statistics(now=base_time + timedelta(minutes=30))
        assert stats["total_alerts"] == 2
        assert stats["active_count"] == 2
        assert stats["by_level"][INFO] == 1
        assert stats["by_level"][WARNING] == 1
        type_list = {t["alert_type"]: t for t in stats["by_type"]}
        assert "A" in type_list
        assert "B" in type_list
        assert type_list["A"]["total"] == 1
        assert type_list["A"]["info"] == 1
        assert type_list["B"]["warning"] == 1
        assert stats["last_24h_total"] == 2
        assert len(stats["last_24h_trend"]) == 24


class TestIntegrationWithMicrogridState:
    def test_state_add_alert_uses_manager(self):
        from models import MicrogridState
        s = MicrogridState()
        s.add_alert("TEST_ALERT", "测试告警", {"key": "value"})
        active = s.alert_manager.get_active_alerts()
        assert len(active) == 1
        assert active[0].level == INFO
        assert active[0].alert_type == "TEST_ALERT"
        assert len(s.alerts) == 1
        assert s.alerts[0]["alert_id"] == active[0].alert_id

    def test_state_repeat_alert_escalates(self):
        from models import MicrogridState
        s = MicrogridState()
        for i in range(3):
            s.add_alert("LOAD_SHEDDING", f"甩负荷{i}")
        active = s.alert_manager.get_active_alerts()
        assert len(active) == 1
        assert active[0].level == WARNING


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
