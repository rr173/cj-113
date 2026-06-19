from __future__ import annotations
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict
import uuid


INFO = "INFO"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
LEVEL_ORDER = {INFO: 0, WARNING: 1, CRITICAL: 2}
LEVEL_TEXT = {INFO: "普通", WARNING: "警告", CRITICAL: "紧急"}

REPEAT_WINDOW_MINUTES = 5
REPEAT_THRESHOLD = 3
WARNING_ACK_TIMEOUT_MINUTES = 10

NOTIFICATION_STATUS_PENDING = "pending"
NOTIFICATION_STATUS_SENT = "sent"
NOTIFICATION_STATUS_UNATTENDED = "unattended"
NOTIFICATION_STATUS_CANCELLED = "cancelled"


@dataclass
class EscalationRecord:
    from_level: str
    to_level: str
    reason: str
    timestamp: datetime


@dataclass
class Alert:
    alert_id: str
    alert_type: str
    message: str
    level: str
    created_at: datetime
    data: Dict[str, Any] = field(default_factory=dict)
    acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    escalation_history: List[EscalationRecord] = field(default_factory=list)
    last_occurrence_at: datetime = field(default_factory=lambda: datetime.now())
    occurrence_count: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "alert_type": self.alert_type,
            "message": self.message,
            "level": self.level,
            "level_text": LEVEL_TEXT.get(self.level, "未知"),
            "created_at": self.created_at.isoformat(),
            "last_occurrence_at": self.last_occurrence_at.isoformat(),
            "occurrence_count": self.occurrence_count,
            "acknowledged": self.acknowledged,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "acknowledged_by": self.acknowledged_by,
            "data": self.data,
            "escalation_history": [
                {
                    "from_level": h.from_level,
                    "from_level_text": LEVEL_TEXT.get(h.from_level, "未知"),
                    "to_level": h.to_level,
                    "to_level_text": LEVEL_TEXT.get(h.to_level, "未知"),
                    "reason": h.reason,
                    "timestamp": h.timestamp.isoformat(),
                }
                for h in self.escalation_history
            ],
        }


@dataclass
class DutyStaff:
    staff_id: str
    name: str
    contact: str
    start_hour: int
    end_hour: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "staff_id": self.staff_id,
            "name": self.name,
            "contact": self.contact,
            "start_hour": self.start_hour,
            "end_hour": self.end_hour,
            "shift": f"{self.start_hour:02d}:00 - {self.end_hour:02d}:00",
        }


@dataclass
class Notification:
    notification_id: str
    alert_id: str
    alert_summary: str
    escalation_path: str
    suggested_action: str
    created_at: datetime
    status: str = NOTIFICATION_STATUS_PENDING
    sent_at: Optional[datetime] = None
    sent_to: Optional[str] = None
    retry_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancelled_by: Optional[str] = None
    cancel_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        status_text = {
            NOTIFICATION_STATUS_PENDING: "待发送",
            NOTIFICATION_STATUS_SENT: "已发送",
            NOTIFICATION_STATUS_UNATTENDED: "无人接收",
            NOTIFICATION_STATUS_CANCELLED: "已取消",
        }
        return {
            "notification_id": self.notification_id,
            "alert_id": self.alert_id,
            "alert_summary": self.alert_summary,
            "escalation_path": self.escalation_path,
            "suggested_action": self.suggested_action,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "status_text": status_text.get(self.status, "未知"),
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "sent_to": self.sent_to,
            "retry_at": self.retry_at.isoformat() if self.retry_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "cancelled_by": self.cancelled_by,
            "cancel_reason": self.cancel_reason,
        }


