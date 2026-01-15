"""
onlineretailpos URL Configuration
"""
from django.views.static import serve
from django.conf import settings
from django.contrib import admin
from django.urls import path, re_path
from transaction import views as transaction_views
from cart import views as cart_views
from . import views as views
from django.contrib.auth import views as auth_views
from inventory import views as inventory_views
from django.contrib.staticfiles.storage import staticfiles_storage
from django.views.generic.base import RedirectView

urlpatterns = [
    # Admin URL
    path('staff_portal/', admin.site.urls ),

    # User URLs
    path("user/login/", views.user_login, name="user_login"),
    path("user/logout/", views.user_logout, name="user_logout"),
    path('user/change-password/', auth_views.PasswordChangeView.as_view(
        template_name='registration/change_password.html',
        success_url='/' ), name='change_password'),

    # Dashboard URLs
    path('', views.dashboard_sales, name="home"),
    path('dashboard_sales/', views.dashboard_sales, name="dashboard_sales"),
    path('dashboard_department/', views.dashboard_department, name="dashboard_department"),
    path('dashboard_products/', views.dashboard_products, name="dashboard_products"),
    path("department_report/<start_date>/<end_date>/", views.report_regular),

    # Inventory
    path('inventory/', inventory_views.inventoryAdd, name="inventory_add"),

    # Register URLs
    path('register/', views.register, name="register"),
    path('register/ProductNotFound/', views.register, name="ProductNotFound"),
    path('register/cart_clear/', cart_views.cart_clear, name='cart_clear'),
    path('register/returns_transaction/', transaction_views.returnsTransaction, name='returns_transaction'),
    path('register/suspend_transaction/', transaction_views.suspendTransaction, name='suspend_transaction'),
    path('register/recall_transaction/', transaction_views.recallTransaction, name='recall_transaction'),
    path('register/recall_transaction/<recallTransNo>/', transaction_views.recallTransaction, name='recall_transaction_no'),
    path('register/product_lookup/', inventory_views.product_lookup, name='product_lookup_default'),
    path('register/<manual_department>/<amount>/', inventory_views.manualAmount, name='manual_amount'),

    # Cart URLs
    path('cart/add/<id>/<qty>/', cart_views.cart_add, name='cart_add'),
    path('cart/item_clear/<id>/', cart_views.item_clear, name='item_clear'),
    # path('cart/item_increment/<id>/',cart_views.item_increment, name='item_increment'),
    # path('cart/item_decrement/<id>/',cart_views.item_decrement, name='item_decrement'),

    # AJAX product search for autocomplete (added)
    path('ajax/product_search/', cart_views.product_search, name='product_search'),

    # Transactions related (specific routes first to avoid catching by the generic <transNo>)
    path('endTransaction/<type>/<value>/', transaction_views.endTransaction, name='endTransaction'),
    path('endTransaction/<transNo>/', transaction_views.endTransactionReceipt, name='endTransactionReceipt'),

    # Transaction listing (exact)
    path('transaction/', transaction_views.transactionView, name='transactionView'),

    # Expenses & Profit/Loss (specific under transaction/)
    path('transaction/expenses/add/', transaction_views.expenses_add, name='expenses_add'),
    path('transaction/expenses/', transaction_views.expenses_list, name='expenses_list'),
    path('transaction/profit-loss/', transaction_views.profit_loss, name='profit_loss'),

    # Transaction receipts (specific)
    path('transaction_receipt/<transNo>/', transaction_views.transactionReceipt, name='transactionReceipt'),
    path('transaction_receipt/<transNo>/print/', transaction_views.transactionPrintReceipt, name='transactionPrintReceipt'),

    # Generic transaction by id (catch-all) — keep this LAST among transaction/ patterns
    path('transaction/<transNo>/', transaction_views.transactionView, name='transactionView_id'),

    # Customer Screen URLs
    path("retail_display/", views.retail_display, name="retail_display"),
    path("retail_display/<values>/", views.retail_display),

    # Other URLs
    re_path(r"^favicon.ico/*", RedirectView.as_view(url=staticfiles_storage.url("/img/cash-register-g87e120a86_640.png"))),

    # Static Files Serve WHEN Debug is False in DEV ENV
    re_path(r'^static/(?P<path>.*)$', serve, {'document_root': settings.STATIC_ROOT}),
]
