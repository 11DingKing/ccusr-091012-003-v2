"""
案件与版本化成员关系

授权模型要点：
- ``CaseMembership`` 仅追加（append-only）：加入、调整时间窗、退出都产生新行，
  旧行永久保留并通过 ``end_version`` 串成版本链，作为历史审计与拒绝判定的证据。
- 是否授权只看 ``is_current=True`` 且案件处于 active 的成员行，因此退出案件后
  下一次请求立即失效，旧版本行不授予任何访问权。
- 案件合并不自动合并成员关系：货物迁移到目标案件，源案件置 merged，
  是否能访问合并后的资源必须显式加入目标案件，杜绝沿合并链越权。
"""
from django.db import models, transaction
from django.utils import timezone

from apps.authentication.models import User


class Case(models.Model):
    """案件（保管资源的归属边界）"""

    STATUS_CHOICES = [
        ('active', '办理中'),
        ('merged', '已合并'),
        ('closed', '已结案'),
    ]

    name = models.CharField('案件名称', max_length=100)
    case_no = models.CharField('案件编号', max_length=50, unique=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='active')
    # 合并目标：仅合并使用。拆分时源案件保持 active，因此不产生链。
    merged_into = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='merged_from', verbose_name='合并至案件'
    )
    version = models.PositiveIntegerField('案件版本号', default=1)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_cases', verbose_name='创建人'
    )
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'cs_case'
        verbose_name = '案件'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.case_no} - {self.name}"

    @property
    def is_active(self):
        return self.status == 'active'

    def current_membership(self, user):
        """用户在本案件的当前成员关系版本；不存在返回 None。"""
        return self.membership_versions.filter(
            user=user, is_current=True
        ).select_related('case', 'user').first()

    def latest_membership(self, user):
        """用户在本案件的最新版本行（含已退出的终止行），用于拒绝时取证。"""
        return self.membership_versions.filter(user=user).order_by('-version').first()

    @transaction.atomic
    def add_member(self, user, *, valid_from=None, valid_to=None,
                   role='member', actor=None, reason=''):
        """加入案件。已退出后可重新加入（生成新版本链）。"""
        latest = self.latest_membership(user)
        if latest and latest.is_current:
            raise ValueError('该用户已在案件中，请使用调整成员关系接口')

        now = timezone.now()
        valid_from = valid_from or now
        version = (latest.version + 1) if latest else 1

        row = CaseMembership.objects.create(
            case=self, user=user, role=role,
            valid_from=valid_from, valid_to=valid_to,
            version=version, is_current=True,
            change_type='join', reason=reason, created_by=actor,
        )
        CaseEvent.objects.record(
            case=self, event_type='join', actor=actor, version=self.version,
            summary=f'{user.username} 加入案件',
            payload={'user_id': user.id, 'username': user.username,
                    'membership_version': version,
                    'valid_from': valid_from.isoformat(),
                    'valid_to': valid_to.isoformat() if valid_to else None,
                    'role': role, 'reason': reason},
        )
        return row

    @transaction.atomic
    def adjust_member(self, user, *, valid_from=None, valid_to=None,
                      role=None, actor=None, reason=''):
        """调整成员的时间窗/角色：关闭旧版本并追加新版本。"""
        current = self.current_membership(user)
        if not current:
            raise ValueError('该用户当前不是案件成员')

        now = timezone.now()
        new_version = current.version + 1
        current.is_current = False
        current.system_to = now
        current.end_version = new_version
        current.save(update_fields=['is_current', 'system_to', 'end_version'])

        row = CaseMembership.objects.create(
            case=self, user=user,
            role=role or current.role,
            valid_from=valid_from if valid_from is not None else current.valid_from,
            valid_to=valid_to if valid_to is not None else current.valid_to,
            version=new_version, is_current=True,
            change_type='update', reason=reason, created_by=actor,
        )
        CaseEvent.objects.record(
            case=self, event_type='update_window', actor=actor, version=self.version,
            summary=f'调整 {user.username} 的成员关系',
            payload={'user_id': user.id, 'username': user.username,
                    'membership_version': new_version,
                    'valid_from': row.valid_from.isoformat(),
                    'valid_to': row.valid_to.isoformat() if row.valid_to else None,
                    'role': row.role, 'reason': reason},
        )
        return row

    @transaction.atomic
    def remove_member(self, user, *, leave_at=None, actor=None, reason=''):
        """退出案件：关闭当前版本并追加终止版本（leave），授权立即停止。"""
        current = self.current_membership(user)
        if not current:
            raise ValueError('该用户当前不是案件成员')

        now = timezone.now()
        leave_at = leave_at or now
        new_version = current.version + 1
        current.is_current = False
        current.system_to = now
        current.end_version = new_version
        current.save(update_fields=['is_current', 'system_to', 'end_version'])

        # 终止版本：保留该成员最后的授权时间窗，is_current 立即为 False
        terminal = CaseMembership.objects.create(
            case=self, user=user, role=current.role,
            valid_from=current.valid_from, valid_to=leave_at,
            version=new_version, is_current=False,
            system_to=now, change_type='leave',
            reason=reason, created_by=actor,
        )
        CaseEvent.objects.record(
            case=self, event_type='leave', actor=actor, version=self.version,
            summary=f'{user.username} 退出案件',
            payload={'user_id': user.id, 'username': user.username,
                    'membership_version': new_version,
                    'leave_at': leave_at.isoformat(), 'reason': reason},
        )
        return terminal


