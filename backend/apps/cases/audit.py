"""
授权判定审计

每一次列表、详情、导出与操作日志查询的允许/拒绝都写入 AccessDecisionLog；
拒绝记录固化当时所依据的成员关系版本外键与快照，管理员可按版本追溯。
"""
import logging

from apps.core.logging_config import get_request_id

logger = logging.getLogger('apps')


def _client_ip(request):
    if request is None:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def log_decision(request, scope, *, decision, action, resource_type,
                 resource_id='', case=None, denied=None, membership=None,
                 reason='', detail=''):
    """
    记录一条授权判定。审计写入失败不影响业务响应本身，
    但会输出 error 日志，避免“悄悄不审计”。
    """
    from .models import AccessDecisionLog

    if denied is not None:
        case = denied.case
        membership = denied.membership
        reason = denied.reason

    try:
        AccessDecisionLog.objects.create(
            user=scope.user if scope.user and scope.user.is_authenticated else None,
            case=case,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id or '')[:64],
            decision=decision,
            reason=reason or '',
            membership_version=membership,
            membership_version_number=membership.version if membership else None,
            membership_snapshot=membership.snapshot() if membership else None,
            case_version=case.version if case else None,
            request_id=get_request_id() or '',
            path=request.path if request is not None else '',
            method=request.method if request is not None else '',
            ip_address=_client_ip(request),
            detail=(detail or '')[:500],
        )
    except Exception:
        logger.error(
            '写入授权判定日志失败 user=%s action=%s resource=%s:%s decision=%s',
            getattr(scope.user, 'id', None), action, resource_type, resource_id,
            decision, exc_info=True,
        )
