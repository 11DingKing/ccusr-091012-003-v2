"""
案件合并与拆分

关键安全约束：
- 合并只迁移 *资源归属*，不迁移成员关系。源案件成员若未加入目标案件，
  对迁移后资源的访问在同一事务提交后立即停止；
- 拆分只把被选中的货物迁到新案件；未被显式加入新案件的人员看不到拆出的资源；
- 两个操作都在单事务内完成案件版本递增、货物迁移与事件落库，
  不存在“货物已迁走、授权仍照旧”的中间泄露窗口。
"""
from django.db import transaction
from django.utils import timezone

from apps.warehouse.models import Goods
from .models import Case, CaseEvent


@transaction.atomic
def merge_cases(*, source, target, actor=None, reason=''):
    """把 source 案件整体并入 target 案件。"""
    if source.pk == target.pk:
        raise ValueError('源案件与目标案件不能相同')
    if source.status != 'active' or target.status != 'active':
        raise ValueError('仅办理中的案件可以合并')

    # 行锁锁定两个案件，防止并发期间成员/资源变动
    locked = list(
        Case.objects.select_for_update().filter(pk__in=[source.pk, target.pk]).order_by('pk')
    )
    source = next(c for c in locked if c.pk == source.pk)
    target = next(c for c in locked if c.pk == target.pk)

    moved_goods_ids = list(
        Goods.objects.filter(case=source).values_list('id', flat=True)
    )
    Goods.objects.filter(case=source).update(case=target)

    source.status = 'merged'
    source.merged_into = target
    source.version += 1
    source.save(update_fields=['status', 'merged_into', 'version', 'updated_at'])
    target.version += 1
    target.save(update_fields=['version', 'updated_at'])

    payload = {
        'source_case_id': source.pk, 'source_case_no': source.case_no,
        'target_case_id': target.pk, 'target_case_no': target.case_no,
        'moved_goods_ids': moved_goods_ids,
        'moved_goods_count': len(moved_goods_ids),
        'reason': reason,
        'note': '成员关系不随合并迁移，需显式加入目标案件',
    }
    CaseEvent.objects.record(
        case=target, event_type='merge', actor=actor, version=target.version,
        summary=f'案件 {source.case_no} 合并入 {target.case_no}', payload=payload)
    # 源案件也留一条指向同一事件的记录，便于从两侧审计
    CaseEvent.objects.record(
        case=source, event_type='merge', actor=actor, version=source.version,
        summary=f'案件合并至 {target.case_no}', payload=payload)
    return source, target, moved_goods_ids


@transaction.atomic
def split_case(*, source, new_case, goods_ids, actor=None, reason=''):
    """把 source 案件中的指定货物拆分到新案件 new_case。"""
    if source.status != 'active':
        raise ValueError('仅办理中的案件可以拆分')

    source = Case.objects.select_for_update().get(pk=source.pk)
    goods_ids = list(Goods.objects.filter(case=source, pk__in=goods_ids)
                     .values_list('id', flat=True))
    if not goods_ids:
        raise ValueError('请选择需要拆分的在案货物')

    Goods.objects.filter(case=source, pk__in=goods_ids).update(case=new_case)

    source.version += 1
    source.save(update_fields=['version', 'updated_at'])

    payload = {
        'source_case_id': source.pk, 'source_case_no': source.case_no,
        'new_case_id': new_case.pk, 'new_case_no': new_case.case_no,
        'moved_goods_ids': goods_ids,
        'moved_goods_count': len(goods_ids),
        'reason': reason,
        'note': '拆出资源仅新案件成员可见，成员需显式加入',
    }
    CaseEvent.objects.record(
        case=new_case, event_type='split', actor=actor, version=new_case.version,
        summary=f'从 {source.case_no} 拆分成立', payload=payload)
    CaseEvent.objects.record(
        case=source, event_type='split', actor=actor, version=source.version,
        summary=f'拆分货物至新案件 {new_case.case_no}', payload=payload)
    return new_case, goods_ids


@transaction.atomic
def create_case(*, name, case_no, actor=None, remark='', member_specs=None):
    """创建案件并可同时录入初始成员（每个成员一个 join 版本）。"""
    now = timezone.now()
    case = Case.objects.create(
        name=name, case_no=case_no, created_by=actor, remark=remark)
    CaseEvent.objects.record(
        case=case, event_type='create', actor=actor, version=case.version,
        summary=f'创建案件 {case.case_no}',
        payload={'name': name, 'case_no': case_no, 'remark': remark})

    for spec in member_specs or []:
        case.add_member(
            spec['user'],
            valid_from=spec.get('valid_from') or now,
            valid_to=spec.get('valid_to'),
            role=spec.get('role', 'member'),
            actor=actor,
            reason=spec.get('reason', '案件初始化成员'),
        )
    return case
