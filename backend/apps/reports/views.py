"""
报表视图

导出、仪表盘、日报的数据范围与列表/详情/操作日志查询使用同一个
ScopeAuthorizer 授权判断，普通成员只能看到自己当前获授权案件、
且业务时间落在授权时间窗内的汇总数据；管理员见全局。
"""
import logging
import os
import shutil
from datetime import datetime, timedelta
from django.db.models import Sum, Count, Q, F
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from apps.core.response import success_response, error_response
from apps.cases.authorization import ScopeAuthorizer
from apps.warehouse.models import Goods, StockIn, StockOut, Warning
from .models import DailyReport

logger = logging.getLogger('apps')


class DashboardView(APIView):
    """仪表盘数据（按案件授权范围聚合）"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = datetime.now().date()
        authorizer = ScopeAuthorizer(request.user)

        goods_qs = authorizer.filter_queryset(Goods.objects.all(), 'goods')
        in_qs = authorizer.filter_queryset(StockIn.objects.all(), 'stock_in')
        out_qs = authorizer.filter_queryset(StockOut.objects.all(), 'stock_out')
        warning_qs = authorizer.filter_queryset(Warning.objects.all(), 'warning')

        # 货物统计
        goods_count = goods_qs.filter(is_active=True).count()
        warning_goods_count = goods_qs.filter(
            is_active=True,
            quantity__lte=F('warning_threshold')
        ).count()

        # 今日入库统计
        today_in = in_qs.filter(
            stock_in_time__date=today
        ).aggregate(
            count=Count('id'),
            total=Sum('quantity')
        )

        # 今日出库统计
        today_out = out_qs.filter(
            stock_out_time__date=today,
            status='completed'
        ).aggregate(
            count=Count('id'),
            total=Sum('quantity')
        )

        # 待审批数量
        pending_approval_count = out_qs.filter(status='pending').count()

        # 未读预警数量
        unread_warning_count = warning_qs.filter(is_read=False).count()

        # 最近7天入库趋势
        in_trend = []
        out_trend = []
        for i in range(6, -1, -1):
            date = today - timedelta(days=i)
            in_data = in_qs.filter(
                stock_in_time__date=date
            ).aggregate(total=Sum('quantity'))
            out_data = out_qs.filter(
                stock_out_time__date=date,
                status='completed'
            ).aggregate(total=Sum('quantity'))

            in_trend.append({
                'date': date.strftime('%m-%d'),
                'value': float(in_data['total'] or 0)
            })
            out_trend.append({
                'date': date.strftime('%m-%d'),
                'value': float(out_data['total'] or 0)
            })

        # 库存预警货物列表
        warning_goods = goods_qs.filter(
            is_active=True,
            quantity__lte=F('warning_threshold')
        ).values('id', 'name', 'code', 'quantity', 'warning_threshold')[:5]

        return success_response(data={
            'goods_count': goods_count,
            'warning_goods_count': warning_goods_count,
            'today_in_count': today_in['count'] or 0,
            'today_in_total': float(today_in['total'] or 0),
            'today_out_count': today_out['count'] or 0,
            'today_out_total': float(today_out['total'] or 0),
            'pending_approval_count': pending_approval_count,
            'unread_warning_count': unread_warning_count,
            'in_trend': in_trend,
            'out_trend': out_trend,
            'warning_goods': list(warning_goods)
        })


class DailyReportView(APIView):
    """每日报表（按案件授权范围聚合）"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')

        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).date()
        else:
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()

        if not end_date:
            end_date = datetime.now().date()
        else:
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()

        authorizer = ScopeAuthorizer(request.user)
        in_qs = authorizer.filter_queryset(StockIn.objects.all(), 'stock_in')
        out_qs = authorizer.filter_queryset(StockOut.objects.all(), 'stock_out')
        warning_qs = authorizer.filter_queryset(Warning.objects.all(), 'warning')

        reports = []
        current_date = start_date

        while current_date <= end_date:
            # 入库统计
            in_data = in_qs.filter(
                stock_in_time__date=current_date
            ).aggregate(
                count=Count('id'),
                total=Sum('quantity')
            )

            # 出库统计
            out_data = out_qs.filter(
                stock_out_time__date=current_date,
                status='completed'
            ).aggregate(
                count=Count('id'),
                total=Sum('quantity')
            )

            # 预警统计
            warning_count = warning_qs.filter(
                created_at__date=current_date
            ).count()

            reports.append({
                'date': current_date.strftime('%Y-%m-%d'),
                'in_count': in_data['count'] or 0,
                'in_total': float(in_data['total'] or 0),
                'out_count': out_data['count'] or 0,
                'out_total': float(out_data['total'] or 0),
                'warning_count': warning_count
            })

            current_date += timedelta(days=1)

        return success_response(data=reports)


