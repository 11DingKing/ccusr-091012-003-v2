"""
案件授权模块URL配置
"""
from django.urls import path
from .views import (
    AccessDecisionLogListView,
    CaseListView, CaseDetailView,
    CaseMembershipListView,
    CaseMemberJoinView, CaseMemberAdjustView, CaseMemberLeaveView,
    CaseMergeView, CaseSplitView, CaseEventListView,
)

urlpatterns = [
    # 案件管理
    path('cases/', CaseListView.as_view(), name='case-list'),
    path('cases/<int:pk>/', CaseDetailView.as_view(), name='case-detail'),
    path('cases/<int:pk>/memberships/', CaseMembershipListView.as_view(),
         name='case-memberships'),
    path('cases/<int:pk>/members/join/', CaseMemberJoinView.as_view(),
         name='case-member-join'),
    path('cases/<int:pk>/members/adjust/', CaseMemberAdjustView.as_view(),
         name='case-member-adjust'),
    path('cases/<int:pk>/members/leave/', CaseMemberLeaveView.as_view(),
         name='case-member-leave'),
    path('cases/<int:pk>/merge/', CaseMergeView.as_view(), name='case-merge'),
    path('cases/<int:pk>/split/', CaseSplitView.as_view(), name='case-split'),
    path('cases/<int:pk>/events/', CaseEventListView.as_view(), name='case-events'),

    # 授权判定审计
    path('access-decisions/', AccessDecisionLogListView.as_view(),
         name='access-decision-list'),
]
