"""
统一资源范围授权服务。

列表、详情、导出、操作日志查询四类访问必须且只能通过本模块做出授权判断，
避免各视图各写一套过滤条件导致绕过（历史问题：临时人员能看到全部记录）。

授权规则（普通成员）：
1. 资源必须归属于一个“办理中”的案件；
2. 访问“当前”必须持有在评估时刻生效的成员关系版本
   （valid_from <= now 且 valid_to 为空或 > now）——退出案件后新版本立即失效，
   新访问立刻被拒绝；
3. 资源业务时间必须落在该用户在该案件任一授权版本的时间窗内，
   即“只访问分配给自己的案件和时间段”；
4. 同时属于多个案件时，可见范围是各案件窗口的并集，互不可见其他案件记录；
5. 案件合并/归档后原案件不再是 active，即使成员关系版本仍开放也拒绝。

管理员（superadmin/admin）不受案件范围限制，但判定同样记录在案。
"""
import logging
from collections import namedtuple

from django.db.models import Q, Exists, OuterRef
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Case, CaseMembership, AccessDecision

logger = logging.getLogger('apps')
security_logger = logging.getLogger('security')

# 各资源类型的案件归属字段（支持经关联表间接归属，如预警 -> 货物 -> 案件）
# 与资源业务时间字段（时间窗按业务时间判定）
RESOURCE_CASE_LOOKUP = {
    'goods': 'case_id',
    'stock_in': 'case_id',
    'stock_out': 'case_id',
    'warning': 'goods__case_id',
    'approval': 'stock_out__case_id',
    'operation_log': 'case_id',
}
RESOURCE_TIME_LOOKUP = {
    'goods': 'created_at',
    'stock_in': 'stock_in_time',
    'stock_out': None,  # stock_out_time 可空，回退 created_at
    'warning': 'created_at',
    'approval': 'created_at',
    'operation_log': 'created_at',
}
# 对象级判定时取案件与业务时间的访问器
RESOURCE_OBJECT_ACCESSORS = {
    'goods': (lambda o: o.case, lambda o: o.created_at),
    'stock_in': (lambda o: o.case, lambda o: o.stock_in_time),
    'stock_out': (lambda o: o.case, lambda o: (o.stock_out_time or o.created_at)),
    'warning': (lambda o: o.goods.case if o.goods_id else None, lambda o: o.created_at),
    'approval': (
        lambda o: o.stock_out.case if o.stock_out_id else None,
        lambda o: o.created_at,
    ),
    'operation_log': (lambda o: o.case, lambda o: o.created_at),
}

# 拒绝理由
REASON_ADMIN = 'admin_bypass'
REASON_NO_CASE = 'case_required'
REASON_CASE_CLOSED = 'case_closed'
REASON_NOT_MEMBER = 'not_member'
REASON_OUTSIDE_WINDOW = 'outside_time_window'

Decision = namedtuple(
    'Decision',
    ['allowed', 'reason', 'case', 'membership', 'action', 'resource_type', 'resource_id'],
)


def membership_snapshot(membership):
    """成员关系版本快照（写入拒绝记录，版本日后被关闭也能还原判定依据）"""
    if membership is None:
        return None
    return {
        'membership_id': membership.id,
        'case_id': membership.case_id,
        'user_id': membership.user_id,
        'version': membership.version,
        'valid_from': membership.valid_from.isoformat() if membership.valid_from else None,
        'valid_to': membership.valid_to.isoformat() if membership.valid_to else None,
        'closed_at': membership.closed_at.isoformat() if membership.closed_at else None,
        'granted_by': membership.granted_by_id,
        'revoked_by': membership.revoked_by_id,
    }


def _resource_time_ref(resource_type):
    """资源业务时间的外层查询表达式（用于子查询关联外层行）"""
    lookup = RESOURCE_TIME_LOOKUP[resource_type]
    if resource_type == 'stock_out':
        return Coalesce(OuterRef('stock_out_time'), OuterRef('created_at'))
    return OuterRef(lookup)


def _resource_case_ref(resource_type):
    """外层行的案件 ID 表达式（支持 goods__case_id 等间接归属）"""
    return OuterRef(RESOURCE_CASE_LOOKUP[resource_type])


