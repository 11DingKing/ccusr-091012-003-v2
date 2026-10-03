"""
认证与操作日志中间件
"""
import logging
import re
from .backends import decode_token
from .models import User, OperationLog

logger = logging.getLogger('apps')


class JWTAuthenticationMiddleware:
    """JWT认证中间件"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 从请求头获取Token
        auth_header = request.headers.get('Authorization')

        if auth_header:
            if auth_header.startswith('Bearer '):
                token = auth_header.split(' ')[1]
            else:
                token = auth_header

            payload = decode_token(token)
            if payload:
                user_id = payload.get('user_id')
                try:
                    user = User.objects.get(id=user_id, is_active=True)
                    request.jwt_user = user
                except User.DoesNotExist:
                    request.jwt_user = None
            else:
                request.jwt_user = None
        else:
            request.jwt_user = None

        response = self.get_response(request)
        return response


# 路径 -> (模型查找方式) 用于把操作日志落到对应案件，支持按案件范围审计
_CASE_ID_PATTERNS = [
    (re.compile(r'^/api/cases/(\d+)'), 'case'),
    (re.compile(r'^/api/goods/(\d+)'), 'goods'),
    (re.compile(r'^/api/stock-in/(\d+)'), 'stock_in'),
    (re.compile(r'^/api/stock-out/(\d+)'), 'stock_out'),
    (re.compile(r'^/api/warnings/(\d+)'), 'warning'),
    (re.compile(r'^/api/approvals/(\d+)'), 'approval'),
]


def _resolve_case_id(path):
    """根据写操作路径反查资源所属案件；无法解析时返回 None。"""
    for pattern, kind in _CASE_ID_PATTERNS:
        match = pattern.match(path)
        if not match:
            continue
        pk = match.group(1)
        try:
            if kind == 'case':
                from apps.cases.models import Case
                return Case.objects.filter(pk=pk).values_list('id', flat=True).first()
            if kind == 'goods':
                from apps.warehouse.models import Goods
                return Goods.objects.filter(pk=pk).values_list('case_id', flat=True).first()
            if kind == 'stock_in':
                from apps.warehouse.models import StockIn
                return StockIn.objects.filter(pk=pk).values_list(
                    'goods__case_id', flat=True).first()
            if kind == 'stock_out':
                from apps.warehouse.models import StockOut
                return StockOut.objects.filter(pk=pk).values_list(
                    'goods__case_id', flat=True).first()
            if kind == 'warning':
                from apps.warehouse.models import Warning
                return Warning.objects.filter(pk=pk).values_list(
                    'goods__case_id', flat=True).first()
            if kind == 'approval':
                from apps.warehouse.models import Approval
                return Approval.objects.filter(pk=pk).values_list(
                    'stock_out__goods__case_id', flat=True).first()
        except Exception:
            logger.error('解析操作案件归属失败 path=%s', path, exc_info=True)
        return None
    return None


class OperationLogMiddleware:
    """操作日志中间件"""

    # 需要记录日志的路径和方法
    LOG_PATHS = {
        '/api/auth/login/': 'login',
        '/api/auth/logout/': 'logout',
    }

    # 需要记录的HTTP方法
    LOG_METHODS = ['POST', 'PUT', 'PATCH', 'DELETE']

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # 记录操作日志
        self._log_operation(request, response)

        return response

    def _log_operation(self, request, response):
        """记录操作日志"""
        path = request.path
        method = request.method

        # 检查是否需要记录
        if path in self.LOG_PATHS:
            action = self.LOG_PATHS[path]
        elif method in self.LOG_METHODS and path.startswith('/api/'):
            if method == 'POST':
                action = 'create'
            elif method in ['PUT', 'PATCH']:
                action = 'update'
            elif method == 'DELETE':
                action = 'delete'
            else:
                return
        else:
            return

        # 只记录成功的操作
        if response.status_code >= 400:
            return

        # 获取用户
        user = getattr(request, 'jwt_user', None) or getattr(request, 'user', None)
        if user and not user.is_authenticated:
            user = None

        # 获取模块名
        module = self._get_module_name(path)

        # 获取IP地址
        ip_address = self._get_client_ip(request)

        # 案件归属：视图可显式标注，否则按路径反查（含 /api/cases/<pk>/ 下的写操作），
        # 用于案件范围的历史审计
        case_id = getattr(request, '_audit_case_id', None)
        if case_id is None:
            case_id = _resolve_case_id(path)

        # 创建日志记录
        try:
            OperationLog.objects.create(
                user=user,
                case_id=case_id,
                action=action,
                module=module,
                detail=f"{method} {path}",
                ip_address=ip_address,
                user_agent=request.headers.get('User-Agent', '')[:500]
            )
            logger.info(f"Operation logged: {user} - {action} - {module} - case={case_id}")
        except Exception as e:
            logger.error(f"Failed to log operation: {e}")

    def _get_module_name(self, path):
        """根据路径获取模块名"""
        module_map = {
            '/api/auth/': '认证管理',
            '/api/cases/': '案件管理',
            '/api/access-decisions/': '授权审计',
            '/api/goods/': '货物管理',
            '/api/stock-in/': '入库管理',
            '/api/stock-out/': '出库管理',
            '/api/units/': '单位管理',
            '/api/categories/': '品类管理',
            '/api/varieties/': '品种管理',
            '/api/attendance-persons/': '考勤人员管理',
            '/api/stock-out-persons/': '出库人员管理',
            '/api/approvals/': '审批管理',
            '/api/warnings/': '预警管理',
        }

        for prefix, name in module_map.items():
            if path.startswith(prefix):
                return name
        return '系统操作'

    def _get_client_ip(self, request):
        """获取客户端IP地址"""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
