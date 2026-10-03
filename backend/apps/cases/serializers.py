"""
案件模块序列化器
"""
from datetime import datetime

from rest_framework import serializers

from apps.authentication.models import User
from .models import AccessDecisionLog, Case, CaseEvent, CaseMembership


class CaseSerializer(serializers.ModelSerializer):
    """案件序列化器"""
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    merged_into_name = serializers.CharField(source='merged_into.case_no', read_only=True)

    class Meta:
        model = Case
        fields = [
            'id', 'name', 'case_no', 'status', 'status_display',
            'merged_into', 'merged_into_name', 'version',
            'created_by', 'created_by_name', 'remark',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'status', 'merged_into', 'version',
                            'created_at', 'updated_at']


class CaseCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=True)
    case_no = serializers.CharField(max_length=50, required=True)
    remark = serializers.CharField(max_length=500, required=False, allow_blank=True)
    member_user_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list)
    valid_from = serializers.DateTimeField(required=False, allow_null=True)
    valid_to = serializers.DateTimeField(required=False, allow_null=True)

    def validate_case_no(self, value):
        if Case.objects.filter(case_no=value).exists():
            raise serializers.ValidationError('案件编号已存在')
        return value

    def validate(self, data):
        if data.get('valid_to') and data.get('valid_from') and \
                data['valid_to'] <= data['valid_from']:
            raise serializers.ValidationError('授权结束时间必须晚于开始时间')
        return data


class CaseMembershipSerializer(serializers.ModelSerializer):
    """成员关系版本序列化器"""
    username = serializers.CharField(source='user.username', read_only=True)
    user_real_name = serializers.CharField(source='user.real_name', read_only=True)
    case_no = serializers.CharField(source='case.case_no', read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    change_type_display = serializers.CharField(source='get_change_type_display', read_only=True)

    class Meta:
        model = CaseMembership
        fields = [
            'id', 'case', 'case_no', 'user', 'username', 'user_real_name',
            'role', 'role_display', 'valid_from', 'valid_to',
            'system_from', 'system_to', 'version', 'is_current',
            'end_version', 'change_type', 'change_type_display',
            'reason', 'created_by',
        ]
        read_only_fields = fields


class MembershipWriteSerializer(serializers.Serializer):
    """加入/调整成员的入参"""
    user_id = serializers.IntegerField(required=True)
    role = serializers.ChoiceField(choices=['lead', 'member'], required=False, default='member')
    valid_from = serializers.DateTimeField(required=False, allow_null=True)
    valid_to = serializers.DateTimeField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_user_id(self, value):
        if not User.objects.filter(pk=value).exists():
            raise serializers.ValidationError('用户不存在')
        return value

    def validate(self, data):
        vf = data.get('valid_from')
        vt = data.get('valid_to')
        if vt:
            if not vf:
                vf = datetime.now()
            if vt <= vf:
                raise serializers.ValidationError('授权结束时间必须晚于开始时间')
        return data


class MembershipLeaveSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(required=True)
    leave_at = serializers.DateTimeField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)


class MergeSerializer(serializers.Serializer):
    target_case_id = serializers.IntegerField(required=True)
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_target_case_id(self, value):
        target = Case.objects.filter(pk=value).first()
        if not target:
            raise serializers.ValidationError('目标案件不存在')
        if target.status != 'active':
            raise serializers.ValidationError('目标案件不是办理中状态')
        return value


class SplitSerializer(serializers.Serializer):
    new_case_name = serializers.CharField(max_length=100, required=True)
    new_case_no = serializers.CharField(max_length=50, required=True)
    goods_ids = serializers.ListField(
        child=serializers.IntegerField(), required=True, allow_empty=False)
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_new_case_no(self, value):
        if Case.objects.filter(case_no=value).exists():
            raise serializers.ValidationError('新案件编号已存在')
        return value


class CaseEventSerializer(serializers.ModelSerializer):
    event_type_display = serializers.CharField(source='get_event_type_display', read_only=True)
    actor_name = serializers.CharField(source='actor.username', read_only=True)

    class Meta:
        model = CaseEvent
        fields = [
            'id', 'case', 'event_type', 'event_type_display',
            'case_version', 'actor', 'actor_name', 'summary',
            'payload', 'created_at',
        ]
        read_only_fields = fields


class AccessDecisionLogSerializer(serializers.ModelSerializer):
    """授权判定日志（管理员查看拒绝依据）"""
    username = serializers.CharField(source='user.username', read_only=True)
    case_no = serializers.CharField(source='case.case_no', read_only=True)
    decision_display = serializers.CharField(source='get_decision_display', read_only=True)

    class Meta:
        model = AccessDecisionLog
        fields = [
            'id', 'user', 'username', 'case', 'case_no',
            'action', 'resource_type', 'resource_id',
            'decision', 'decision_display', 'reason',
            'membership_version', 'membership_version_number',
            'membership_snapshot', 'case_version',
            'request_id', 'path', 'method', 'ip_address',
            'detail', 'created_at',
        ]
        read_only_fields = fields
