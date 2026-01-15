# inventory/models.py
from django.db import models
from django.template.defaultfilters import slugify
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation, getcontext

# ensure Decimal precision is generous
getcontext().prec = 28

PERCENTAGE_VALIDATOR = [MinValueValidator(0), MaxValueValidator(100)]


class Supplier(models.Model):
    """Supplier / vendor for products."""
    name = models.CharField(max_length=100, unique=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    def __str__(self) -> str:
        return self.name

    class Meta:
        verbose_name = "Supplier"
        verbose_name_plural = "Suppliers"


class Department(models.Model):
    department_name = models.CharField(max_length=32, unique=True, null=False, blank=False)
    department_desc = models.TextField(blank=True)
    department_slug = models.SlugField(max_length=32, unique=True, blank=True)

    def __str__(self) -> str:
        return self.department_name

    def save(self, *args, **kwargs):
        self.department_slug = slugify(self.department_name)
        return super().save(*args, **kwargs)

    class Meta:
        verbose_name = "Department"
        verbose_name_plural = "Departments"


class Tax(models.Model):
    """
    Tax category model.
    Example: tax_category='VAT', tax_percentage=18.000
    """
    tax_category = models.CharField(max_length=32, unique=True, null=False, blank=False)
    tax_desc = models.TextField(blank=True)
    tax_percentage = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        validators=PERCENTAGE_VALIDATOR,
        null=False,
        blank=False,
    )

    def __str__(self) -> str:
        return self.tax_category

    @property
    def percentage_decimal(self) -> Decimal:
        """Return tax percentage as Decimal fraction (e.g., 18 -> Decimal('0.18'))."""
        try:
            return (Decimal(self.tax_percentage) / Decimal("100")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        except Exception:
            return Decimal("0.00")

    class Meta:
        verbose_name = "Tax"
        verbose_name_plural = "Tax Information"


class Product(models.Model):
    """
    Product model — linked to a supplier and department.

    Notes:
    - `is_vat_applicable` is the single boolean toggle that controls whether VAT is applied.
      (We removed `is_taxable` to avoid confusion — use only `is_vat_applicable`.)
    - `tax_category` is optional. If missing and `is_vat_applicable` is True, default VAT is 18%.
    - VAT calculations use VAT-INCLUSIVE extraction (i.e., VAT portion inside the sales price).
    """
    department = models.ForeignKey("Department", on_delete=models.RESTRICT, null=False, blank=False)
    supplier = models.ForeignKey("Supplier", on_delete=models.RESTRICT, null=False, blank=False)

    barcode = models.CharField(unique=True, max_length=16, blank=False, null=False)
    name = models.CharField(max_length=125, blank=False, null=False)

    sales_price = models.DecimalField(max_digits=12, decimal_places=2, null=False, blank=False)
    qty = models.IntegerField(default=0, null=False)
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), null=False)

    # tax_category is optional now: you can leave blank and system will default to 18% when VAT applies
    tax_category = models.ForeignKey("Tax", on_delete=models.RESTRICT, null=True, blank=True)

    product_desc = models.TextField(blank=True, null=True)

    # Keep only this toggle as the canonical VAT applicability flag
    is_vat_applicable = models.BooleanField(default=True)

    # per-product low stock threshold (default 5)
    low_stock_threshold = models.PositiveIntegerField(default=5)

    def __str__(self) -> str:
        return f"{self.name} ({self.barcode})"

    def get_fields(self):
        """Short summary used in templates/admin displays."""
        return [
            ("Barcode", self.barcode),
            ("Name", self.name),
            ("Price", self.sales_price),
            ("Department Category", self.department.department_name if self.department else ""),
            ("Tax Category", self.tax_category.tax_category if self.tax_category else "Default (18%)"),
            ("Supplier", self.supplier.name if self.supplier else ""),
            ("VAT Applicable", "Yes" if self.is_vat_applicable else "No"),
            ("Low Stock Threshold", self.low_stock_threshold),
        ]

    def get_fields_2(self):
        """Detailed summary for inspection screens."""
        return [
            ("Barcode", self.barcode),
            ("Name", self.name),
            ("Inventory Qty", self.qty),
            ("Sales Price", self.sales_price),
            ("Cost Price", self.cost_price),
            ("Department Category", self.department.department_name if self.department else ""),
            ("Tax Category", self.tax_category.tax_category if self.tax_category else "Default (18%)"),
            ("Tax Percentage", self.tax_category.tax_percentage if self.tax_category else 18),
            ("Supplier", self.supplier.name if self.supplier else ""),
            ("VAT Applicable", "Yes" if self.is_vat_applicable else "No"),
            ("Low Stock Threshold", self.low_stock_threshold),
        ]

    def is_low_stock(self):
        """
        Returns True if current stock is less than or equal to the low stock threshold.
        """
        try:
            return int(self.qty or 0) <= int(self.low_stock_threshold or 0)
        except (TypeError, ValueError):
            return False

    @property
    def stock_left(self):
        """Return current stock left as integer (safe)."""
        try:
            return int(self.qty or 0)
        except (TypeError, ValueError):
            return 0

    def can_fulfill(self, requested_qty=1):
        """
        Return True if the requested quantity can be fulfilled without going negative.
        """
        try:
            return int(requested_qty) <= int(self.qty or 0)
        except (TypeError, ValueError):
            return False

    # ---- VAT helper properties/methods ----
    @property
    def tax_percentage(self) -> Decimal:
        """
        Return tax percentage as Decimal 0..100.
        If tax_category is present, use it; otherwise default to 18 (Tanzania) when VAT applies,
        otherwise 0.
        """
        try:
            if not self.is_vat_applicable:
                return Decimal("0.00")
            if self.tax_category:
                return Decimal(self.tax_category.tax_percentage).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            # default to 18% when VAT applies and no category set
            return Decimal("18.000")
        except Exception:
            return Decimal("0.00")

    @property
    def tax_fraction(self) -> Decimal:
        """
        Return tax as fraction (e.g., 18 -> Decimal('0.18')).
        """
        try:
            return (self.tax_percentage / Decimal("100.00")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        except Exception:
            return Decimal("0.00")

    def extract_vat_from_gross(self, gross: Decimal, qty: int = 1) -> Decimal:
        """
        Extract VAT portion from a VAT-INCLUSIVE gross amount.
        Formula: extracted = gross * pct / (100 + pct)  where pct is percent (e.g., 18)
        We accept gross as total for qty (or single unit if qty=1).
        Returns Decimal quantized to 2 dp.
        """
        try:
            gross = Decimal(gross)
            pct = self.tax_percentage  # percent like 18
            if not self.is_vat_applicable or pct == Decimal("0.00") or gross == Decimal("0.00"):
                return Decimal("0.00")
            denom = pct + Decimal("100.00")
            vat = (gross * pct / denom).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return vat
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0.00")

    def get_tax_amount_on_sale(self, qty: int = 1) -> Decimal:
        """
        Return the extracted VAT amount (Decimal) for a given quantity based on sales_price.
        This assumes sales_price is VAT-INCLUSIVE (common TRA approach).
        Example: for sales_price=500 and pct=18, extracted VAT = 500 * 18 / 118.
        """
        try:
            if not self.is_vat_applicable:
                return Decimal("0.00")
            unit_gross = (Decimal(self.sales_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            total_gross = (unit_gross * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return self.extract_vat_from_gross(total_gross, qty=qty)
        except (InvalidOperation, AttributeError, TypeError):
            return Decimal("0.00")

    def get_tax_amount_on_cost(self, qty: int = 1) -> Decimal:
        """
        Return extracted VAT amount based on cost_price for the given quantity.
        This is useful if you want to compute VAT effect on cost (internal accounting).
        Uses same extraction formula but applied to cost_price instead of sales_price.
        """
        try:
            if not self.is_vat_applicable:
                return Decimal("0.00")
            unit_cost = (Decimal(self.cost_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            total_cost = (unit_cost * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return self.extract_vat_from_gross(total_cost, qty=qty)
        except (InvalidOperation, AttributeError, TypeError):
            return Decimal("0.00")

    def get_line_total(self, qty: int = 1) -> Decimal:
        """
        Return line total that the customer pays for qty units.
        Since we treat sales_price as VAT-INCLUSIVE, the customer pays sales_price * qty.
        This method does NOT add VAT on top.
        """
        try:
            price_total = (Decimal(self.sales_price) * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return price_total
        except (InvalidOperation, TypeError, ValueError, AttributeError):
            return Decimal("0.00")

    # ---- Validation and save ----
    def clean(self):
        """
        Ensure qty and threshold are non-negative before saving.
        Called by model forms and can be invoked manually.
        """
        super_clean = getattr(super(Product, self), "clean", None)
        if callable(super_clean):
            super_clean()
        if self.qty is None:
            self.qty = 0
        if self.qty < 0:
            self.qty = 0
        if self.low_stock_threshold is None:
            self.low_stock_threshold = 5
        if self.low_stock_threshold < 0:
            self.low_stock_threshold = 0

    def save(self, *args, **kwargs):
        """
        Run clean to guard values then save.
        """
        try:
            self.clean()
        except Exception:
            # If clean raises, fallback to safe defaults to avoid breaking save.
            if self.qty is None:
                self.qty = 0
            if self.low_stock_threshold is None:
                self.low_stock_threshold = 5
        return super().save(*args, **kwargs)

    class Meta:
        verbose_name = "Product"
        verbose_name_plural = "Products"
        ordering = ["name"]
