"""
onlineretailpos URL Configuration
"""
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.staticfiles.storage import staticfiles_storage
from django.urls import path, re_path
from django.views.generic.base import RedirectView
from django.views.static import serve

from . import views as views
from cart import views as cart_views
from inventory import views as inventory_views
from transaction import views as transaction_views


urlpatterns = [
    # ============================================================
    # Admin
    # ============================================================
    path("staff_portal/", admin.site.urls),

    # ============================================================
    # Auth
    # ============================================================
    path("user/login/", views.user_login, name="user_login"),
    path("user/logout/", views.user_logout, name="user_logout"),
    path(
        "user/change-password/",
        auth_views.PasswordChangeView.as_view(
            template_name="registration/change_password.html",
            success_url="/",
        ),
        name="change_password",
    ),

    # ============================================================
    # Dashboards
    # ============================================================
    path("", views.dashboard_sales, name="home"),
    path("dashboard_sales/", views.dashboard_sales, name="dashboard_sales"),
    path("dashboard_department/", views.dashboard_department, name="dashboard_department"),
    path("dashboard_products/", views.dashboard_products, name="dashboard_products"),
    path("department_report/<start_date>/<end_date>/", views.report_regular, name="department_report"),

    # ============================================================
    # Inventory
    # ============================================================
    path("inventory/", inventory_views.inventoryAdd, name="inventory_add"),
    path("inventory/history/", inventory_views.inventory_history, name="inventory_history"),
    path(
        "inventory/history/<int:product_id>/",
        inventory_views.inventory_history,
        name="inventory_history_product",
    ),
    path("inventory/adjust/", inventory_views.stock_adjustment, name="stock_adjustment"),
    path(
        "inventory/adjustments/",
        inventory_views.stock_adjustment_history,
        name="stock_adjustment_history",
    ),
    path(
        "inventory/adjustments/<int:product_id>/",
        inventory_views.stock_adjustment_history,
        name="stock_adjustment_history_product",
    ),

    # Product lookup must be explicit.
    path(
        "register/product_lookup/",
        inventory_views.product_lookup,
        name="product_lookup_default",
    ),

    # Manual amount must use /register/manual/... so it can NEVER conflict
    # with /register/recall_transaction/<id>/.
    path(
        "register/manual/<str:manual_department>/<str:amount>/",
        inventory_views.manualAmount,
        name="manual_amount",
    ),

    # ============================================================
    # Register / POS
    # ============================================================
    path("register/", views.register, name="register"),
    path("register/ProductNotFound/", views.register, name="ProductNotFound"),
    path("register/cart_clear/", cart_views.cart_clear, name="cart_clear"),
    path(
        "register/returns_transaction/",
        transaction_views.returnsTransaction,
        name="returns_transaction",
    ),
    path(
        "register/suspend_transaction/",
        transaction_views.suspendTransaction,
        name="suspend_transaction",
    ),
    path(
        "register/recall_transaction/",
        transaction_views.recallTransaction,
        name="recall_transaction",
    ),
    path(
        "register/recall_transaction/<str:recallTransNo>/",
        transaction_views.recallTransaction,
        name="recall_transaction_no",
    ),

    path("transaction/stock-report/", transaction_views.stock_report, name="stock_report"),

    # ============================================================
    # Cart
    # ============================================================
    path("cart/add/<str:id>/<int:qty>/", cart_views.cart_add, name="cart_add"),
    path("cart/item_clear/<str:id>/", cart_views.item_clear, name="item_clear"),
    path("cart/item_increment/<str:id>/", cart_views.item_increment, name="item_increment"),
    path("cart/item_decrement/<str:id>/", cart_views.item_decrement, name="item_decrement"),
    path("ajax/product_search/", cart_views.product_search, name="product_search"),
    path("cart/add_ajax/", cart_views.cart_add_ajax, name="cart_add_ajax"),
    path(
        "cart/update_quantity/",
        cart_views.cart_update_quantity,
        name="cart_update_quantity",
    ),
    path(
        "cart/void_item_ajax/",
        cart_views.cart_void_item_ajax,
        name="cart_void_item_ajax",
    ),

    # ============================================================
    # Transactions / Receipts
    # ============================================================
    path(
        "endTransaction/debt/",
        transaction_views.endDebtTransaction,
        name="end_debt_transaction",
    ),
    path(
        "endTransaction/<str:type>/<str:value>/",
        transaction_views.endTransaction,
        name="endTransaction",
    ),
    path(
        "endTransaction/<str:transNo>/",
        transaction_views.endTransactionReceipt,
        name="endTransactionReceipt",
    ),
    path("transaction/", transaction_views.transactionView, name="transactionView"),
    path(
        "transaction/view/<str:transNo>/",
        transaction_views.transactionView,
        name="transactionView_id",
    ),
    path(
        "transaction_receipt/<str:transNo>/",
        transaction_views.transactionReceipt,
        name="transactionReceipt",
    ),
    path(
        "transaction_receipt/<str:transNo>/print/",
        transaction_views.transactionPrintReceipt,
        name="transactionPrintReceipt",
    ),

    # ============================================================
    # Debts
    # ============================================================
    path("transaction/debts/", transaction_views.debts_list, name="debts_list"),
    path("transaction/debt/<int:debt_id>/", transaction_views.debt_detail, name="debt_detail"),
    path(
        "transaction/debt/<int:debt_id>/payment/",
        transaction_views.debt_payment,
        name="debt_payment",
    ),
    path(
        "transaction/debt/<int:debt_id>/payments/",
        transaction_views.debt_payments_history,
        name="debt_payments_history",
    ),
    path(
        "transaction/debt/<int:debt_id>/pay/",
        transaction_views.pay_debt,
        name="pay_debt",
    ),

    # ============================================================
    # Expenses / Profit & Loss
    # ============================================================
    path("transaction/expenses/add/", transaction_views.expenses_add, name="expenses_add"),
    path("transaction/expenses/", transaction_views.expenses_list, name="expenses_list"),
    path("transaction/profit-loss/", transaction_views.profit_loss, name="profit_loss"),

    # ============================================================
    # End Day / Close Day
    # ============================================================
    path("end-day/", transaction_views.end_day_home, name="end_day_home"),
    path("end-day/close/", transaction_views.close_day_action, name="close_day_action"),
    path("end-day/history/", transaction_views.day_close_history, name="day_close_history"),

    # ============================================================
    # Customer Display
    # ============================================================
    path("retail_display/", views.retail_display, name="retail_display"),
    path("retail_display/<str:values>/", views.retail_display, name="retail_display_values"),
    path("qz/cert/", transaction_views.qz_certificate, name="qz_certificate"),
    path("qz/sign/", transaction_views.qz_sign, name="qz_sign"),

    # ============================================================
    # Misc
    # ============================================================
    re_path(
        r"^favicon.ico/*",
        RedirectView.as_view(
            url=staticfiles_storage.url("/img/cash-register-g87e120a86_640.png")
        ),
    ),

    # Static Files Serve WHEN Debug is False in DEV ENV
    re_path(r"^static/(?P<path>.*)$", serve, {"document_root": settings.STATIC_ROOT}),
]