SUGGESTED_ACTIONS: Dict[str, str] = {
    "LOAD_SHEDDING": "检查负荷配置，评估甩负荷影响，尽快恢复重要负荷供电",
    "BATTERY_LIFE_WARNING": "安排电池维护计划，评估是否需要更换电池",
    "BATTERY_RESISTANCE_ALERT": "立即检查电池内阻，安排电池健康检测",
    "STORAGE_PLAN_SUSPENDED": "检查电池SOC，恢复后手动恢复储能计划",
    "STORAGE_PLAN_RESUMED": "确认储能计划恢复正常执行",
    "SOURCE_WARNING": "检查发电源运行状态，预防进一步恶化",
    "SOURCE_DANGER": "立即处理发电源故障，检查备份预案",
    "SOURCE_UNEXPECTED_FAULT": "紧急处理发电源掉线，启动备份预案",
    "SOURCE_MAINTENANCE_START": "记录维护开始，跟进维护进度",
    "SOURCE_MAINTENANCE_END": "确认维护完成，验证设备恢复正常",
    "DIESEL_STARTUP": "确认柴油机启动正常，监控燃油消耗",
    "DIESEL_RUNTIME_EXCEEDED": "安排柴油机停机冷却，检查维护状态",
    "GRID_IMPORT_HIGH": "检查电价策略，评估储能放电或发电切换可行性",
    "DISPATCH_ERROR": "检查调度逻辑，修复异常配置",
}


def _default_suggested_action(alert_type: str, level: str) -> str:
    if alert_type in SUGGESTED_ACTIONS:
        return SUGGESTED_ACTIONS[alert_type]
    if level == CRITICAL:
        return f"紧急处理{alert_type}告警，检查系统状态"
    elif level == WARNING:
        return f"关注{alert_type}告警，排查潜在问题"
    else:
        return f"留意{alert_type}告警，持续观察系统状态"


