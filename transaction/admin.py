from django.contrib import admin
from .models import transaction, productTransaction, Expense
from django.utils.html import format_html
from django.urls import reverse
from django.utils.http import urlencode
from import_export.admin import ImportExportModelAdmin
from rangefilter.filters import DateTimeRangeFilter

# Register your models here.
@admin.register(transaction)
class TransactionAdmin(ImportExportModelAdmin):
    list_display = ("transaction_dt", "transaction_id", "total_sale", "tax_total", "payment_type", "products_link", "receipt_link",)
    fields = ["user", "transaction_dt", "transaction_id", "total_sale", "sub_total", "tax_total", "deposit_total", "payment_type", "receipt", "receipt_link", "products_link"]
    list_filter = (("transaction_dt", DateTimeRangeFilter), "transaction_dt", "user", "payment_type",)
    search_fields = ["transaction_id"]

    def receipt_link(self, obj=None):
        if obj is not None:
            # link to the front-end receipt route (keeps your original behaviour)
            return format_html(f'<a href="/transaction_receipt/{obj.transaction_id}/" style="color:green;" target="_blank">View Receipt</a>')
        return "-"
    receipt_link.short_description = "Receipt"

    def products_link(self, obj):
        count = productTransaction.objects.filter(transaction=obj).count()
        url = (
            reverse("admin:transaction_producttransaction_changelist")
            + "?"
            + urlencode({"transaction__id": f"{obj.id}"})
        )
        return format_html('<a href="{}" style="color:#4e73df;">{} Product Transaction</a>', url, count)
    products_link.short_description = "Products"

    def has_add_permission(self, request, *args):
        return False

    def has_change_permission(self, request, *args):
        return False

    def has_delete_permission(self, request, *args):
        return False

    def has_import_permission(self, request, *args):
        return False

    def get_rangefilter_created_at_title(self, request, field_path):
        return 'Date and Time Filter'

    class Media:
        js = ["js/jquery.js", "js/list_filter_collapse.js",]


@admin.register(productTransaction)
class ProductTransactionAdmin(ImportExportModelAdmin):
    list_display = ("transaction_date_time", "barcode", "name", "qty", "sales_price", "sales_amount", "tax_amount", "deposit_amount", "total_amount", "link_transaction",)
    fields = ["transaction_date_time", "barcode", "name", "department", "qty", "sales_price", "cost_price", "profit_per_item", "Profit_amount", "tax_category", "tax_percentage", "deposit_category", "deposit", "sales_amount", "tax_amount",
        "deposit_amount", "total_amount", "payment_type", "link_transaction",]
    list_filter = [("transaction_date_time", DateTimeRangeFilter), "department", "tax_category", "deposit_category", "payment_type"]
    search_fields = ["transaction_id_num", "barcode", "name",]

    def link_transaction(self, obj=None):
        if obj is not None and getattr(obj, "transaction", None):
            return format_html(
                '<a href="/staff_portal/transaction/transaction/{}/change/" style="color:#4e73df;">{}</a>',
                obj.transaction.id,
                obj.transaction
            )
        return "-"
    link_transaction.short_description = "Transaction"

    def sales_amount(self, obj=None):
        try:
            return obj.qty * obj.sales_price
        except Exception:
            return 0
    sales_amount.short_description = "Sales Amount"

    def total_amount(self, obj=None):
        try:
            return (obj.qty * obj.sales_price) + (obj.tax_amount or 0) + (obj.deposit_amount or 0)
        except Exception:
            return 0
    total_amount.short_description = "Total Amount"

    def profit_per_item(self, obj=None):
        try:
            return obj.sales_price - obj.cost_price
        except Exception:
            return 0
    profit_per_item.short_description = "P/L per Item"

    def Profit_amount(self, obj=None):
        try:
            return (obj.sales_price - obj.cost_price) * obj.qty
        except Exception:
            return 0
    Profit_amount.short_description = "P/L amount"

    def has_add_permission(self, request, *args):
        return False

    def has_change_permission(self, request, *args):
        return False

    def has_delete_permission(self, request, *args):
        return False

    def has_import_permission(self, request, *args):
        return False

    class Media:
        js = ["js/jquery.js", "js/list_filter_collapse.js",]


# --------- Expense admin (new) ----------
@admin.register(Expense)
class ExpenseAdmin(ImportExportModelAdmin):
    list_display = ("created_at", "created_by", "category", "amount", "note_short", "view_link")
    fields = ("created_by", "created_at", "category", "amount", "note")
    readonly_fields = ("created_by", "created_at")
    list_filter = (("created_at", DateTimeRangeFilter), "category")
    search_fields = ("note", "created_by__username")
    ordering = ("-created_at",)

    def note_short(self, obj):
        if not obj.note:
            return "-"
        txt = str(obj.note)
        return txt if len(txt) <= 80 else f"{txt[:77]}..."
    note_short.short_description = "Note"

    def view_link(self, obj):
        if obj is not None:
            # link to admin change page for this expense
            url = reverse("admin:transaction_expense_change", args=(obj.id,))
            return format_html('<a href="{}" style="color:#4e73df;">Open</a>', url)
        return "-"
    view_link.short_description = "Open"

    def save_model(self, request, obj, form, change):
        # automatically set created_by for new expenses
        if not change or not obj.created_by:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    class Media:
        js = ["js/jquery.js", "js/list_filter_collapse.js",]
