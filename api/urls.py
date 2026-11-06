from django.urls import include, path
from rest_framework.routers import DefaultRouter
from api.analytics import UsageAnalyticsView

from api.views import ApplyTemplateView, BatchMetadataView, BidCostingView, DownloadView, HRCHistoryMarketPriceView, HRCLatestMarketPriceView, HeaderView, ImportHistoryDetailView, ImportHistoryView, ImportRowUpdateView, ImportReviewRowCreateView, ImportReviewRowDeleteView, LineItemGroupSearchView, MappingView, MarketPriceView, PreviewView, ProductResolutionView, SaveTemplateView, StartImportView, SystemFieldsView, TemplateViewSet, UploadView,BulkUploadView,BulkImportStatusView,BulkReviewListView,BulkReviewImportView,BulkReviewSkipView,LineItemRecordUpdateView,PurchaseQuoteDownloadDashboardView,PurchaseQuoteDownloadFileView,PurchaseAutoExtractionStartView,RegisterView,LoginView,CurrentUserView,LogoutView,UserManagementView,ForgotPasswordView,ResetPasswordView,ChangePasswordView

router = DefaultRouter()
router.register(r"import/templates", TemplateViewSet, basename="import-template")

urlpatterns = [
    path("analytics/usage/", UsageAnalyticsView.as_view()),
    path("auth/register/", RegisterView.as_view()),
    path("auth/login/", LoginView.as_view()),
    path("auth/forgot-password/", ForgotPasswordView.as_view()),
    path("auth/reset-password/", ResetPasswordView.as_view()),
    path("auth/change-password/", ChangePasswordView.as_view()),
    path("auth/me/", CurrentUserView.as_view()),
    path("auth/logout/", LogoutView.as_view()),
    path("auth/users/", UserManagementView.as_view()),
    path("auth/users/<int:user_id>/", UserManagementView.as_view()),
    path("", include(router.urls)),
    path("import/system-fields/", SystemFieldsView.as_view()),
    path("import/upload/", UploadView.as_view()),
    path("import/<int:batch_id>/metadata/", BatchMetadataView.as_view()),
    path("import/<int:batch_id>/rows/<int:row_id>/", ImportRowUpdateView.as_view()),
    path("import/<int:batch_id>/review-rows/", ImportReviewRowCreateView.as_view()),
    path("import/<int:batch_id>/review-rows/<int:row_id>/", ImportReviewRowDeleteView.as_view()),
    path("import/<int:batch_id>/header/", HeaderView.as_view()),
    path("import/<int:batch_id>/mapping/", MappingView.as_view()),
    path("import/<int:batch_id>/preview/", PreviewView.as_view()),
    path("import/<int:batch_id>/start/", StartImportView.as_view()),
    path("import/<int:batch_id>/product-resolution/", ProductResolutionView.as_view()),
    path("import/<int:batch_id>/save-template/", SaveTemplateView.as_view()),
    path("import/<int:batch_id>/apply-template/", ApplyTemplateView.as_view()),
    path("line-items/groups/", LineItemGroupSearchView.as_view()),
    path(
    "line-items/<int:row_id>/",
    LineItemRecordUpdateView.as_view(),),
    path("market-prices/", MarketPriceView.as_view()),
    path("market-prices/hrc/latest/", HRCLatestMarketPriceView.as_view()),
    path("market-prices/hrc/history/", HRCHistoryMarketPriceView.as_view()),
    path("bid-costings/", BidCostingView.as_view()),
    path("bid-costings/<int:bid_id>/", BidCostingView.as_view()),
    path("import/history/", ImportHistoryView.as_view()),
    path("import/history/<int:batch_id>/", ImportHistoryDetailView.as_view()),
    path("import/history/<int:batch_id>/download/", DownloadView.as_view()),
    path("import/bulk/upload/", BulkUploadView.as_view()),
    path("import/bulk/<int:bulk_batch_id>/status/", BulkImportStatusView.as_view()),

    path("import/review/", BulkReviewListView.as_view()),
    path("import/review/<int:job_id>/import/", BulkReviewImportView.as_view()),
    path("import/review/<int:job_id>/skip/",BulkReviewSkipView.as_view()),

    path("purchase-quotes/downloads/", PurchaseQuoteDownloadDashboardView.as_view()),
    path("purchase-quotes/downloads/<int:download_id>/file/", PurchaseQuoteDownloadFileView.as_view()),
    path("purchase-quotes/automatic-extraction/start/", PurchaseAutoExtractionStartView.as_view()),

]
