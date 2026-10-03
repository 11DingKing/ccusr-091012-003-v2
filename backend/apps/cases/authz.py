"""
统一案件授权层

所有保管资源的访问面——列表、详情、导出、操作日志查询——都必须经由本模块判定，
不允许各视图自行拼凑过滤条件，避免某条路径绕过案件范围。

判定规则：
- 管理员（superadmin/admin）不受案件范围限制，但判定仍会记录；
- 普通用户仅当其在某案件上存在 *当前版本* 成员关系、案件处于 active、
  且请求时刻落在授权时间窗内时，才能访问该案件名下资源；
- 一人多案件时可取多个案件范围的并集，跨案件记录互不可见；
- 退出（is_current=False）或时间窗外立即失效，不依赖 JWT 是否过期。
"""
import logging
from dataclasses import dataclass, field

from django.utils import timezone

logger = logging.getLogger('apps')

# 资源类型 -> ORM 上通往案件的查询路径
RESOURCE_CASE_PATH = {
    'goods': 'case',
    'stock_in': 'goods__case',
    'stock_out': 'goods__case',
    'warning': 'goods__case',
    'approval': 'stock_out__goods__case',
    'operation_log': 'case',
}

# 拒绝原因码
NOT_MEMBER = 'not_member'
CASE_INACTIVE = 'case_inactive'
OUT_OF_WINDOW = 'out_of_window'
LEFT_CASE = 'left_case'
NO_ACTIVE_MEMBERSHIP = 'no_active_membership'
OUT_OF_SCOPE = 'out_of_scope'
# 用户的成员关系版本仅存在于已并入本案的源案件上（成员关系不随合并迁移）
MERGED_SOURCE_NOT_MEMBER = 'merged_source_not_member'


class AccessDenied(Exception):
    """统一授权拒绝异常，携带拒绝所依据的成员关系证据。"""

    def __init__(self, reason, *, case=None, membership=None, message='无权访问该案件资源'):
        self.reason = reason
        self.case = case
        self.membership = membership
        super().__init__(message)


@dataclass
class Scope:
    """单个请求的授权范围快照（每次请求实时构建）。"""

    user: object
    at: object
    is_admin: bool = False
    # case_id -> 授予该访问的当前成员关系版本行
    grants: dict = field(default_factory=dict)

    @property
    def case_ids(self):
        return set(self.grants.keys())

    def membership_for(self, case_id):
        return self.grants.get(case_id)

    def to_detail(self):
        return {
            'is_admin': self.is_admin,
            'at': self.at.isoformat(),
            'cases': [
                {'case_id': cid, 'membership_id': m.id, 'membership_version': m.version}
                for cid, m in self.grants.items()
            ],
        }


def build_scope(user, at=None):
    """实时计算用户当前授权范围；退出案件或时间窗结束后下一次请求即收缩。"""
    at = at or timezone.now()
    is_admin = bool(user and user.is_authenticated and user.is_admin)
    scope = Scope(user=user, at=at, is_admin=is_admin)
    if is_admin or not user or not user.is_authenticated:
        return scope

    rows = user.case_memberships.filter(
        is_current=True,
        case__status='active',
        valid_from__lte=at,
    ).select_related('case')
    for row in rows:
        if row.valid_to is not None and at >= row.valid_to:
            continue
        scope.grants[row.case_id] = row
    return scope


def get_scope(request):
    """请求级缓存，保证列表/导出/日志在同一请求内使用同一份范围判定。"""
    scope = getattr(request, '_case_scope', None)
    if scope is None:
        scope = build_scope(request.user)
        request._case_scope = scope
    return scope


def _case_path(resource_type):
    path = RESOURCE_CASE_PATH.get(resource_type)
    if not path:
        raise ValueError(f'未知资源类型: {resource_type}')
    return path + '__in'


def filter_visible(scope, queryset, resource_type):
    """
    统一的列表/导出范围过滤（同一授权判断的集合形态）。

    管理员可见全部；普通用户仅可见授权案件范围内的记录，
    无任何有效成员关系时收缩为空集——绝不放行。
    """
    if scope.is_admin:
        return queryset
    if not scope.grants:
        return queryset.none()
    return queryset.filter(**{_case_path(resource_type): list(scope.case_ids)})


def _evidence_for_case(scope, case):
    """
    找出拒绝某案件访问时最相关的成员关系版本作为证据：
    优先当前不覆盖时间窗的行，其次最新版本（含退出终止行），
    再检查成员关系是否仅挂在已并入本案的源案件上。
    """
    if not case:
        return None, NOT_MEMBER
    membership = case.latest_membership(scope.user)
    if membership is None:
        # 是否仅为已并入本案的源案件成员（成员关系不随合并迁移）
        if case.status == 'active':
            from .models import CaseMembership
            source_membership = CaseMembership.objects.filter(
                user=scope.user, is_current=True,
                case__status='merged', case__merged_into=case,
            ).select_related('case').order_by('-version').first()
            if source_membership:
                return source_membership, MERGED_SOURCE_NOT_MEMBER
        return None, NOT_MEMBER
    if not membership.is_current and membership.change_type == 'leave':
        return membership, LEFT_CASE
    if membership.is_current and not membership.covers(scope.at):
        return membership, OUT_OF_WINDOW
    if case.status != 'active':
        return membership, CASE_INACTIVE
    return membership, OUT_OF_SCOPE


def authorize_object(scope, obj, resource_type, *, case_getter=None):
    """
    统一的详情/单资源判定（同一授权判断的个体形态）。

    通过则返回 obj；否则抛 :class:`AccessDenied`，异常中带证据版本，
    由调用方负责写入 AccessDecisionLog。
    """
    if scope.is_admin:
        return obj
    if case_getter is not None:
        case = case_getter(obj)
    elif resource_type == 'goods':
        case = obj.case
    elif resource_type == 'stock_in':
        case = obj.goods.case
    elif resource_type == 'stock_out':
        case = obj.goods.case
    elif resource_type == 'warning':
        case = obj.goods.case
    elif resource_type == 'approval':
        case = obj.stock_out.goods.case
    else:
        raise ValueError(f'未知资源类型: {resource_type}')
    if case and case.id in scope.grants:
        return obj
    membership, reason = _evidence_for_case(scope, case)
    raise AccessDenied(reason, case=case, membership=membership)


def authorize_case_id(scope, case_id):
    """显式按案件维度的判定（导出 ?case=、日志 ?case= 等）。"""
    if scope.is_admin:
        return
    if case_id in scope.grants:
        return
    from .models import Case
    case = Case.objects.filter(pk=case_id).first()
    membership, reason = _evidence_for_case(scope, case)
    raise AccessDenied(reason, case=case, membership=membership)
