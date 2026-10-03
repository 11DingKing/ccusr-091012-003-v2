"""案件模块 URL 配置"""
from django.urls import path

from .views import (
    CaseListView, CaseDetailView, CaseMembershipView,
    CaseMergeView, CaseSplitView, CaseLineageView, AccessDecisionListView,
)

urlpatterns = [
    path('cases/', CaseListView.as_view(), name='case-list'),
    path('cases/<int:pk>/', CaseDetailView.as_view(), name='case-detail'),
    path('cases/<int:case_id>/memberships/', CaseMembershipView.as_view(), name='case-membership'),
    path('cases/merge/', CaseMergeView.as_view(), name='case-merge'),
    path('cases/split/', CaseSplitView.as_view(), name='case-split'),
    path('cases/lineage/', CaseLineageView.as_view(), name='case-lineage'),
    path('access-decisions/', AccessDecisionListView.as_view(), name='access-decision-list'),
]
