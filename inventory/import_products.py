import pandas as pd
from decimal import Decimal
from django.db import transaction

from inventory.models import Product, Department, Supplier, Tax


def run():
    file_path = "products.xlsx"  # <-- your cleaned Excel file

    df = pd.read_excel(file_path)

    with transaction.atomic():
        for _, row in df.iterrows():

            # -------- REQUIRED FIELDS --------
            barcode = str(row['barcode']).strip()
            name = str(row['name']).strip()

            qty = int(row.get('qty', 0) or 0)
            sales_price = Decimal(str(row.get('sales_price', 0) or 0))
            cost_price = Decimal(str(row.get('cost_price', 0) or 0))
            low_stock = int(row.get('low_stock', 5) or 5)

            # -------- VAT FLAG --------
            vat_raw = str(row.get('vat', 'YES')).strip().upper()
            is_vat_applicable = vat_raw in ("YES", "TRUE", "1")

            # -------- DEPARTMENT --------
            dept_name = str(row.get('department', 'General')).strip()
            department, _ = Department.objects.get_or_create(
                department_name=dept_name
            )

            # -------- SUPPLIER --------
            supplier_name = str(row.get('supplier', 'Default Supplier')).strip()
            supplier, _ = Supplier.objects.get_or_create(
                name=supplier_name
            )

            # -------- TAX (OPTIONAL) --------
            tax_category = None
            if is_vat_applicable:
                tax_name = str(row.get('tax_category', 'VAT')).strip()
                tax_percentage = Decimal(str(row.get('tax_percentage', 18)))

                tax_category, _ = Tax.objects.get_or_create(
                    tax_category=tax_name,
                    defaults={'tax_percentage': tax_percentage}
                )

            # -------- CREATE / UPDATE PRODUCT --------
            Product.objects.update_or_create(
                barcode=barcode,
                defaults={
                    'name': name,
                    'qty': qty,
                    'sales_price': sales_price,
                    'cost_price': cost_price,
                    'department': department,
                    'supplier': supplier,
                    'tax_category': tax_category,
                    'is_vat_applicable': is_vat_applicable,
                    'low_stock_threshold': low_stock,
                }
            )

    print("✅ PRODUCTS IMPORTED SUCCESSFULLY")
