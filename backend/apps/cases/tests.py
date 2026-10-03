"""
案件成员关系与资源范围授权测试

覆盖：
- 列表/详情/导出/操作日志四类访问面的统一授权判断
- 授权时间窗、退出立即生效、一人多案件
- 案件合并、拆分后的资源可见性
- 拒绝判定日志中固化的成员关系版本证据
- 退出成员的历史操作仍可被管理员审计
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import OperationLog, User
from apps.warehouse.models import (
    Approval, Category, Goods, StockIn, StockOut, Unit, Variety,
)
from .models import AccessDecisionLog, Case, CaseMembership


class AuthzFixture(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin1", "pass123456", role="admin")
        # 联合调查临时人员
        self.alpha = User.objects.create_user("alpha", "pass123456", role="user")
        self.beta = User.objects.create_user("beta", "pass123456", role="user")
        self.gamma = User.objects.create_user("gamma", "pass123456", role="user")

        self.case_a = Case.objects.create(name="案件甲", case_no="A-001", created_by=self.admin)
        self.case_b = Case.objects.create(name="案件乙", case_no="B-001", created_by=self.admin)
        self.case_c = Case.objects.create(name="案件丙", case_no="C-001", created_by=self.admin)

        self.unit = Unit.objects.create(name="件", created_by=self.admin)
        self.category = Category.objects.create(
            name="受控器材", unit=self.unit, created_by=self.admin)
        self.variety = Variety.objects.create(
            name="记录终端", category=self.category, created_by=self.admin)

        self.goods_a1 = self._goods("甲案终端-1", "A1", self.case_a)
        self.goods_a2 = self._goods("甲案终端-2", "A2", self.case_a)
        self.goods_b1 = self._goods("乙案终端-1", "B1", self.case_b)
        self.goods_c1 = self._goods("丙案终端-1", "C1", self.case_c)

        self.stock_in_a = StockIn.objects.create(
            goods=self.goods_a1, operator=self.admin, quantity=Decimal("3"))
        self.stock_out_b = StockOut.objects.create(
            goods=self.goods_b1, operator=self.admin, receiver="保管员",
            quantity=Decimal("1"))
        self.approval_b = Approval.objects.create(
            stock_out=self.stock_out_b, approver=self.admin)

    def _goods(self, name, code, case):
        return Goods.objects.create(
            case=case, variety=self.variety, name=name, code=code,
            quantity=Decimal("8"), warning_threshold=Decimal("5"))

    def client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(user)}")
        return client

    def join(self, case, user, **kwargs):
        return case.add_member(user, actor=self.admin, **kwargs)

    def leave(self, case, user, **kwargs):
        return case.remove_member(user, actor=self.admin, **kwargs)


class ScopeListDetailTest(AuthzFixture):
    def test_member_lists_only_assigned_case_goods(self):
        self.join(self.case_a, self.alpha)
        client = self.client_for(self.alpha)

        resp = client.get("/api/goods/")
        self.assertEqual(resp.status_code, 200)
        codes = {row["code"] for row in resp.json()["data"]["list"]}
        self.assertEqual(codes, {"A1", "A2"})

    def test_member_cannot_see_other_case_detail(self):
        self.join(self.case_a, self.alpha)
        client = self.client_for(self.alpha)

        ok = client.get(f"/api/goods/{self.goods_a1.id}/")
        self.assertEqual(ok.status_code, 200)

        denied = client.get(f"/api/goods/{self.goods_b1.id}/")
        self.assertEqual(denied.status_code, 403)

    def test_user_without_membership_sees_nothing(self):
        client = self.client_for(self.gamma)
        resp = client.get("/api/goods/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 0)

        detail = client.get(f"/api/goods/{self.goods_a1.id}/")
        self.assertEqual(detail.status_code, 403)

    def test_stock_in_out_warning_approval_chain_scoping(self):
        self.join(self.case_a, self.alpha)
        self.join(self.case_b, self.beta)
        ca, cb = self.client_for(self.alpha), self.client_for(self.beta)

        self.assertEqual(ca.get("/api/stock-in/").json()["data"]["total"], 1)
        self.assertEqual(ca.get("/api/stock-out/").json()["data"]["total"], 0)
        self.assertEqual(cb.get("/api/stock-out/").json()["data"]["total"], 1)
        self.assertEqual(cb.get("/api/approvals/").json()["data"]["total"], 1)
        self.assertEqual(ca.get("/api/approvals/").json()["data"]["total"], 0)

        self.assertEqual(
            ca.get(f"/api/stock-in/{self.stock_in_a.id}/").status_code, 200)
        self.assertEqual(
            ca.get(f"/api/stock-out/{self.stock_out_b.id}/").status_code, 403)

    def test_explicit_case_param_cannot_escape_scope(self):
        self.join(self.case_a, self.alpha)
        client = self.client_for(self.alpha)

        resp = client.get(f"/api/goods/?case={self.case_b.id}")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["data"]["reason"], "not_member")

        # 自己的案件可以显式限定
        ok = client.get(f"/api/goods/?case={self.case_a.id}")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["data"]["total"], 2)

    def test_member_multi_case_sees_union_only(self):
        self.join(self.case_a, self.alpha)
        self.join(self.case_b, self.alpha)
        resp = self.client_for(self.alpha).get("/api/goods/")
        codes = {row["code"] for row in resp.json()["data"]["list"]}
        self.assertEqual(codes, {"A1", "A2", "B1"})  # 不含 C1

    def test_admin_sees_everything(self):
        resp = self.client_for(self.admin).get("/api/goods/")
        self.assertEqual(resp.json()["data"]["total"], 4)

    def test_dashboard_http_is_scoped(self):
        self.join(self.case_a, self.alpha)
        data = self.client_for(self.alpha).get("/api/dashboard/").json()["data"]
        # 甲案 2 件货物；乙、丙案的不进入统计
        self.assertEqual(data["goods_count"], 2)
        admin_data = self.client_for(self.admin).get("/api/dashboard/").json()["data"]
        self.assertEqual(admin_data["goods_count"], 4)


class TimeWindowTest(AuthzFixture):
    def test_future_valid_from_denied(self):
        self.join(self.case_a, self.alpha,
                  valid_from=timezone.now() + timedelta(days=1))
        client = self.client_for(self.alpha)
        self.assertEqual(client.get("/api/goods/").json()["data"]["total"], 0)
        denied = client.get(f"/api/goods/{self.goods_a1.id}/")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["data"]["reason"], "out_of_window")

    def test_expired_valid_to_denied(self):
        self.join(self.case_a, self.alpha,
                  valid_to=timezone.now() - timedelta(hours=1))
        client = self.client_for(self.alpha)
        denied = client.get(f"/api/goods/{self.goods_a1.id}/")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["data"]["reason"], "out_of_window")

    def test_adjust_window_appends_new_version(self):
        v1 = self.join(self.case_a, self.alpha)
        self.case_a.adjust_member(
            self.alpha, valid_to=timezone.now() - timedelta(hours=1),
            actor=self.admin)
        client = self.client_for(self.alpha)
        self.assertEqual(client.get("/api/goods/").json()["data"]["total"], 0)

        versions = list(CaseMembership.objects.filter(
            case=self.case_a, user=self.alpha).order_by("version"))
        self.assertEqual([v.version for v in versions], [1, 2])
        self.assertFalse(versions[0].is_current)
        self.assertEqual(versions[0].end_version, 2)
        self.assertTrue(versions[1].is_current)


class LeaveTest(AuthzFixture):
    def test_access_stops_immediately_after_leave_but_history_auditable(self):
        self.join(self.case_a, self.alpha)
        client = self.client_for(self.alpha)

        # 在案件期间登记一件货物，产生带案件归属的历史操作日志
        created = client.post("/api/goods/", {
            "case": self.case_a.id, "variety": self.variety.id,
            "name": "甲案新终端", "code": "A9",
        }, format="json")
        self.assertEqual(created.status_code, 200)
        new_goods_id = created.json()["data"]["id"]

        self.assertEqual(client.get("/api/goods/").json()["data"]["total"], 3)

        # 退出案件
        self.leave(self.case_a, self.alpha, reason="联合调查结束")

        # 新的访问立即停止（JWT 仍有效，不依赖重新登录）
        self.assertEqual(client.get("/api/goods/").json()["data"]["total"], 0)
        denied = client.get(f"/api/goods/{new_goods_id}/")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["data"]["reason"], "left_case")

        # 历史操作仍可被管理员按案件审计
        admin = self.client_for(self.admin)
        logs = admin.get(f"/api/auth/logs/?case={self.case_a.id}").json()["data"]
        self.assertTrue(any(
            log["user"] == self.alpha.id and log["action"] == "create"
            for log in logs["list"]))

    def test_leave_records_terminal_version_chain(self):
        self.join(self.case_a, self.alpha)
        terminal = self.leave(self.case_a, self.alpha)
        self.assertFalse(terminal.is_current)
        self.assertEqual(terminal.change_type, "leave")
        self.assertEqual(terminal.version, 2)
        self.assertFalse(
            CaseMembership.objects.filter(
                case=self.case_a, user=self.alpha, is_current=True).exists())


class MergeSplitTest(AuthzFixture):
    def test_merge_moves_resources_without_extending_membership(self):
        from .services import merge_cases

        self.join(self.case_a, self.alpha)
        self.join(self.case_b, self.beta)
        merge_cases(source=self.case_a, target=self.case_b, actor=self.admin)

        ca, cb = self.client_for(self.alpha), self.client_for(self.beta)

        # alpha 未加入乙案，合并后看不到已迁走的原甲案货物
        self.assertEqual(ca.get("/api/goods/").json()["data"]["total"], 0)
        denied = ca.get(f"/api/goods/{self.goods_a1.id}/")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["data"]["case_id"], self.case_b.id)

        # 拒绝依据：成员关系版本挂在已合并走的源案件上
        admin = self.client_for(self.admin)
        audit = admin.get(
            "/api/access-decisions/?decision=deny").json()["data"]["list"]
        merge_denial = next(
            d for d in audit if d["reason"] == "merged_source_not_member")
        self.assertEqual(merge_denial["membership_snapshot"]["case_id"], self.case_a.id)
        self.assertEqual(merge_denial["case"], self.case_b.id)

        # beta 可见乙案全部（含合并进来的）
        codes = {r["code"] for r in cb.get("/api/goods/").json()["data"]["list"]}
        self.assertEqual(codes, {"A1", "A2", "B1"})

        # 合并后把 alpha 显式加入乙案，立即恢复可见
        self.join(self.case_b, self.alpha)
        self.assertEqual(ca.get("/api/goods/").json()["data"]["total"], 3)

    def test_split_keeps_resources_separate(self):
        from .services import create_case, split_case

        self.join(self.case_a, self.alpha)
        case_d = create_case(name="拆方案件", case_no="D-001", actor=self.admin)
        split_case(source=self.case_a, new_case=case_d,
                   goods_ids=[self.goods_a1.id], actor=self.admin)

        client = self.client_for(self.alpha)
        codes = {r["code"] for r in client.get("/api/goods/").json()["data"]["list"]}
        self.assertEqual(codes, {"A2"})  # 拆出的 A1 不可见
        self.assertEqual(client.get(f"/api/goods/{self.goods_a1.id}/").status_code, 403)

        # 新案件成员只能看到拆出的资源
        self.join(case_d, self.gamma)
        gc = self.client_for(self.gamma).get("/api/goods/")
        self.assertEqual({r["code"] for r in gc.json()["data"]["list"]}, {"A1"})


class ExportScopingTest(AuthzFixture):
    @staticmethod
    def _xlsx_text(response):
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(b"".join(response.streaming_content))
                           if response.streaming else io.BytesIO(response.content))
        ws = wb.active
        return "\n".join(
            "".join(str(cell.value or "") for cell in row)
            for row in ws.iter_rows())

    def test_export_respects_scope_and_case_param(self):
        self.join(self.case_a, self.alpha)
        client = self.client_for(self.alpha)

        resp = client.get("/api/export/?type=goods")
        self.assertEqual(resp.status_code, 200)
        text = self._xlsx_text(resp)
        self.assertIn("甲案终端-1", text)
        self.assertIn("A-001", text)
        self.assertNotIn("乙案终端-1", text)

        denied = client.get(f"/api/export/?type=goods&case={self.case_b.id}")
        self.assertEqual(denied.status_code, 403)

    def test_member_export_stock_in_only_own_case(self):
        self.join(self.case_a, self.alpha)
        resp = self.client_for(self.alpha).get("/api/export/?type=stock_in")
        self.assertEqual(resp.status_code, 200)
        text = self._xlsx_text(resp)
        self.assertIn("A-001", text)
        self.assertNotIn("B-001", text)


class CreateGuardingTest(AuthzFixture):
    def test_member_cannot_register_goods_into_other_case(self):
        self.join(self.case_a, self.alpha)
        client = self.client_for(self.alpha)
        resp = client.post("/api/goods/", {
            "case": self.case_b.id, "variety": self.variety.id,
            "name": "越权登记", "code": "X1",
        }, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Goods.objects.filter(code="X1").exists())

    def test_non_admin_cannot_manage_cases(self):
        resp = self.client_for(self.alpha).post("/api/cases/", {
            "name": "私建案件", "case_no": "Z-001"}, format="json")
        self.assertEqual(resp.status_code, 403)


class OperationLogScopeTest(AuthzFixture):
    def test_logs_scoped_by_case_and_param_guarded(self):
        self.join(self.case_a, self.alpha)
        self.join(self.case_b, self.beta)
        # alpha 在甲案产生一条操作
        self.client_for(self.alpha).post("/api/goods/", {
            "case": self.case_a.id, "variety": self.variety.id,
            "name": "日志货物", "code": "LOG1"}, format="json")

        client = self.client_for(self.beta)
        # 乙案成员看不到甲案日志
        rows = client.get(f"/api/auth/logs/?case={self.case_a.id}")
        self.assertEqual(rows.status_code, 403)

        # 不带 case 时也只含授权案件（乙案），不含甲案
        logs = client.get("/api/auth/logs/").json()["data"]["list"]
        self.assertFalse(any(log.get("case") == self.case_a.id for log in logs))


class AccessDecisionAuditTest(AuthzFixture):
    def test_deny_log_pins_membership_version_for_admin(self):
        self.join(self.case_a, self.alpha,
                  valid_to=timezone.now() - timedelta(hours=1))
        client = self.client_for(self.alpha)
        resp = client.get(f"/api/goods/{self.goods_a1.id}/")
        self.assertEqual(resp.status_code, 403)

        admin = self.client_for(self.admin)
        denials = admin.get(
            "/api/access-decisions/?decision=deny").json()["data"]["list"]
        self.assertTrue(denials)
        latest = denials[0]
        self.assertEqual(latest["user"], self.alpha.id)
        self.assertEqual(latest["resource_type"], "goods")
        self.assertEqual(latest["reason"], "out_of_window")
        # 管理员能看到拒绝所依据的成员关系版本及其快照
        self.assertEqual(latest["membership_version_number"], 1)
        self.assertEqual(latest["membership_snapshot"]["version"], 1)
        self.assertEqual(latest["membership_snapshot"]["case_id"], self.case_a.id)
        self.assertEqual(latest["case_version"], self.case_a.version)
        self.assertTrue(latest["request_id"])

    def test_leave_denial_pins_terminal_version(self):
        self.join(self.case_a, self.alpha)
        self.leave(self.case_a, self.alpha)
        self.client_for(self.alpha).get(f"/api/goods/{self.goods_a1.id}/")

        admin = self.client_for(self.admin)
        denials = admin.get(
            "/api/access-decisions/?decision=deny&reason="
        ).json()["data"]["list"]
        leave_denials = [d for d in denials if d["reason"] == "left_case"]
        self.assertTrue(leave_denials)
        self.assertEqual(leave_denials[0]["membership_version_number"], 2)
        self.assertEqual(
            leave_denials[0]["membership_snapshot"]["change_type"], "leave")

    def test_member_cannot_read_decision_logs(self):
        resp = self.client_for(self.alpha).get("/api/access-decisions/")
        self.assertEqual(resp.status_code, 403)

    def test_merge_denial_audited_with_inactive_case_reason(self):
        from .services import merge_cases

        self.join(self.case_a, self.alpha)
        merge_cases(source=self.case_a, target=self.case_b, actor=self.admin)
        self.client_for(self.alpha).get(f"/api/goods/{self.goods_a1.id}/")

        admin = self.client_for(self.admin)
        denials = admin.get("/api/access-decisions/?decision=deny").json()["data"]["list"]
        # 资源现属乙案，alpha 的成员关系仅挂在已合并走的甲案上
        self.assertTrue(
            any(d["reason"] == "merged_source_not_member" for d in denials))
