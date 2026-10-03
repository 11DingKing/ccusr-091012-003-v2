"""
视图层与统一授权层之间的桥接

任何保管资源视图都通过这里完成“取范围 → 过滤/判定 → 记录判定日志”，
保证列表、详情、导出、操作日志查询是同一套授权判断。
"""
from apps.core.response import error_response
from apps.core.logging_config import get_request_id
from . import authz
from .audit import log_decision


def scope_for(request):
    return authz.get_scope(request)


def mark_audit_case(request, case_id):
    """
    把新建资源的案件归属标注给操作日志中间件。

    视图内的 request 是 DRF 包装对象，属性必须挂到其底层 Django request，
    否则中间件读取不到。
    """
    underlying = getattr(request, '_request', request)
    underlying._audit_case_id = case_id


def visible_queryset(request, queryset, resource_type, *, action='list'):
    """列表/导出：按当前授权范围收窄查询集，并记录一次 allow 判定。"""
    scope = authz.get_scope(request)
    qs = authz.filter_visible(scope, queryset, resource_type)
    # 同一请求内相同访问面只记一次判定（仪表盘会对多类资源分别过滤）
    logged = getattr(request, '_decisions_logged', None)
    if logged is None:
        logged = set()
        request._decisions_logged = logged
    key = ('allow', action, resource_type)
    if key not in logged:
        logged.add(key)
        log_decision(
            request, scope, decision='allow', action=action,
            resource_type=resource_type,
            detail='scoped' if not scope.is_admin else 'admin_unrestricted',
        )
    return qs


def guard_case_param(request, resource_type, *, action='list'):
    """
    处理显式 ?case= 参数：成员只能限定在自己的授权案件内。
    返回 (scope, case_id, error_response)；error_response 非 None 时调用方直接返回。
    """
    scope = authz.get_scope(request)
    case_id = request.query_params.get('case')
    if case_id:
        try:
            case_id = int(case_id)
        except (TypeError, ValueError):
            return scope, None, error_response(message='案件参数无效', code=400)
        try:
            authz.authorize_case_id(scope, case_id)
        except authz.AccessDenied as denied:
            log_decision(
                request, scope, decision='deny', action=action,
                resource_type=resource_type, denied=denied,
                detail='explicit case parameter out of scope')
            return scope, None, _deny_http_response(denied)
    return scope, case_id, None


def get_or_deny(request, scope, obj, resource_type, *, action='detail', case_getter=None):
    """
    详情：通过返回 (obj, None)；拒绝返回 (None, response)，
    同时落拒绝判定日志（含成员关系版本证据）。
    """
    try:
        authz.authorize_object(scope, obj, resource_type, case_getter=case_getter)
    except authz.AccessDenied as denied:
        log_decision(
            request, scope, decision='deny', action=action,
            resource_type=resource_type,
            resource_id=getattr(obj, 'pk', ''), denied=denied)
        return None, _deny_http_response(denied)
    log_decision(
        request, scope, decision='allow', action=action,
        resource_type=resource_type, resource_id=getattr(obj, 'pk', ''),
        case=_case_of(obj, resource_type, case_getter))
    return obj, None


def _case_of(obj, resource_type, case_getter):
    if case_getter is not None:
        return case_getter(obj)
    if resource_type == 'goods':
        return obj.case
    if resource_type in ('stock_in', 'stock_out', 'warning'):
        return obj.goods.case
    if resource_type == 'approval':
        return obj.stock_out.goods.case
    return None


def _deny_http_response(denied):
    data = {'request_id': get_request_id() or '', 'reason': denied.reason}
    if denied.membership is not None:
        data['membership_version'] = denied.membership.version
        data['membership_id'] = denied.membership.id
    if denied.case is not None:
        data['case_id'] = denied.case.id
        data['case_version'] = denied.case.version
    return error_response(message='无权访问该案件资源', code=403, data=data)