class ExportView(APIView):
    """数据导出：与列表同一授权判断，仅导出授权范围内的记录"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        export_type = request.query_params.get('type', 'goods')
        authorizer = ScopeAuthorizer(request.user)

        wb = Workbook()
        ws = wb.active

        # 样式定义
        header_font = Font(bold=True, color='FFFFFF')
        header_fill = PatternFill(start_color='0066FF', end_color='0066FF', fill_type='solid')
        header_alignment = Alignment(horizontal='center', vertical='center')
        thin_border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )

        if export_type == 'goods':
            ws.title = '货物清单'
            headers = ['编码', '名称', '所属案件', '品类', '品种', '规格', '库存', '单位', '存放位置', '预警阈值']
            ws.append(headers)

            for col in range(1, len(headers) + 1):
                cell = ws.cell(row=1, column=col)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
                cell.border = thin_border

            # 数据：统一范围过滤
            goods_list = authorizer.filter_queryset(
                Goods.objects.select_related(
                    'variety', 'variety__category', 'variety__category__unit', 'case'
                ),
                'goods',
            ).filter(is_active=True)
            exported = 0
            for goods in goods_list:
                ws.append([
                    goods.code,
                    goods.name,
                    goods.case.case_no if goods.case else '',
                    goods.variety.category.name if goods.variety else '',
                    goods.variety.name if goods.variety else '',
                    goods.specification,
                    float(goods.quantity),
                    goods.variety.category.unit.name if goods.variety and goods.variety.category.unit else '',
                    goods.location,
                    float(goods.warning_threshold)
                ])
                exported += 1
            authorizer.record_scope_decision('goods', 'export', request)

            filename = f'货物清单_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx'

        elif export_type == 'stock_in':
            ws.title = '入库记录'
            headers = ['货物编码', '货物名称', '所属案件', '入库数量', '批次号', '供应商', '操作人', '入库时间']
            ws.append(headers)

            for col in range(1, len(headers) + 1):
                cell = ws.cell(row=1, column=col)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
                cell.border = thin_border

            stock_ins = authorizer.filter_queryset(
                StockIn.objects.select_related('goods', 'operator', 'case'),
                'stock_in',
            )
            exported = 0
            for record in stock_ins:
                ws.append([
                    record.goods.code,
                    record.goods.name,
                    record.case.case_no if record.case else '',
                    float(record.quantity),
                    record.batch_no,
                    record.supplier,
                    record.operator.username if record.operator else '',
                    record.stock_in_time.strftime('%Y-%m-%d %H:%M:%S')
                ])
                exported += 1
            authorizer.record_scope_decision('stock_in', 'export', request)

            filename = f'入库记录_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx'

        elif export_type == 'stock_out':
            ws.title = '出库记录'
            headers = ['货物编码', '货物名称', '所属案件', '出库数量', '领用人', '领用部门', '状态', '操作人', '出库时间']
            ws.append(headers)

            for col in range(1, len(headers) + 1):
                cell = ws.cell(row=1, column=col)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
                cell.border = thin_border

            stock_outs = authorizer.filter_queryset(
                StockOut.objects.select_related('goods', 'operator', 'case'),
                'stock_out',
            )
            exported = 0
            for record in stock_outs:
                ws.append([
                    record.goods.code,
                    record.goods.name,
                    record.case.case_no if record.case else '',
                    float(record.quantity),
                    record.receiver,
                    record.receiver_dept,
                    record.get_status_display(),
                    record.operator.username if record.operator else '',
                    record.stock_out_time.strftime('%Y-%m-%d %H:%M:%S') if record.stock_out_time else ''
                ])
                exported += 1
            authorizer.record_scope_decision('stock_out', 'export', request)

            filename = f'出库记录_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx'
        else:
            return error_response(message='无效的导出类型')

        # 调整列宽
        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except Exception:
                    pass
            adjusted_width = (max_length + 2) * 1.2
            ws.column_dimensions[column_letter].width = adjusted_width

        # 返回文件
        response = HttpResponse(
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        wb.save(response)

        logger.info("Data exported: %s (exported=%s, user=%s)",
                    export_type, exported, getattr(request.user, 'id', None))
        return response


class SystemMonitorView(APIView):
    """返回容器内可直接读取的轻量运行状态。"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cpu_count = os.cpu_count() or 1
        load_1m = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
        disk_total, disk_used, _ = shutil.disk_usage('/')
        disk_percent = round(disk_used * 100 / disk_total, 2) if disk_total else 0
        process_count = len([name for name in os.listdir('/proc') if name.isdigit()]) if os.path.isdir('/proc') else 1

        return success_response(data={
            'cpu': {
                'percent': round(load_1m * 100 / cpu_count, 2),
                'count': cpu_count
            },
            'memory': {
                'total': None,
                'used': None,
                'percent': None
            },
            'disk': {
                'total': round(disk_total / (1024 ** 3), 2),
                'used': round(disk_used / (1024 ** 3), 2),
                'percent': disk_percent
            },
            'network': {
                'bytes_sent': None,
                'bytes_recv': None
            },
            'system': {
                'boot_time': None,
                'uptime': None,
                'process_count': process_count
            }
        })