class CaseMembership(models.Model):
    """
    案件成员关系版本行（append-only，双时间）。

    - 业务时间：``valid_from`` / ``valid_to`` 描述该版本在案件内的授权时段；
    - 系统时间：``system_from`` / ``system_to`` 描述版本行在库中的生效区间；
    - ``is_current=True`` 的行才可能授予访问权；终止版本（leave）自身即为非当前。
    """

    ROLE_CHOICES = [
        ('lead', '案件负责人'),
        ('member', '案件成员'),
    ]
    CHANGE_CHOICES = [
        ('join', '加入'),
        ('update', '调整时间窗/角色'),
        ('leave', '退出'),
    ]

    case = models.ForeignKey(
        Case, on_delete=models.PROTECT,
        related_name='membership_versions', verbose_name='案件'
    )
    user = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='case_memberships', verbose_name='用户'
    )
    role = models.CharField('案件角色', max_length=20, choices=ROLE_CHOICES, default='member')
    valid_from = models.DateTimeField('授权开始时间')
    valid_to = models.DateTimeField('授权结束时间', null=True, blank=True)
    system_from = models.DateTimeField('版本生效时间', auto_now_add=True)
    system_to = models.DateTimeField('版本失效时间', null=True, blank=True)
    version = models.PositiveIntegerField('版本号')
    is_current = models.BooleanField('是否当前版本', default=False, db_index=True)
    end_version = models.PositiveIntegerField('后继版本号', null=True, blank=True)
    change_type = models.CharField('变更类型', max_length=20, choices=CHANGE_CHOICES)
    reason = models.CharField('变更原因', max_length=200, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_memberships', verbose_name='操作人'
    )

    class Meta:
        db_table = 'cs_case_membership'
        verbose_name = '案件成员关系版本'
        verbose_name_plural = verbose_name
        ordering = ['case_id', 'user_id', '-version']
        constraints = [
            models.UniqueConstraint(
                fields=['case', 'user', 'version'],
                name='uniq_membership_case_user_version'
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'is_current'], name='idx_membership_user_cur'),
            models.Index(fields=['case', 'user', 'is_current'],
                         name='idx_membership_case_user_cur'),
        ]

    def __str__(self):
        return f"{self.user}@{self.case_id} v{self.version} {self.change_type}"

    def covers(self, at):
        """业务时间点是否落在授权时间窗内（左闭右开）。"""
        return self.valid_from <= at and (self.valid_to is None or at < self.valid_to)

    def snapshot(self):
        """供判定日志固化的证据快照。"""
        return {
            'membership_id': self.id,
            'case_id': self.case_id,
            'user_id': self.user_id,
            'version': self.version,
            'role': self.role,
            'change_type': self.change_type,
            'is_current': self.is_current,
            'valid_from': self.valid_from.isoformat() if self.valid_from else None,
            'valid_to': self.valid_to.isoformat() if self.valid_to else None,
            'system_from': self.system_from.isoformat() if self.system_from else None,
            'system_to': self.system_to.isoformat() if self.system_to else None,
            'end_version': self.end_version,
            'reason': self.reason,
        }


