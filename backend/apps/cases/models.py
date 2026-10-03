"""
案件与成员关系模型。

授权模型要点：
- CaseMembership 采用 SCD2（缓慢变化维）版本化：每个成员在案件中可能存在
  多行记录，每行有 [valid_from, valid_to) 半开时间窗与递增 version。
  人员退出、时间范围调整都会关闭当前版本并新开一行，历史版本永不删除，
  因此“某次访问被拒绝时依据的是哪个成员关系版本”可以被精确还原。
- CaseLineage 记录案件合并、拆分：父案件 -> 子案件，资源在事务中迁移，
  成员关系不随谱系自动继承，必须显式重新授权。
- AccessDecision 记录每一次资源访问的授权判定（允许/拒绝），拒绝时
  保存评估到的成员关系版本快照，供管理员审计“为什么被拒”。
"""
from django.db import models, transaction
from django.utils import timezone

from apps.authentication.models import User


class Case(models.Model):
    """案件（联合调查的基本资源隔离单元）"""

    STATUS_ACTIVE = 'active'
    STATUS_MERGED = 'merged'
    STATUS_ARCHIVED = 'archived'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, '办理中'),
        (STATUS_MERGED, '已合并'),
        (STATUS_ARCHIVED, '已归档'),
    ]

    name = models.CharField('案件名称', max_length=100)
    case_no = models.CharField('案件编号', max_length=50, unique=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='active')
    # 合并后指向承接案件；拆分后父案件视情况归档
    merged_into = models.ForeignKey(
        'self', on_delete=models.PROTECT, null=True, blank=True,
        related_name='merged_sources', verbose_name='合并至'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_cases', verbose_name='创建人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'cs_case'
        verbose_name = '案件'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.case_no} {self.name}'

    @property
    def is_open(self):
        """案件是否仍可接受新访问（合并/归档后立即停止授权）"""
        return self.status == 'active'

    # ---------------- 成员关系管理 ----------------

    def grant_membership(self, user, valid_from, valid_to=None, granted_by=None):
        """
        授予/调整成员关系。

        若用户当前存在开放版本（valid_to 为空），先将其在 valid_from 处关闭，
        再开出新版本；历史版本保留。同一用户可同时属于多个案件，也可在
        同一案件内拥有互不重叠或相接的多个时间窗版本。
        """
        with transaction.atomic():
            open_versions = list(
                self.memberships.filter(user=user, valid_to__isnull=True).select_for_update()
            )
            now = timezone.now()
            for version in open_versions:
                version.valid_to = valid_from
                version.closed_at = now
                version.save(update_fields=['valid_to', 'closed_at', 'updated_at'])

            latest = self.memberships.filter(user=user).order_by('-version').first()
            return CaseMembership.objects.create(
                case=self,
                user=user,
                version=(latest.version + 1) if latest else 1,
                valid_from=valid_from,
                valid_to=valid_to,
                granted_by=granted_by,
            )

    def revoke_membership(self, user, effective_at=None, revoked_by=None):
        """
        退出案件：在 effective_at（默认当前时间）关闭该用户所有开放版本。

        关闭后该用户的新访问立即被拒绝（评估点 >= valid_to），
        但关闭前产生的历史操作日志仍以当时的版本为依据，可被审计。
        """
        effective_at = effective_at or timezone.now()
        with transaction.atomic():
            open_versions = list(
                self.memberships.filter(user=user, valid_to__isnull=True).select_for_update()
            )
            for version in open_versions:
                close_at = max(effective_at, version.valid_from)
                version.valid_to = close_at
                version.closed_at = timezone.now()
                version.revoked_by = revoked_by
                version.save(update_fields=['valid_to', 'closed_at', 'revoked_by', 'updated_at'])
            return len(open_versions)

    def membership_at(self, user, at):
        """
        返回用户在指定时刻对本案件“生效中”的成员关系版本；不存在返回 None。

        判定：valid_from <= at 且 (valid_to 为空 或 at < valid_to)。
        """
        return self.memberships.filter(
            user=user,
            valid_from__lte=at,
        ).filter(
            models.Q(valid_to__isnull=True) | models.Q(valid_to__gt=at)
        ).order_by('-version').first()

    def effective_membership_ids(self, user, at=None):
        """用户在 at 时刻对本案件生效的全部成员关系版本 ID（一般为 0 或 1）。"""
        at = at or timezone.now()
        return list(
            self.memberships.filter(
                user=user,
                valid_from__lte=at,
            ).filter(
                models.Q(valid_to__isnull=True) | models.Q(valid_to__gt=at)
            ).values_list('id', flat=True)
        )


