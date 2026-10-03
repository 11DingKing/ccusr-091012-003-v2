"""案件模块序列化器"""
from django.utils import timezone
from rest_framework import serializers

from apps.authentication.models import User
from .models import Case, CaseMembership, CaseLineage, AccessDecision


class CaseSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    merged_into_name = serializers.CharField(source='merged_into.case_no', read_only=True)

    class Meta:
        model = Case
        fields = [
            'id', 'name', 'case_no', 'status', 'status_display',
            'merged_into', 'merged_into_name',
            'created_by', 'created_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'status', 'merged_into', 'created_at', 'updated_at']


class CaseCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=True)
    case_no = serializers.CharField(max_length=50, required=True)

    def validate_case_no(self, value):
        if Case.objects.filter(case_no=value).exists():
            raise serializers.ValidationError('案件编号已存在')
        return value


class MembershipSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    user_real_name = serializers.CharField(source='user.real_name', read_only=True)
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = CaseMembership
        fields = [
            'id', 'case', 'user', 'username', 'user_real_name', 'version',
            'valid_from', 'valid_to', 'is_open',
            'granted_by', 'revoked_by', 'closed_at', 'created_at',
        ]
        read_only_fields = fields


class MembershipGrantSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(required=True)
    valid_from = serializers.DateTimeField(required=False)
    valid_to = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, data):
        if not User.objects.filter(pk=data['user_id'], is_active=True).exists():
            raise serializers.ValidationError('用户不存在或已停用')
        data.setdefault('valid_from', timezone.now())
        if data.get('valid_to') and data['valid_to'] <= data['valid_from']:
            raise serializers.ValidationError('授权结束时间必须晚于开始时间')
        return data


class MembershipRevokeSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(required=True)
    effective_at = serializers.DateTimeField(required=False, allow_null=True)


class CaseMergeSerializer(serializers.Serializer):
    source_case_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, required=True
    )
    target_case_id = serializers.IntegerField(required=True)
    remark = serializers.CharField(required=False, allow_blank=True, default='')


class SplitTargetSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=True)
    case_no = serializers.CharField(max_length=50, required=True)
    goods_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    stock_in_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    stock_out_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)

    def validate_case_no(self, value):
        if Case.objects.filter(case_no=value).exists():
            raise serializers.ValidationError(f'案件编号 {value} 已存在')
        return value


class CaseSplitSerializer(serializers.Serializer):
    source_case_id = serializers.IntegerField(required=True)
    targets = SplitTargetSerializer(many=True, allow_empty=False)
    archive_source = serializers.BooleanField(required=False, default=False)
    remark = serializers.CharField(required=False, allow_blank=True, default='')


class CaseLineageSerializer(serializers.ModelSerializer):
    relation_display = serializers.CharField(source='get_relation_display', read_only=True)
    source_case_no = serializers.CharField(source='source_case.case_no', read_only=True)
    target_case_no = serializers.CharField(source='target_case.case_no', read_only=True)

    class Meta:
        model = CaseLineage
        fields = [
            'id', 'relation', 'relation_display',
            'source_case', 'source_case_no', 'target_case', 'target_case_no',
            'goods_ids', 'stock_in_ids', 'stock_out_ids',
            'operated_by', 'created_at', 'remark',
        ]


class AccessDecisionSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True, default=None)
    case_no = serializers.CharField(source='case.case_no', read_only=True, default=None)

    class Meta:
        model = AccessDecision
        fields = [
            'id', 'user', 'username', 'decision', 'action',
            'resource_type', 'resource_id',
            'case', 'case_no', 'membership', 'membership_snapshot',
            'reason', 'evaluated_at', 'ip_address', 'created_at',
        ]