class AlertManager:
    def __init__(self):
        self._alerts: Dict[str, Alert] = {}
        self._alert_counter: int = 0
        self._type_recent_occurrences: Dict[str, List[datetime]] = defaultdict(list)
        self._active_alerts_by_type: Dict[str, Alert] = {}

        self._duty_staff: Dict[str, DutyStaff] = {}
        self._duty_counter: int = 0

        self._notifications: List[Notification] = []
        self._notification_counter: int = 0

    def _generate_alert_id(self) -> str:
        self._alert_counter += 1
        return f"ALT-{self._alert_counter:08d}"

    def _generate_staff_id(self) -> str:
        self._duty_counter += 1
        return f"DS-{self._duty_counter:04d}"

    def _generate_notification_id(self) -> str:
        self._notification_counter += 1
        return f"NOT-{self._notification_counter:08d}"

    def report_alert(self, alert_type: str, message: str, data: Dict[str, Any] = None,
                     now: datetime = None) -> Alert:
        if now is None:
            now = datetime.now()

        data = data or {}

        existing = self._active_alerts_by_type.get(alert_type)
        if existing and not existing.acknowledged:
            existing.occurrence_count += 1
            existing.last_occurrence_at = now
            if existing.data != data:
                existing.data.update(data)
            self._track_occurrence(alert_type, now)
            self._check_repeat_escalation(existing, now)
            return existing

        alert_id = self._generate_alert_id()
        alert = Alert(
            alert_id=alert_id,
            alert_type=alert_type,
            message=message,
            level=INFO,
            created_at=now,
            data=data,
            last_occurrence_at=now,
            occurrence_count=1,
        )
        self._alerts[alert_id] = alert
        self._active_alerts_by_type[alert_type] = alert
        self._track_occurrence(alert_type, now)
        self._check_repeat_escalation(alert, now)
        return alert

    def _track_occurrence(self, alert_type: str, now: datetime):
        occurrences = self._type_recent_occurrences[alert_type]
        occurrences.append(now)
        cutoff = now - timedelta(minutes=REPEAT_WINDOW_MINUTES)
        self._type_recent_occurrences[alert_type] = [
            t for t in occurrences if t >= cutoff
        ]

    def _check_repeat_escalation(self, alert: Alert, now: datetime):
        if alert.acknowledged:
            return
        if alert.level != INFO:
            return
        recent = self._type_recent_occurrences[alert.alert_type]
        if len(recent) >= REPEAT_THRESHOLD:
            self._escalate_alert(
                alert, WARNING,
                f"{alert.alert_type}在{REPEAT_WINDOW_MINUTES}分钟内出现{len(recent)}次，自动升级为警告",
                now,
            )

    def _escalate_alert(self, alert: Alert, to_level: str, reason: str, now: datetime):
        if alert.acknowledged:
            return
        if LEVEL_ORDER.get(to_level, -1) <= LEVEL_ORDER.get(alert.level, -1):
            return

        record = EscalationRecord(
            from_level=alert.level,
            to_level=to_level,
            reason=reason,
            timestamp=now,
        )
        alert.escalation_history.append(record)
        alert.level = to_level

        if to_level == CRITICAL:
            self._trigger_critical_notification(alert, now)

    def check_time_based_escalation(self, now: datetime = None) -> List[Alert]:
        if now is None:
            now = datetime.now()
        escalated = []
        for alert in self._alerts.values():
            if alert.acknowledged:
                continue
            if alert.level == WARNING:
                time_since_warning = None
                for h in reversed(alert.escalation_history):
                    if h.to_level == WARNING:
                        time_since_warning = now - h.timestamp
                        break
                if time_since_warning is None:
                    continue
                if time_since_warning >= timedelta(minutes=WARNING_ACK_TIMEOUT_MINUTES):
                    self._escalate_alert(
                        alert, CRITICAL,
                        f"警告级告警持续{time_since_warning.total_seconds()/60:.0f}分钟未确认，自动升级为紧急",
                        now,
                    )
                    escalated.append(alert)
        return escalated

    def _trigger_critical_notification(self, alert: Alert, now: datetime):
        escalation_path_parts = [LEVEL_TEXT[INFO]]
        for h in alert.escalation_history:
            escalation_path_parts.append(LEVEL_TEXT[h.to_level])
        escalation_path = " → ".join(escalation_path_parts)

        notification = Notification(
            notification_id=self._generate_notification_id(),
            alert_id=alert.alert_id,
            alert_summary=f"[{LEVEL_TEXT[CRITICAL]}] {alert.alert_type}: {alert.message}",
            escalation_path=escalation_path,
            suggested_action=_default_suggested_action(alert.alert_type, CRITICAL),
            created_at=now,
            status=NOTIFICATION_STATUS_PENDING,
        )
        self._notifications.append(notification)
        self._dispatch_notification(notification, now)

    def _cancel_pending_notifications_for_alert(self, alert_id: str, cancelled_by: str,
                                                 now: datetime):
        for n in self._notifications:
            if n.alert_id != alert_id:
                continue
            if n.status in (NOTIFICATION_STATUS_SENT, NOTIFICATION_STATUS_CANCELLED):
                continue
            n.status = NOTIFICATION_STATUS_CANCELLED
            n.cancelled_at = now
            n.cancelled_by = cancelled_by or "系统"
            n.cancel_reason = "关联告警已被确认处理，通知取消"
            n.retry_at = None

    def acknowledge_alert(self, alert_id: str, acknowledged_by: str = None,
                          now: datetime = None) -> bool:
        if now is None:
            now = datetime.now()
        alert = self._alerts.get(alert_id)
        if alert is None or alert.acknowledged:
            return False
        alert.acknowledged = True
        alert.acknowledged_at = now
        alert.acknowledged_by = acknowledged_by
        if alert.alert_type in self._active_alerts_by_type:
            if self._active_alerts_by_type[alert.alert_type].alert_id == alert_id:
                del self._active_alerts_by_type[alert.alert_type]
        self._cancel_pending_notifications_for_alert(alert_id, acknowledged_by, now)
        return True

    def acknowledge_alerts_by_type(self, alert_type: str, acknowledged_by: str = None,
                                    now: datetime = None) -> int:
        if now is None:
            now = datetime.now()
        count = 0
        for alert in self._alerts.values():
            if alert.alert_type == alert_type and not alert.acknowledged:
                alert.acknowledged = True
                alert.acknowledged_at = now
                alert.acknowledged_by = acknowledged_by
                self._cancel_pending_notifications_for_alert(
                    alert.alert_id, acknowledged_by, now
                )
                count += 1
        if alert_type in self._active_alerts_by_type:
            del self._active_alerts_by_type[alert_type]
        return count

    def get_active_alerts(self, level: str = None, sort_by_time: bool = True) -> List[Alert]:
        alerts = [a for a in self._alerts.values() if not a.acknowledged]
        if level:
            alerts = [a for a in alerts if a.level == level]
        if sort_by_time:
            alerts.sort(key=lambda a: a.last_occurrence_at, reverse=True)
        return alerts

    def get_alert(self, alert_id: str) -> Optional[Alert]:
        return self._alerts.get(alert_id)

    def get_alert_escalation_history(self, alert_id: str) -> Optional[List[Dict[str, Any]]]:
        alert = self._alerts.get(alert_id)
        if alert is None:
            return None
        timeline = []
        timeline.append({
            "timestamp": alert.created_at.isoformat(),
            "event": "告警产生",
            "level": alert.level if not alert.escalation_history else INFO,
            "level_text": LEVEL_TEXT[alert.level] if not alert.escalation_history else LEVEL_TEXT[INFO],
            "details": f"初始级别为{LEVEL_TEXT[INFO] if not alert.escalation_history else LEVEL_TEXT[alert.level]}",
        })
        for h in alert.escalation_history:
            timeline.append({
                "timestamp": h.timestamp.isoformat(),
                "event": "级别升级",
                "level": h.to_level,
                "level_text": LEVEL_TEXT.get(h.to_level, "未知"),
                "details": f"从{LEVEL_TEXT.get(h.from_level, '未知')}升级至{LEVEL_TEXT.get(h.to_level, '未知')}: {h.reason}",
            })
        if alert.acknowledged:
            timeline.append({
                "timestamp": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
                "event": "告警确认",
                "level": alert.level,
                "level_text": LEVEL_TEXT.get(alert.level, "未知"),
                "details": f"由 {alert.acknowledged_by or '系统'} 确认处理",
            })
        timeline.sort(key=lambda x: x["timestamp"])
        return timeline

    def add_duty_staff(self, name: str, contact: str, start_hour: int, end_hour: int) -> DutyStaff:
        if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
            raise ValueError("值班时段小时必须在0-23之间")
        staff_id = self._generate_staff_id()
        staff = DutyStaff(
            staff_id=staff_id,
            name=name,
            contact=contact,
            start_hour=start_hour,
            end_hour=end_hour,
        )
        self._duty_staff[staff_id] = staff
        return staff

    def update_duty_staff(self, staff_id: str, name: str = None, contact: str = None,
                           start_hour: int = None, end_hour: int = None) -> bool:
        staff = self._duty_staff.get(staff_id)
        if staff is None:
            return False
        if name is not None:
            staff.name = name
        if contact is not None:
            staff.contact = contact
        if start_hour is not None:
            if not (0 <= start_hour <= 23):
                raise ValueError("值班时段小时必须在0-23之间")
            staff.start_hour = start_hour
        if end_hour is not None:
            if not (0 <= end_hour <= 23):
                raise ValueError("值班时段小时必须在0-23之间")
            staff.end_hour = end_hour
        return True

    def delete_duty_staff(self, staff_id: str) -> bool:
        if staff_id in self._duty_staff:
            del self._duty_staff[staff_id]
            return True
        return False

    def list_duty_staff(self) -> List[DutyStaff]:
        return list(self._duty_staff.values())

    def get_duty_staff(self, staff_id: str) -> Optional[DutyStaff]:
        return self._duty_staff.get(staff_id)

    def is_on_duty(self, staff: DutyStaff, now: datetime = None) -> bool:
        if now is None:
            now = datetime.now()
        hour = now.hour
        if staff.start_hour == staff.end_hour:
            return True
        if staff.start_hour < staff.end_hour:
            return staff.start_hour <= hour < staff.end_hour
        else:
            return hour >= staff.start_hour or hour < staff.end_hour

    def get_on_duty_staff(self, now: datetime = None) -> List[DutyStaff]:
        if now is None:
            now = datetime.now()
        return [s for s in self._duty_staff.values() if self.is_on_duty(s, now)]

    def get_next_on_duty_time(self, now: datetime = None) -> Optional[datetime]:
        if now is None:
            now = datetime.now()
        current_on_duty = self.get_on_duty_staff(now)
        if current_on_duty:
            return now
        next_time = None
        for hour in range(1, 49):
            check_time = now + timedelta(hours=hour)
            if self.get_on_duty_staff(check_time):
                next_time = check_time.replace(minute=0, second=0, microsecond=0)
                break
        return next_time

    def _dispatch_notification(self, notification: Notification, now: datetime):
        on_duty = self.get_on_duty_staff(now)
        if on_duty:
            notification.status = NOTIFICATION_STATUS_SENT
            notification.sent_at = now
            notification.sent_to = ",".join(s.name for s in on_duty)
        else:
            notification.status = NOTIFICATION_STATUS_UNATTENDED
            next_time = self.get_next_on_duty_time(now)
            notification.retry_at = next_time

    def process_pending_notifications(self, now: datetime = None):
        if now is None:
            now = datetime.now()
        for n in self._notifications:
            if n.status == NOTIFICATION_STATUS_UNATTENDED and n.retry_at:
                if now >= n.retry_at:
                    alert = self._alerts.get(n.alert_id)
                    if alert and alert.acknowledged:
                        n.status = NOTIFICATION_STATUS_CANCELLED
                        n.cancelled_at = now
                        n.cancelled_by = "系统"
                        n.cancel_reason = "重试发送时发现关联告警已被确认，通知取消"
                        n.retry_at = None
                        continue
                    on_duty = self.get_on_duty_staff(now)
                    if on_duty:
                        n.status = NOTIFICATION_STATUS_SENT
                        n.sent_at = now
                        n.sent_to = ",".join(s.name for s in on_duty)
                        n.retry_at = None
                    else:
                        n.retry_at = self.get_next_on_duty_time(now)

    def get_notifications(self, status: str = None) -> List[Notification]:
        result = list(self._notifications)
        if status:
            result = [n for n in result if n.status == status]
        result.sort(key=lambda n: n.created_at, reverse=True)
        return result

    def get_alert_statistics(self, now: datetime = None) -> Dict[str, Any]:
        if now is None:
            now = datetime.now()
        by_type_level: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        by_type_total: Dict[str, int] = defaultdict(int)
        by_level_total: Dict[str, int] = defaultdict(int)
        active_count = 0
        acknowledged_count = 0
        critical_count = 0

        last_24h_start = now - timedelta(hours=24)
        hourly_trend: Dict[int, int] = defaultdict(int)
        last_24h_total = 0

        for alert in self._alerts.values():
            type_key = alert.alert_type
            level_key = alert.level
            by_type_level[type_key][level_key] += 1
            by_type_total[type_key] += 1
            by_level_total[level_key] += 1

            if alert.acknowledged:
                acknowledged_count += 1
            else:
                active_count += 1
                if alert.level == CRITICAL:
                    critical_count += 1

            if alert.created_at >= last_24h_start:
                last_24h_total += 1
                hour_key = alert.created_at.hour
                hourly_trend[hour_key] += 1

        trend_24h = []
        for h in range(24):
            trend_24h.append({
                "hour": h,
                "hour_label": f"{h:02d}:00",
                "count": hourly_trend.get(h, 0),
            })

        type_summary = []
        for t in sorted(by_type_total.keys()):
            type_summary.append({
                "alert_type": t,
                "total": by_type_total[t],
                "info": by_type_level[t][INFO],
                "warning": by_type_level[t][WARNING],
                "critical": by_type_level[t][CRITICAL],
            })

        return {
            "total_alerts": len(self._alerts),
            "active_count": active_count,
            "acknowledged_count": acknowledged_count,
            "critical_active_count": critical_count,
            "by_level": {
                INFO: by_level_total[INFO],
                WARNING: by_level_total[WARNING],
                CRITICAL: by_level_total[CRITICAL],
            },
            "by_type": type_summary,
            "last_24h_total": last_24h_total,
            "last_24h_trend": trend_24h,
            "query_time": now.isoformat(),
        }
