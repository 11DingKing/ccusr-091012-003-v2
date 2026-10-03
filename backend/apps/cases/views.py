"""
案件管理视图。

- 案件的创建、成员授权、合并/拆分仅管理员可执行；
- 普通成员只能列出自己当前获授权的案件；
- 访问判定记录（AccessDecision）仅供管理员查询，用于回答
  “某次拒绝访问依据的成员关系版本”。
"""
import logging

from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.response import success_response, error_response
from apps.core.exceptions import BusinessException
from apps.authentication.models import User
from .models import Case, CaseMembership, CaseLineage, AccessDecision
from .authorization import ScopeAuthorizer
from .services import merge_cases, split_case
from .serializers import (
    CaseSerializer, CaseCreateSerializer,
    MembershipSerializer, MembershipGrantSerializer, MembershipRevokeSerializer,
    CaseMergeSerializer, CaseSplitSerializer,
    CaseLineageSerializer, AccessDecisionSerializer,
)

logger = logging.getLogger('apps')


def _is_admin(request):
    return bool(request.user.is_authenticated and request.user.is_admin)


def _first_error(serializer):
    first_error = list(serializer.errors.values())[0]
    if isinstance(first_error, list):
        first_error = first_error[0]
    return str(first_error)


def paginate(request):
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = max(int(request.query_params.get('page_size', 10)), 1)
    return page, page_size, (page - 1) * page_size


class CaseListView(APIView):
    """案件列表：管理员见全部；普通成员仅见当前授权在身的案件"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if _is_admin(request):
            queryset = Case.objects.all().order_by('-created_at')
        else:
            queryset = Case.objects.filter(
                pk__in=ScopeAuthorizer(request.user).scoped_case_id_subquery()
            ).order_by('-created_at')

        status_ = request.query_params.get('status')
        if status_:
            queryset = queryset.filter(status=status_)

        page, page_size, start = paginate(request)
        total = queryset.count()
        serializer = CaseSerializer(queryset[start:start + page_size], many=True)
        return success_response(data={
            'list': serializer.data, 'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        if not _is_admin(request):
            return error_response(message='无权限操作', code=403)
        serializer = CaseCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        case = Case.objects.create(
            name=serializer.validated_data['name'],
            case_no=serializer.validated_data['case_no'],
            created_by=request.user,
        )
        logger.info('User %s created case %s', request.user.username, case.case_no)
        return success_response(data=CaseSerializer(case).data, message='创建成功')


class CaseDetailView(APIView):
    """案件详情：管理员或当前成员可查看"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        case = Case.objects.filter(pk=pk).first()
        if case is None:
            return error_response(message='案件不存在', code=404)
        if not _is_admin(request) and case.id not in ScopeAuthorizer(request.user).effective_case_ids():
            return error_response(message='您无权查看该案件', code=403)
        return success_response(data=CaseSerializer(case).data)