class CaseEventQuerySet(models.QuerySet):
    def record(self, *, case, event_type, actor=None, version=None,
               summary='', payload=None):
        return self.create(
            case=case, event_type=event_type, actor=actor,
            case_version=version if version is not None else case.version,
            summary=summary, payload=payload or {},
        )


class CaseEvent(models.Model):
    """案件生命周期事件（创建、成员变更、合并、拆分），保证版本可追溯。"""

    EVENT_CHOICES = [
        ('create', '创建案件'),
        ('join', '成员加入'),
        ('update_window', '调整成员关系'),
        ('leave', '成员退出'),
        ('merge', '案件合并'),
        ('split', '案件拆分'),
        ('assign', '资源归属调整'),
    ]

    case = models.ForeignKey(
        Case, on_delete=models.CASCADE,
        related_name='events', verbose_name='案件'
    )
    event_type = models.CharField('事件类型', max_length=20, choices=EVENT_CHOICES)
    case_version = models.PositiveIntegerField('事件后案件版本')
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='case_events', verbose_name='操作人'
    )
    summary = models.CharField('摘要', max_length=200, blank=True)
    payload = models.JSONField('事件详情', default=dict, blank=True)
    created_at = models.DateTimeField('发生时间', auto_now_add=True)

    objects = CaseEventQuerySet.as_manager()

    class Meta:
        db_table = 'cs_case_event'
        verbose_name = '案件事件'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.case_id} {self.event_type} v{self.case_version}"


class AccessDecisionLog(models.Model):
    """
    授权判定日志：列表/详情/导出/操作日志查询的每一次允许或拒绝都记录于此，
    拒绝时固化所依据的成员关系版本快照，供管理员审计。
    """

    DECISION_CHOICES = [('allow', '允许'), ('deny', '拒绝')]

    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='access_decisions', verbose_name='访问用户'
    )
    case = models.ForeignKey(
        Case, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='access_decisions', verbose_name='相关案件'
    )
    action = models.CharField('访问动作', max_length=30)
    resource_type = models.CharField('资源类型', max_length=30)
    resource_id = models.CharField('资源标识', max_length=64, blank=True, default='')
    decision = models.CharField('判定结果', max_length=10, choices=DECISION_CHOICES, db_index=True)
    reason = models.CharField('判定原因', max_length=50, blank=True, default='')
    membership_version = models.ForeignKey(
        CaseMembership, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='依据的成员关系版本'
    )
    membership_version_number = models.PositiveIntegerField('依据版本号', null=True, blank=True)
    membership_snapshot = models.JSONField('成员关系版本快照', null=True, blank=True)
    case_version = models.PositiveIntegerField('判定时案件版本', null=True, blank=True)
    request_id = models.CharField('请求追踪ID', max_length=64, blank=True, db_index=True)
    path = models.CharField('请求路径', max_length=255, blank=True)
    method = models.CharField('请求方法', max_length=10, blank=True)
    ip_address = models.GenericIPAddressField('IP地址', null=True, blank=True)
    detail = models.CharField('附加信息', max_length=500, blank=True, default='')
    created_at = models.DateTimeField('判定时间', auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'cs_access_decision_log'
        verbose_name = '授权判定日志'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['decision', 'created_at']),
            models.Index(fields=['user', 'decision', 'created_at']),
            models.Index(fields=['case', 'created_at']),
        ]

    def __str__(self):
        return f"{self.user_id} {self.decision} {self.resource_type}:{self.resource_id}"
