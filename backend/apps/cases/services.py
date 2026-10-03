"""
案件合并 / 拆分服务。

资源（货物、出入库记录）在事务中整体迁移并逐案记录 CaseLineage；
成员关系绝不沿谱系自动继承——目标案件必须重新授权，从机制上杜绝
“合并/拆分后看到其他案件记录”。
"""
from django.db import transaction

from apps.core.exceptions import BusinessException
from apps.warehouse.models import Goods, StockIn, StockOut
from .models import Case, CaseLineage


def _move(qs, target_case):
    ids = list(qs.values_list('id', flat=True))
    if ids:
        qs.update(case=target_case)
    return ids


@transaction.atomic
def merge_cases(source_case_ids, target_case_id, operated_by, remark=''):
    """
    将多个来源案件的全部保管资源并入目标案件。

    - 目标案件必须处于办理中；
    - 来源案件标记为 merged 并指向目标案件；
    - 不迁移任何成员关系；来源案件的成员访问随案件关闭立即停止，
      需访问目标案件的，必须在目标案件中重新授权。
    """
    if not source_case_ids:
        raise BusinessException('请选择需要合并的案件')
    if int(target_case_id) in [int(i) for i in source_case_ids]:
        raise BusinessException('目标案件不能同时是来源案件')

    target = Case.objects.select_for_update().filter(pk=target_case_id).first()
    if target is None:
        raise BusinessException('目标案件不存在', code=404)
    if not target.is_open:
        raise BusinessException('目标案件已合并或归档，无法承接资源')

    sources = list(
        Case.objects.select_for_update().filter(pk__in=source_case_ids).order_by('id')
    )
    if len(sources) != len(set(source_case_ids)):
        raise BusinessException('部分来源案件不存在')
    for source in sources:
        if not source.is_open:
            raise BusinessException(f'案件 {source.case_no} 已关闭，不能参与合并')

    lineages = []
    for source in sources:
        goods_ids = _move(Goods.objects.filter(case=source), target)
        stock_in_ids = _move(StockIn.objects.filter(case=source), target)
        stock_out_ids = _move(StockOut.objects.filter(case=source), target)

        lineages.append(CaseLineage.objects.create(
            relation='merge',
            source_case=source,
            target_case=target,
            goods_ids=goods_ids,
            stock_in_ids=stock_in_ids,
            stock_out_ids=stock_out_ids,
            operated_by=operated_by,
            remark=remark,
        ))

        source.status = Case.STATUS_MERGED
        source.merged_into = target
        source.save(update_fields=['status', 'merged_into', 'updated_at'])

    return target, lineages


@transaction.atomic
def split_case(source_case_id, targets, operated_by, archive_source=False, remark=''):
    """
    将来源案件中的指定资源拆分到若干新建案件。

    targets: [{'name', 'case_no', 'goods_ids', 'stock_in_ids', 'stock_out_ids'}, ...]
    - 每条资源 ID 必须确实属于来源案件，否则整体回滚；
    - 同一资源不能被重复分配到多个目标；
    - 成员关系不跟随资源迁移；
    - archive_source=True 且资源全部迁空后，来源案件归档。
    """
    if not targets:
        raise BusinessException('请至少指定一个拆分目标案件')

    source = Case.objects.select_for_update().filter(pk=source_case_id).first()
    if source is None:
        raise BusinessException('来源案件不存在', code=404)
    if not source.is_open:
        raise BusinessException('来源案件已合并或归档，无法拆分')

    all_goods, all_in, all_out = [], [], []
    for target_data in targets:
        for key, bucket in (('goods_ids', all_goods), ('stock_in_ids', all_in),
                            ('stock_out_ids', all_out)):
            ids = target_data.get(key, []) or []
            if len(ids) != len(set(ids)):
                raise BusinessException(f'目标案件 {target_data.get("case_no")} 内存在重复资源ID')
            bucket.extend(ids)
    for bucket, label in ((all_goods, '货物'), (all_in, '入库记录'), (all_out, '出库记录')):
        if len(bucket) != len(set(bucket)):
            raise BusinessException(f'同一{label}不能拆分到多个目标案件')

    owned_goods = set(Goods.objects.filter(case=source, pk__in=all_goods).values_list('id', flat=True))
    owned_in = set(StockIn.objects.filter(case=source, pk__in=all_in).values_list('id', flat=True))
    owned_out = set(StockOut.objects.filter(case=source, pk__in=all_out).values_list('id', flat=True))
    missing = [
        f'货物 {i}' for i in all_goods if i not in owned_goods
    ] + [
        f'入库记录 {i}' for i in all_in if i not in owned_in
    ] + [
        f'出库记录 {i}' for i in all_out if i not in owned_out
    ]
    if missing:
        raise BusinessException('以下资源不属于来源案件，无法拆分：' + '、'.join(missing[:10]))

    lineages = []
    created_cases = []
    for target_data in targets:
        target = Case.objects.create(
            name=target_data['name'],
            case_no=target_data['case_no'],
            created_by=operated_by,
        )
        goods_ids = _move(Goods.objects.filter(case=source, pk__in=target_data.get('goods_ids', [])), target)
        stock_in_ids = _move(StockIn.objects.filter(case=source, pk__in=target_data.get('stock_in_ids', [])), target)
        stock_out_ids = _move(StockOut.objects.filter(case=source, pk__in=target_data.get('stock_out_ids', [])), target)

        lineages.append(CaseLineage.objects.create(
            relation='split',
            source_case=source,
            target_case=target,
            goods_ids=goods_ids,
            stock_in_ids=stock_in_ids,
            stock_out_ids=stock_out_ids,
            operated_by=operated_by,
            remark=remark,
        ))
        created_cases.append(target)

    if archive_source:
        source.status = Case.STATUS_ARCHIVED
        source.save(update_fields=['status', 'updated_at'])

    return source, created_cases, lineages