class CaseMembershipView(APIView):
    """案件成员关系：列出全部版本、授予（版本化）、退出（关闭版本）"""
    permission_classes = [IsAuthenticated]

    def get(self, request, case_id):
        case = Case.objects.filter(pk=case_id).first()
        if case is None:
            return error_response(message='案件不存在', code=404)
        if not _is_admin(request) and case.id not in ScopeAuthorizer(request.user).effective_case_ids():
            return error_response(message='您无权查看该案件成员', code=403)
        versions = case.memberships.select_related('user', 'granted_by', 'revoked_by').order_by(
            'user_id', '-version'
        )
        user_id = request.query_params.get('user_id')
        if user_id:
            versions = versions.filter(user_id=user_id)
        page, page_size, start = paginate(request)
        total = versions.count()
        return success_response(data={
            'list': MembershipSerializer(versions[start:start + page_size], many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request, case_id):
        """授予/调整成员关系（新开版本，旧版本保留）"""
        if not _is_admin(request):
            return error_response(message='无权限操作', code=403)
        case = Case.objects.filter(pk=case_id).first()
        if case is None:
            return error_response(message='案件不存在', code=404)
        if not case.is_open:
            return error_response(message='案件已合并或归档，无法调整成员')

        serializer = MembershipGrantSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        data = serializer.validated_data
        target_user = User.objects.get(pk=data['user_id'])
        membership = case.grant_membership(
            target_user,
            valid_from=data['valid_from'],
            valid_to=data.get('valid_to'),
            granted_by=request.user,
        )
        logger.info(
            'User %s granted case %s membership v%s to user %s',
            request.user.username, case.case_no, membership.version, target_user.username,
        )
        return success_response(data=MembershipSerializer(membership).data, message='授权成功')

    def delete(self, request, case_id):
        """人员退出案件：关闭开放版本，立即生效，历史版本保留"""
        if not _is_admin(request):
            return error_response(message='无权限操作', code=403)
        case = Case.objects.filter(pk=case_id).first()
        if case is None:
            return error_response(message='案件不存在', code=404)

        serializer = MembershipRevokeSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        data = serializer.validated_data
        target_user = User.objects.filter(pk=data['user_id']).first()
        if target_user is None:
            return error_response(message='用户不存在', code=404)

        closed = case.revoke_membership(
            target_user,
            effective_at=data.get('effective_at'),
            revoked_by=request.user,
        )
        if not closed:
            return error_response(message='该用户当前没有生效中的成员关系')
        logger.info(
            'User %s revoked case %s membership of user %s (%s versions closed)',
            request.user.username, case.case_no, target_user.username, closed,
        )
        return success_response(message=f'已退出案件，关闭 {closed} 个生效版本')


class CaseMergeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _is_admin(request):
            return error_response(message='无权限操作', code=403)
        serializer = CaseMergeSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        try:
            target, _ = merge_cases(
                serializer.validated_data['source_case_ids'],
                serializer.validated_data['target_case_id'],
                operated_by=request.user,
                remark=serializer.validated_data.get('remark', ''),
            )
        except BusinessException as exc:
            return error_response(message=exc.message, code=exc.code)
        return success_response(data=CaseSerializer(target).data, message='合并成功')


class CaseSplitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _is_admin(request):
            return error_response(message='无权限操作', code=403)
        serializer = CaseSplitSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data
        try:
            _, created, _ = split_case(
                data['source_case_id'],
                data['targets'],
                operated_by=request.user,
                archive_source=data.get('archive_source', False),
                remark=data.get('remark', ''),
            )
        except BusinessException as exc:
            return error_response(message=exc.message, code=exc.code)
        return success_response(data=CaseSerializer(created, many=True).data, message='拆分成功')


class CaseLineageView(APIView):
    """案件合并/拆分谱系（管理员见全部；成员仅见与自己曾参与案件相关的谱系）"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = CaseLineage.objects.select_related('source_case', 'target_case')
        if not _is_admin(request):
            case_ids = list(
                CaseMembership.objects.filter(user=request.user)
                .values_list('case_id', flat=True).distinct()
            )
            if not case_ids:
                queryset = queryset.none()
            else:
                queryset = queryset.filter(
                    Q(source_case_id__in=case_ids) | Q(target_case_id__in=case_ids)
                )
        page, page_size, start = paginate(request)
        total = queryset.count()
        return success_response(data={
            'list': CaseLineageSerializer(queryset[start:start + page_size], many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })


class AccessDecisionListView(APIView):
    """访问判定记录（仅管理员）：用于审计每次允许/拒绝及其成员关系版本"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request):
            return error_response(message='无权限访问', code=403)

        queryset = AccessDecision.objects.select_related('user', 'case', 'membership').all()
        decision = request.query_params.get('decision')
        if decision in ('allow', 'deny'):
            queryset = queryset.filter(decision=decision)
        user_id = request.query_params.get('user')
        if user_id:
            queryset = queryset.filter(user_id=user_id)
        case_id = request.query_params.get('case')
        if case_id:
            queryset = queryset.filter(case_id=case_id)
        resource_type = request.query_params.get('resource_type')
        if resource_type:
            queryset = queryset.filter(resource_type=resource_type)

        page, page_size, start = paginate(request)
        total = queryset.count()
        return success_response(data={
            'list': AccessDecisionSerializer(queryset[start:start + page_size], many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })
