"""
案件管理视图

案件与成员关系的写操作仅管理员可执行；资源访问面（列表/详情/导出/日志）
的范围判定在 warehouse、reports、authentication 各视图内统一调用
``apps.cases.authz`` 完成。
"""
import logging

from django.db import transaction
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.response import success_response, error_response
from apps.authentication.models import User
from .models import AccessDecisionLog, Case
from .serializers import (
    AccessDecisionLogSerializer, CaseCreateSerializer, CaseEventSerializer,
    CaseMembershipSerializer, CaseSerializer, MembershipLeaveSerializer,
    MembershipWriteSerializer, MergeSerializer, SplitSerializer,
)
from . import services

logger = logging.getLogger('apps')


def _require_admin(request):
    if not request.user.is_admin:
        return error_response(message='无权限操作，仅管理员可管理案件与成员关系', code=403)
    return None


def _paginate(request, queryset):
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = max(int(request.query_params.get('page_size', 10)), 1)
    start = (page - 1) * page_size
    return queryset.count(), queryset[start:start + page_size], page, page_size


class CaseListView(APIView):
    """案件列表/创建。普通用户仅能看到自己当前所属的案件。"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Case.objects.all().select_related('created_by', 'merged_into')
        if not request.user.is_admin:
            # 成员仅可见自己当前所属且仍在办理中的案件
            queryset = queryset.filter(
                status='active',
                membership_versions__user=request.user,
                membership_versions__is_current=True,
            ).distinct()

        status_param = request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)
        keyword = request.query_params.get('keyword')
        if keyword:
            queryset = queryset.filter(name__icontains=keyword) | \
                queryset.filter(case_no__icontains=keyword)

        total, rows, page, page_size = _paginate(request, queryset.order_by('-created_at'))
        return success_response(data={
            'list': CaseSerializer(rows, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        denied = _require_admin(request)
        if denied:
            return denied
        serializer = CaseCreateSerializer(data=request.data)
        if not serializer.is_valid():
            first = list(serializer.errors.values())[0]
            return error_response(message=str(first[0] if isinstance(first, list) else first))

        data = serializer.validated_data
        member_specs = []
        if data.get('member_user_ids'):
            users = User.objects.filter(pk__in=data['member_user_ids'])
            if users.count() != len(set(data['member_user_ids'])):
                return error_response(message='存在不存在的用户')
            for user in users:
                member_specs.append({
                    'user': user,
                    'valid_from': data.get('valid_from'),
                    'valid_to': data.get('valid_to'),
                })

        case = services.create_case(
            name=data['name'], case_no=data['case_no'],
            actor=request.user, remark=data.get('remark', ''),
            member_specs=member_specs)
        logger.info('User %s created case %s', request.user.username, case.case_no)
        return success_response(data=CaseSerializer(case).data, message='创建成功')


class CaseDetailView(APIView):
    """案件详情（含版本与合并去向）。"""
    permission_classes = [IsAuthenticated]

    def _get_visible_case(self, request, pk):
        case = Case.objects.filter(pk=pk).first()
        if not case:
            return None, error_response(message='案件不存在', code=404)
        if request.user.is_admin:
            return case, None
        if case.status == 'active' and case.membership_versions.filter(
                user=request.user, is_current=True).exists():
            return case, None
        return None, error_response(message='无权查看该案件', code=403)

    def get(self, request, pk):
        case, denied = self._get_visible_case(request, pk)
        if denied:
            return denied
        data = CaseSerializer(case).data
        data['current_members'] = CaseMembershipSerializer(
            case.membership_versions.filter(is_current=True).select_related('user', 'case'),
            many=True).data
        return success_response(data=data)


class CaseMembershipListView(APIView):
    """查看案件成员关系版本链（管理员看全部版本，成员只看自己）。"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        case = Case.objects.filter(pk=pk).first()
        if not case:
            return error_response(message='案件不存在', code=404)
        queryset = case.membership_versions.select_related('user', 'case', 'created_by')
        if not request.user.is_admin:
            own = queryset.filter(
                user=request.user, is_current=True).exists() and case.status == 'active'
            if not own:
                return error_response(message='无权查看该案件成员关系', code=403)
            queryset = queryset.filter(user=request.user)

        total, rows, page, page_size = _paginate(request, queryset.order_by('-version', '-id'))
        return success_response(data={
            'list': CaseMembershipSerializer(rows, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })


class CaseMemberJoinView(APIView):
    """加入成员（管理员）。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _require_admin(request)
        if denied:
            return denied
        case = Case.objects.filter(pk=pk).first()
        if not case:
            return error_response(message='案件不存在', code=404)
        if case.status != 'active':
            return error_response(message='案件已结束或已合并，不能新增成员')

        serializer = MembershipWriteSerializer(data=request.data)
        if not serializer.is_valid():
            first = list(serializer.errors.values())[0]
            return error_response(message=str(first[0] if isinstance(first, list) else first))
        data = serializer.validated_data
        user = User.objects.get(pk=data['user_id'])
        try:
            membership = case.add_member(
                user, valid_from=data.get('valid_from'), valid_to=data.get('valid_to'),
                role=data.get('role', 'member'), actor=request.user,
                reason=data.get('reason', ''))
        except ValueError as exc:
            return error_response(message=str(exc))
        logger.info('User %s joined user %s to case %s',
                    request.user.username, user.username, case.case_no)
        return success_response(data=CaseMembershipSerializer(membership).data, message='已加入案件')


class CaseMemberAdjustView(APIView):
    """调整成员时间窗/角色（管理员）：追加新版本。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _require_admin(request)
        if denied:
            return denied
        case = Case.objects.filter(pk=pk).first()
        if not case:
            return error_response(message='案件不存在', code=404)

        serializer = MembershipWriteSerializer(data=request.data)
        if not serializer.is_valid():
            first = list(serializer.errors.values())[0]
            return error_response(message=str(first[0] if isinstance(first, list) else first))
        data = serializer.validated_data
        user = User.objects.get(pk=data['user_id'])
        try:
            membership = case.adjust_member(
                user, valid_from=data.get('valid_from'), valid_to=data.get('valid_to'),
                role=data.get('role'), actor=request.user,
                reason=data.get('reason', ''))
        except ValueError as exc:
            return error_response(message=str(exc))
        return success_response(data=CaseMembershipSerializer(membership).data, message='成员关系已更新')


class CaseMemberLeaveView(APIView):
    """成员退出案件（管理员）：授权下一次请求立即停止。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _require_admin(request)
        if denied:
            return denied
        case = Case.objects.filter(pk=pk).first()
        if not case:
            return error_response(message='案件不存在', code=404)

        serializer = MembershipLeaveSerializer(data=request.data)
        if not serializer.is_valid():
            first = list(serializer.errors.values())[0]
            return error_response(message=str(first[0] if isinstance(first, list) else first))
        data = serializer.validated_data
        user = User.objects.get(pk=data['user_id'])
        try:
            terminal = case.remove_member(
                user, leave_at=data.get('leave_at'), actor=request.user,
                reason=data.get('reason', ''))
        except ValueError as exc:
            return error_response(message=str(exc))
        logger.info('User %s removed user %s from case %s',
                    request.user.username, user.username, case.case_no)
        return success_response(data=CaseMembershipSerializer(terminal).data, message='成员已退出案件')


class CaseMergeView(APIView):
    """案件合并（管理员）。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _require_admin(request)
        if denied:
            return denied
        source = Case.objects.filter(pk=pk).first()
        if not source:
            return error_response(message='源案件不存在', code=404)
        serializer = MergeSerializer(data=request.data)
        if not serializer.is_valid():
            first = list(serializer.errors.values())[0]
            return error_response(message=str(first[0] if isinstance(first, list) else first))
        target = Case.objects.get(pk=serializer.validated_data['target_case_id'])
        try:
            source, target, moved = services.merge_cases(
                source=source, target=target, actor=request.user,
                reason=serializer.validated_data.get('reason', ''))
        except ValueError as exc:
            return error_response(message=str(exc))
        return success_response(message=f'合并完成，迁移货物 {len(moved)} 件', data={
            'source': CaseSerializer(source).data,
            'target': CaseSerializer(target).data,
            'moved_goods_count': len(moved),
        })


class CaseSplitView(APIView):
    """案件拆分（管理员）。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _require_admin(request)
        if denied:
            return denied
        source = Case.objects.filter(pk=pk).first()
        if not source:
            return error_response(message='源案件不存在', code=404)
        serializer = SplitSerializer(data=request.data)
        if not serializer.is_valid():
            first = list(serializer.errors.values())[0]
            return error_response(message=str(first[0] if isinstance(first, list) else first))
        data = serializer.validated_data

        # 建案与资源迁移放在同一事务内，任何一步失败都不会留下空壳案件
        try:
            with transaction.atomic():
                new_case = services.create_case(
                    name=data['new_case_name'], case_no=data['new_case_no'],
                    actor=request.user, remark=f'由案件 {source.case_no} 拆分成立')
                new_case, moved = services.split_case(
                    source=source, new_case=new_case, goods_ids=data['goods_ids'],
                    actor=request.user, reason=data.get('reason', ''))
        except ValueError as exc:
            return error_response(message=str(exc))
        return success_response(message=f'拆分完成，迁出货物 {len(moved)} 件', data={
            'source_id': source.pk, 'new_case': CaseSerializer(new_case).data,
            'moved_goods_count': len(moved),
        })


class CaseEventListView(APIView):
    """案件生命周期事件（管理员或案件当前成员）。"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        case = Case.objects.filter(pk=pk).first()
        if not case:
            return error_response(message='案件不存在', code=404)
        if not request.user.is_admin and not (
                case.status == 'active' and case.membership_versions.filter(
                    user=request.user, is_current=True).exists()):
            return error_response(message='无权查看该案件事件', code=403)
        queryset = case.events.select_related('actor').order_by('-created_at')
        total, rows, page, page_size = _paginate(request, queryset)
        return success_response(data={
            'list': CaseEventSerializer(rows, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })


class AccessDecisionLogListView(APIView):
    """
    授权判定日志查询（仅管理员）。
    支持按 decision=deny、案件、用户、请求追踪ID 过滤，
    返回内容包含每次拒绝所依据的成员关系版本外键与版本快照。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_admin:
            return error_response(message='无权限访问，仅管理员可查看授权判定日志', code=403)

        queryset = AccessDecisionLog.objects.all().select_related(
            'user', 'case', 'membership_version')
        decision = request.query_params.get('decision')
        if decision in ('allow', 'deny'):
            queryset = queryset.filter(decision=decision)
        case_id = request.query_params.get('case')
        if case_id:
            queryset = queryset.filter(case_id=case_id)
        user_id = request.query_params.get('user')
        if user_id:
            queryset = queryset.filter(user_id=user_id)
        resource_type = request.query_params.get('resource_type')
        if resource_type:
            queryset = queryset.filter(resource_type=resource_type)
        request_id = request.query_params.get('request_id')
        if request_id:
            queryset = queryset.filter(request_id=request_id)

        total, rows, page, page_size = _paginate(request, queryset)
        return success_response(data={
            'list': AccessDecisionLogSerializer(rows, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })
