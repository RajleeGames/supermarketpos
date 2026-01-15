# inventory/admin.py
from django.contrib import admin
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.html import format_html

from import_export.admin import ImportExportModelAdmin
from import_export import resources

from .models import Product, Department, Tax, Supplier


# ------------------------------
# ImportExport Resource for Product
# ------------------------------
class ProductResource(resources.ModelResource):
    class Meta:
        model = Product
        fields = (
            "id",
            "barcode",
            "name",
            "sales_price",
            "qty",
            "cost_price",
            "department",
            "department__department_name",
            "department__department_desc",
            "department__department_slug",
            "tax_category",
            "tax_category__tax_category",
            "tax_category__tax_desc",
            "tax_category__tax_percentage",
            "supplier",
            "supplier__name",
            "supplier__phone",
            "supplier__email",
            "supplier__address",
            "is_vat_applicable",
            "low_stock_threshold",
            "product_desc",
        )
        export_order = fields  # keep exported field order consistent


# ------------------------------
# Product Admin
# ------------------------------
@admin.register(Product)
class ProductAdmin(ImportExportModelAdmin):
    resource_class = ProductResource

    list_display = (
        "barcode",
        "name",
        "supplier",
        "sales_price",
        "qty",
        "low_stock_threshold",
        "low_stock_indicator",
        "department",
        "tax_category",
        "is_vat_applicable",
    )

    list_editable = (
        "sales_price",
        "qty",
        "low_stock_threshold",
        "is_vat_applicable",
    )

    list_filter = ("department", "tax_category", "supplier", "is_vat_applicable")
    search_fields = ("name", "barcode")
    ordering = ("-qty", "name")

    readonly_fields = ()

    def low_stock_indicator(self, obj):
        """
        Display a clear visual indicator if product is low on stock.
        Shows: red warning with count left when low, otherwise green OK with count.
        Clicking the value goes to the product change page in admin.
        """
        try:
            change_url = reverse("admin:inventory_product_change", args=[obj.id])
            if obj.is_low_stock():
                return format_html(
                    '<a href="{}" style="color:#b02a37;font-weight:700">⚠ LOW ({} left)</a>',
                    change_url,
                    obj.stock_left,
                )
            else:
                return format_html(
                    '<a href="{}" style="color:#198754;">✓ OK ({} left)</a>',
                    change_url,
                    obj.stock_left,
                )
        except Exception:
            # fallback simple text
            if obj.is_low_stock():
                return format_html('<span style="color:#b02a37;font-weight:700">⚠ LOW</span>')
            return format_html('<span style="color:#198754">✓ OK</span>')

    low_stock_indicator.short_description = "Stock Status"
    low_stock_indicator.admin_order_field = "qty"


# ------------------------------
# Department Admin
# ------------------------------
@admin.register(Department)
class DepartmentAdmin(ImportExportModelAdmin):
    list_display = ("department_name", "department_desc", "products_in_department")

    def products_in_department(self, obj):
        count = Product.objects.filter(department=obj).count()
        url = (
            reverse("admin:inventory_product_changelist")
            + "?"
            + urlencode({"department__id": f"{obj.id}"})
        )
        return format_html(
            '<a href="{}" style="color:green;padding-left:20px">{} Products</a>', url, count
        )

    products_in_department.short_description = "Products"


# ------------------------------
# Tax Admin
# ------------------------------
@admin.register(Tax)
class TaxAdmin(ImportExportModelAdmin):
    list_display = ("tax_category", "tax_percentage", "tax_desc")
    search_fields = ("tax_category",)


# ------------------------------
# Supplier Admin
# ------------------------------
@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email", "address")
    search_fields = ("name", "email", "phone")
