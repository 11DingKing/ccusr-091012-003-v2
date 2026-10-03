"""
仓库管理URL配置
"""
from django.urls import path
from .views import (
    UnitListView, UnitDetailView, UnitBatchDeleteView, UnitAllView,
    CategoryListView, CategoryDetailView, CategoryBatchDeleteView, CategoryAllView,
    VarietyListView, VarietyDetailView, VarietyBatchDeleteView,
    VarietyTemplateView, VarietyImportView,
    GoodsListView, GoodsDetailView,
    StockInListView, StockInDetailView,
    StockOutListView, StockOutDetailView,
    WarningListView, WarningDetailView,
    ApprovalListView, ApprovalDetailView,
)

urlpatterns = [
    # 单位管理
    path('units/', UnitListView.as_view(), name='unit-list'),
    path('units/all/', UnitAllView.as_view(), name='unit-all'),
    path('units/batch-delete/', UnitBatchDeleteView.as_view(), name='unit-batch-delete'),
    path('units/<int:pk>/', UnitDetailView.as_view(), name='unit-detail'),
    
    # 品类管理
    path('categories/', CategoryListView.as_view(), name='category-list'),
    path('categories/all/', CategoryAllView.as_view(), name='category-all'),
    path('categories/batch-delete/', CategoryBatchDeleteView.as_view(), name='category-batch-delete'),
    path('categories/<int:pk>/', CategoryDetailView.as_view(), name='category-detail'),
    
    # 品种管理
    path('varieties/', VarietyListView.as_view(), name='variety-list'),
    path('varieties/batch-delete/', VarietyBatchDeleteView.as_view(), name='variety-batch-delete'),
    path('varieties/template/', VarietyTemplateView.as_view(), name='variety-template'),
    path('varieties/import/', VarietyImportView.as_view(), name='variety-import'),
    path('varieties/<int:pk>/', VarietyDetailView.as_view(), name='variety-detail'),
    
    # 货物管理（案件范围授权）
    path('goods/', GoodsListView.as_view(), name='goods-list'),
    path('goods/<int:pk>/', GoodsDetailView.as_view(), name='goods-detail'),

    # 入库管理（案件范围授权）
    path('stock-in/', StockInListView.as_view(), name='stock-in-list'),
    path('stock-in/<int:pk>/', StockInDetailView.as_view(), name='stock-in-detail'),

    # 出库管理（案件范围授权）
    path('stock-out/', StockOutListView.as_view(), name='stock-out-list'),
    path('stock-out/<int:pk>/', StockOutDetailView.as_view(), name='stock-out-detail'),

    # 预警管理（案件范围授权）
    path('warnings/', WarningListView.as_view(), name='warning-list'),
    path('warnings/<int:pk>/', WarningDetailView.as_view(), name='warning-detail'),

    # 审批管理（案件范围授权）
    path('approvals/', ApprovalListView.as_view(), name='approval-list'),
    path('approvals/<int:pk>/', ApprovalDetailView.as_view(), name='approval-detail'),
]