class ScopeAuthorizer:
    """对单个访问者在某一评估时刻的授权器"""

    def __init__(self, user, at=None):
        self.user = user
        self.at = at or timezone.now()

    # ---------------- 基础查询 ----------------

    def is_admin(self):
        return bool(self.user and self.user.is_authenticated and self.user.is_admin)

    def effective_case_ids(self):
        """当前时刻持有生效成员关系、且案件仍在办理中的案件 ID 列表"""
        if not self.user or not self.user.is_authenticated:
            return []
        return list(
            CaseMembership.objects.filter(
                user=self.user,
                case__status=Case.STATUS_ACTIVE,
                valid_from__lte=self.at,
            ).filter(
                Q(valid_to__isnull=True) | Q(valid_to__gt=self.at)
            ).values_list('case_id', flat=True).distinct()
        )

    def scoped_case_id_subquery(self):
        return CaseMembership.objects.filter(
            user=self.user,
            case__status=Case.STATUS_ACTIVE,
            valid_from__lte=self.at,
        ).filter(
            Q(valid_to__isnull=True) | Q(valid_to__gt=self.at)
        ).values('case_id')

    def _window_covering_exists(self, resource_type):
        """
        存在一条该用户在该案件的成员关系版本，其时间窗覆盖资源业务时间。
        跨全部版本取并集，因此成员在多个授权时间段内的记录均可见，
        而授权窗口之外的历史记录不可见。
        """
        time_ref = _resource_time_ref(resource_type)
        case_ref = _resource_case_ref(resource_type)
        return Exists(
            CaseMembership.objects.filter(
                user=self.user,
                case_id=case_ref,
                valid_from__lte=time_ref,
            ).filter(
                Q(valid_to__isnull=True) | Q(valid_to__gt=time_ref)
            ).values('pk')
        )

    # ---------------- 列表 / 导出 / 日志查询 ----------------

    def filter_queryset(self, queryset, resource_type):
        """
        统一的列表/导出范围过滤。管理员不过滤；普通成员按
        “当前生效案件 ∩ 资源时间落在授权窗口内”收窄。
        未挂案件的资源对普通成员一律不可见。
        """
        if resource_type not in RESOURCE_CASE_LOOKUP:
            raise ValueError(f'未知资源类型: {resource_type}')
        if self.is_admin():
            return queryset
        if not self.user or not self.user.is_authenticated:
            return queryset.none()

        case_lookup = RESOURCE_CASE_LOOKUP[resource_type]
        scoped = queryset.filter(
            **{f'{case_lookup}__in': self.scoped_case_id_subquery()}
        ).annotate(
            _scope_in_window=self._window_covering_exists(resource_type)
        ).filter(
            _scope_in_window=True
        )

        if resource_type == 'operation_log':
            # 本人历史操作始终可自查（退出案件后其历史操作仍可被审计），
            # 其余日志严格按案件+时间窗过滤，管理员见全部。
            return queryset.filter(Q(pk__in=scoped.values('pk')) | Q(user=self.user)).distinct()
        return scoped

    # ---------------- 详情 / 单对象 ----------------

    def authorize_object(self, obj, resource_type, action='view', request=None, log=True):
        """对象级统一授权判断，返回 Decision 并落 AccessDecision 记录"""
        decision = self._evaluate_object(obj, resource_type, action)
        if log:
            self._record_decision(
                decision, request,
                resource_id=str(getattr(obj, 'pk', '') or ''),
            )
        return decision

    def _evaluate_object(self, obj, resource_type, action):
        obj_id = str(getattr(obj, 'pk', '') or '')
        if self.is_admin():
            case = self._object_case(obj, resource_type)
            return Decision(True, REASON_ADMIN, case, None,
                            action, resource_type, obj_id)

        case = self._object_case(obj, resource_type)
        if case is None:
            return Decision(False, REASON_NO_CASE, None, None,
                            action, resource_type, obj_id)
        if not case.is_open:
            latest = self._latest_version(case)
            return Decision(False, REASON_CASE_CLOSED, case, latest,
                            action, resource_type, obj_id)

        current = case.membership_at(self.user, self.at)
        if current is None:
            # 可能从未加入，也可能已退出：附上最新版本，管理员即可看到
            # “拒绝所依据的成员关系版本”（例如 valid_to 已过）
            latest = self._latest_version(case)
            return Decision(False, REASON_NOT_MEMBER, case, latest,
                            action, resource_type, obj_id)

        record_time = RESOURCE_OBJECT_ACCESSORS[resource_type][1](obj)
        covering = CaseMembership.objects.filter(
            case=case, user=self.user, valid_from__lte=record_time,
        ).filter(
            Q(valid_to__isnull=True) | Q(valid_to__gt=record_time)
        ).order_by('-version').first()
        if covering is None:
            return Decision(False, REASON_OUTSIDE_WINDOW, case, current,
                            action, resource_type, obj_id)

        return Decision(True, 'in_window', case, covering,
                        action, resource_type, obj_id)

    def _object_case(self, obj, resource_type):
        """取资源实例归属的案件（间接归属会沿关联取到案件实例）"""
        accessor = RESOURCE_OBJECT_ACCESSORS.get(resource_type)
        if accessor is None:
            return getattr(obj, 'case', None)
        try:
            return accessor[0](obj)
        except Exception:
            return None

    def _latest_version(self, case):
        return CaseMembership.objects.filter(case=case, user=self.user).order_by('-version').first()

    # ---------------- 写操作授权（案件维度） ----------------

    def authorize_case_action(self, case, action='create', request=None, log=True, resource_id=''):
        """
        新建/操作某案件下资源前的授权：案件办理中、成员关系此刻生效。
        返回 Decision；允许时 decision.membership 即操作时刻的授权版本，
        可写入操作日志实现“历史操作可审计”。
        """
        decision = self._evaluate_case(case, action, resource_id)
        if log:
            self._record_decision(decision, request, resource_id=resource_id)
        return decision

    def _evaluate_case(self, case, action, resource_id):
        if self.is_admin():
            return Decision(True, REASON_ADMIN, case, None, action, 'case', resource_id)
        if case is None:
            return Decision(False, REASON_NO_CASE, None, None, action, 'case', resource_id)
        if not case.is_open:
            return Decision(False, REASON_CASE_CLOSED, case, self._latest_version(case),
                            action, 'case', resource_id)
        current = case.membership_at(self.user, self.at)
        if current is None:
            return Decision(False, REASON_NOT_MEMBER, case, self._latest_version(case),
                            action, 'case', resource_id)
        return Decision(True, 'in_window', case, current, action, 'case', resource_id)

    # ---------------- 判定记录 ----------------

    def record_scope_decision(self, resource_type, action, request, case_ids=None):
        """列表/导出/日志查询等批量访问记一条汇总判定"""
        case_ids = case_ids if case_ids is not None else self.effective_case_ids()
        if self.is_admin():
            allowed, reason = True, REASON_ADMIN
        elif case_ids:
            allowed, reason = True, 'scoped_list'
        else:
            allowed, reason = False, REASON_NOT_MEMBER
        decision = Decision(allowed, reason, None, None, action, resource_type, '')
        self._record_decision(decision, request, extra={
            'scope_case_ids': list(case_ids),
            'evaluated_at': self.at.isoformat(),
        })
        return decision

    def _record_decision(self, decision, request=None, resource_id=None, extra=None):
        try:
            snapshot = membership_snapshot(decision.membership)
            if extra:
                snapshot = {'membership': snapshot, **extra}
            ip = None
            if request is not None:
                ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() \
                    or request.META.get('REMOTE_ADDR')
            AccessDecision.objects.create(
                user=self.user if self.user and self.user.is_authenticated else None,
                decision='allow' if decision.allowed else 'deny',
                action=decision.action,
                resource_type=decision.resource_type,
                resource_id=resource_id if resource_id is not None else decision.resource_id,
                case=decision.case,
                membership=decision.membership,
                membership_snapshot=snapshot,
                reason=decision.reason,
                evaluated_at=self.at,
                ip_address=ip,
            )
            if not decision.allowed:
                security_logger.warning(
                    'Access denied: user=%s resource=%s/%s case=%s reason=%s membership=%s',
                    getattr(self.user, 'id', None), decision.resource_type,
                    resource_id if resource_id is not None else decision.resource_id,
                    decision.case.id if decision.case else None,
                    decision.reason,
                    (decision.membership.id if decision.membership else None),
                )
        except Exception:
            logger.exception('记录访问判定失败，不影响本次授权结果')


def deny_message(reason):
    """拒绝理由 -> 面向调用方的提示语"""
    return {
        REASON_NO_CASE: '该记录未归属案件，无权访问',
        REASON_CASE_CLOSED: '案件已合并或归档，访问已停止',
        REASON_NOT_MEMBER: '您已退出该案件或尚未被授权，访问已停止',
        REASON_OUTSIDE_WINDOW: '该记录不在授权您访问的时间段内',
    }.get(reason, '无权访问该资源')