class CaseMembership(models.Model):
    """
    案件成员关系（版本化）。

    半开区间 [valid_from, valid_to)：valid_to 为空表示至今有效。
    每次授权变更新增一行并使旧版本失效，version 在同一 (case, user) 内递增。
    """

    case = models.ForeignKey(
        Case, on_delete=models.PROTECT,
        related_name='memberships', verbose_name='案件'
    )
    user = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='case_memberships', verbose_name='成员'
    )
    version = models.PositiveIntegerField('版本号', default=1)
    valid_from = models.DateTimeField('生效开始时间')
    valid_to = models.DateTimeField('生效结束时间', null=True, blank=True)
    granted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='granted_memberships', verbose_name='授权人'
    )
    revoked_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='revoked_memberships', verbose_name='撤销人'
    )
    closed_at = models.DateTimeField('版本关闭时间', null=True, blank=True)
    created_at = models.DateTimeField('版本创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'cs_case_membership'
        verbose_name = '案件成员关系'
        verbose_name_plural = verbose_name
        ordering = ['case_id', 'user_id', '-version']
        indexes = [
            models.Index(fields=['user', 'valid_from', 'valid_to']),
            models.Index(fields=['case', 'user', '-version']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['case', 'user', 'version'],
                name='uniq_case_user_membership_version',
            ),
        ]

    def __str__(self):
        return f'{self.user_id}@{self.case_id} v{self.version}'

    @property
    def is_open(self):
        """该版本当前是否开放（未设结束时间）"""
        return self.valid_to is None

    def is_effective_at(self, at):
        """在给定时刻该版本是否生效：[valid_from, valid_to)"""
        if at < self.valid_from:
            return False
        return self.valid_to is None or at < self.valid_to


class CaseLineage(models.Model):
    """
    案件谱系：记录合并与拆分的资源流向。

    - 合并：多个 source_case 指向同一个 target_case
    - 拆分：一个 source_case 指向多个 target_case
    仅记录谱系事实；成员授权不会沿谱系自动传递。
    """

    RELATION_CHOICES = [
        ('merge', '合并'),
        ('split', '拆分'),
    ]

    relation = models.CharField('关系类型', max_length=10, choices=RELATION_CHOICES)
    source_case = models.ForeignKey(
        Case, on_delete=models.PROTECT,
        related_name='lineage_children', verbose_name='来源案件'
    )
    target_case = models.ForeignKey(
        Case, on_delete=models.PROTECT,
        related_name='lineage_parents', verbose_name='目标案件'
    )
    # 迁移的保管资源 ID 清单（按资源类型分组）
    goods_ids = models.JSONField('迁移货物ID', default=list, blank=True)
    stock_in_ids = models.JSONField('迁移入库记录ID', default=list, blank=True)
    stock_out_ids = models.JSONField('迁移出库记录ID', default=list, blank=True)
    operated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='case_lineage_ops', verbose_name='操作人'
    )
    created_at = models.DateTimeField('操作时间', auto_now_add=True)
    remark = models.TextField('备注', blank=True)

    class Meta:
        db_table = 'cs_case_lineage'
        verbose_name = '案件谱系'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_relation_display()}: {self.source_case_id} -> {self.target_case_id}'


class AccessDecision(models.Model):
    """
    访问授权判定记录。

    列表、详情、导出、操作日志查询的每一次资源范围判定都会落一条记录。
    拒绝时携带：评估时刻、匹配到的案件、成员关系版本（若有）及快照，
    管理员据此回答“这次拒绝依据的是哪一版成员关系”。
    """

    DECISION_CHOICES = [
        ('allow', '允许'),
        ('deny', '拒绝'),
    ]

    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='access_decisions', verbose_name='访问人'
    )
    decision = models.CharField('判定结果', max_length=10, choices=DECISION_CHOICES)
    action = models.CharField('访问动作', max_length=30)
    resource_type = models.CharField('资源类型', max_length=30)
    resource_id = models.CharField('资源标识', max_length=64, blank=True)
    case = models.ForeignKey(
        Case, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='access_decisions', verbose_name='相关案件'
    )
    # 评估时命中的成员关系版本（拒绝也可能命中：例如时间窗外）
    membership = models.ForeignKey(
        CaseMembership, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='access_decisions', verbose_name='依据成员关系版本'
    )
    # 成员关系版本快照：即使版本日后被关闭/清理，拒绝理由仍可还原
    membership_snapshot = models.JSONField('成员关系版本快照', null=True, blank=True)
    reason = models.CharField('判定理由', max_length=200, blank=True)
    evaluated_at = models.DateTimeField('评估时刻', default=timezone.now)
    ip_address = models.GenericIPAddressField('IP地址', null=True, blank=True)
    created_at = models.DateTimeField('记录时间', auto_now_add=True)

    class Meta:
        db_table = 'cs_access_decision'
        verbose_name = '访问判定记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['decision', '-created_at']),
        ]

    def __str__(self):
        return f'{self.user_id} {self.decision} {self.resource_type}:{self.resource_id}'
