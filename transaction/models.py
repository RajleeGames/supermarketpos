# transaction/models.py
from ast import literal_eval
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
import pytz

from django.conf import settings
from django.db import models, transaction as db_transaction
from django.db.models import F
from django.utils import timezone as dj_timezone

from inventory.models import Product, PERCENTAGE_VALIDATOR

# configure decimal precision
getcontext().prec = 28

# Timezone target (from settings)
TZ = pytz.timezone(settings.TIME_ZONE)


def safe_decimal(value, default=Decimal("0.00")):
    """
    Convert value to Decimal safely. Accepts Decimal, int, float, str.
    Returns a quantized Decimal with 2 decimal places or default on error.
    """
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        try:
            return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            return default
    try:
        d = Decimal(str(value))
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return default


# -----------------------------
# Models
# -----------------------------
class transaction(models.Model):
    """
    Transaction header (one row per sale).
    Expects `products` to be a stringifiable/list-like structure with items:
      {'barcode': '123', 'name': 'Milk', 'price': 500, 'quantity': 2, 'tax_value': 76.27, ...}
    The save() method will:
      - parse products
      - compute sub_total (sum of price * qty), tax_total (sum of extracted taxes),
        deposit_total (sum of deposit amounts)
      - set total_sale = sub_total + deposit_total (customer payable; for VAT-inclusive pricing sub_total already includes VAT)
      - save header, then create productTransaction rows if none exist
      - if payment_type == 'DEBT' create Debt and DebtPayment as needed (safe/atomic)
    """
    date_time = models.DateTimeField(auto_now_add=True)
    transaction_dt = models.DateTimeField(editable=False, null=False, blank=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.RESTRICT, null=False, blank=False, editable=False)
    transaction_id = models.CharField(unique=True, max_length=50, editable=False, null=False)

    # money fields
    total_sale = models.DecimalField(max_digits=15, decimal_places=2, null=False, editable=False, default=Decimal("0.00"))
    sub_total = models.DecimalField(max_digits=15, decimal_places=2, null=False, editable=False, default=Decimal("0.00"))
    tax_total = models.DecimalField(max_digits=15, decimal_places=2, null=True, editable=False, default=Decimal("0.00"))
    deposit_total = models.DecimalField(max_digits=15, decimal_places=2, null=True, editable=False, default=Decimal("0.00"))

    # new: amount paid at time of transaction (for partial payments)
    paid_amount = models.DecimalField(max_digits=15, decimal_places=2, null=False, editable=False, default=Decimal("0.00"))

    # payment types: include DEBT option
    PAYMENT_CHOICES = [
        ('CASH', 'CASH'),
        ('DEBIT/CREDIT', 'DEBIT/CREDIT'),
        ('EBT', 'EBT'),
        ('DEBT', 'DEBT'),  # added debt option
    ]
    payment_type = models.CharField(
        choices=PAYMENT_CHOICES,
        max_length=32, null=False, editable=False
    )

    # optional debtor metadata (string fields to avoid coupling if you don't have a Customer model here)
    debtor_name = models.CharField(max_length=200, blank=True, default="", editable=False)
    debt_due_date = models.DateField(null=True, blank=True, editable=False)
    debt_created = models.BooleanField(default=False, editable=False)

    receipt = models.TextField(blank=False, null=False, editable=False)
    # products: expected string repr of list of dicts
    products = models.TextField(blank=False, null=False, editable=False)

    def __str__(self) -> str:
        return str(self.transaction_id)

    def _ensure_transaction_dt_timezone(self):
        """
        Ensure transaction_dt is timezone-aware in TZ.
        If naive -> localize; if aware -> convert to TZ.
        """
        try:
            if self.transaction_dt is None:
                return
            if getattr(self.transaction_dt, "tzinfo", None) is None or self.transaction_dt.tzinfo.utcoffset(self.transaction_dt) is None:
                # naive -> localize
                self.transaction_dt = TZ.localize(self.transaction_dt)
            else:
                # aware -> convert
                self.transaction_dt = self.transaction_dt.astimezone(TZ)
        except Exception:
            pass

    def _extract_vat_from_gross(self, gross: Decimal, pct: Decimal) -> Decimal:
        """
        Extract VAT from a VAT-inclusive gross amount given percentage (pct as percent, e.g. 18).
        Formula: VAT = gross * pct / (100 + pct)
        Returns Decimal quantized to 2 dp.
        """
        try:
            if pct is None or pct == Decimal("0") or gross is None or gross == Decimal("0.00"):
                return Decimal("0.00")
            # ensure pct is Decimal (percent like 18)
            pct_dec = safe_decimal(pct, default=Decimal("0.00"))
            if pct_dec == Decimal("0.00"):
                return Decimal("0.00")
            vat = (gross * pct_dec / (pct_dec + Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return vat
        except Exception:
            return Decimal("0.00")

    from django.utils import timezone
    def save(self, *args, **kwargs):
        """
        Save transaction header, compute totals from `products` payload, and create productTransaction rows.
        Also create Debt and DebtPayment records if payment_type is DEBT.
        This method is defensive and avoids duplicate child rows by checking the DB first.
        """
        # Ensure transaction_dt has proper TZ
        self._ensure_transaction_dt_timezone()

        # Parse products payload early so we can compute header totals before saving header.
        try:
            products_list = literal_eval(self.products) if self.products else []
            if not isinstance(products_list, list):
                products_list = []
        except Exception:
            products_list = []

        # Compute totals from payload (use Decimal arithmetic)
        computed_subtotal = Decimal("0.00")
        computed_tax_total = Decimal("0.00")
        computed_deposit_total = Decimal("0.00")

        for product_item in products_list:
            try:
                price = safe_decimal(product_item.get("price", 0))
                qty = int(product_item.get("quantity", 0) or 0)
                gross_line = (price * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                # deposit: product_item may contain deposit_value/deposit_amount keys (store whichever)
                deposit_amount = safe_decimal(product_item.get("deposit_value", product_item.get("deposit_amount", 0)))

                # Determine tax percentage priority:
                # 1) inventory product tax (if product found)
                # 2) product_item['tax_percentage'] if provided
                # 3) product_item may provide tax_category/tax_percentage fields: fallback to 0
                tax_pct = Decimal("0.00")
                tax_amount = safe_decimal(product_item.get("tax_value", 0))

                # Try to resolve product in inventory to use authoritative tax info/cost price
                item = None
                barcode_raw = str(product_item.get("barcode", "")).strip()
                if barcode_raw:
                    try:
                        item = Product.objects.filter(barcode=barcode_raw).first()
                        if item is None and "_" in barcode_raw:
                            # try base before underscore
                            base_barcode = barcode_raw.split("_")[0]
                            item = Product.objects.filter(barcode=base_barcode).first()
                    except Exception:
                        item = None

                if item:
                    # primary tax percentage comes from inventory's tax_category
                    try:
                        tax_pct = safe_decimal(getattr(getattr(item, "tax_category", None), "tax_percentage", 0))
                    except Exception:
                        tax_pct = Decimal("0.00")
                else:
                    # no inventory item found -> check payload for tax_percentage
                    try:
                        tax_pct = safe_decimal(product_item.get("tax_percentage", product_item.get("tax_pct", 0)))
                    except Exception:
                        tax_pct = Decimal("0.00")

                # If payload already provided tax_value (total tax for line) and it's > 0, use it.
                # Otherwise extract VAT from gross_line using tax_pct (VAT-inclusive pricing).
                if tax_amount == Decimal("0.00") and tax_pct != Decimal("0.00"):
                    tax_amount = self._extract_vat_from_gross(gross_line, tax_pct)
                # else tax_amount stays as provided (or zero)

                # accumulate
                computed_subtotal += gross_line
                computed_tax_total += tax_amount
                computed_deposit_total += deposit_amount
            except Exception:
                # on error with one item, skip and continue
                continue

        # Quantize totals
        computed_subtotal = computed_subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        computed_tax_total = computed_tax_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        computed_deposit_total = computed_deposit_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Assign computed totals to header (this ensures receipts and reports are auditable)
        # total_sale represents customer payable amount; for VAT-inclusive pricing computed_subtotal already includes VAT
        self.sub_total = computed_subtotal
        self.tax_total = computed_tax_total
        self.deposit_total = computed_deposit_total
        self.total_sale = (computed_subtotal + computed_deposit_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Normalize numeric fields (defensive)
        self.total_sale = safe_decimal(self.total_sale)
        self.sub_total = safe_decimal(self.sub_total)
        self.tax_total = safe_decimal(self.tax_total)
        self.deposit_total = safe_decimal(self.deposit_total)
        self.paid_amount = safe_decimal(self.paid_amount)

        # Save header first so we have a PK for child rows
        super().save(*args, **kwargs)

        # Create productTransaction rows only if none exist for this header
        try:
            if not productTransaction.objects.filter(transaction=self).exists():
                # Use atomic block so either all child rows are created or none (prevents partials)
                with db_transaction.atomic():
                    for product_item in products_list:
                        try:
                            barcode_raw = str(product_item.get("barcode", "")).strip()
                            # Resolve product item from inventory if possible
                            item = None
                            if barcode_raw:
                                try:
                                    item = Product.objects.filter(barcode=barcode_raw).first()
                                except Exception:
                                    try:
                                        base_barcode = barcode_raw.split("_")[0]
                                        item = Product.objects.filter(barcode=base_barcode).first()
                                    except Exception:
                                        item = None

                            # Prepare safe numeric values used in row creation
                            price = safe_decimal(product_item.get("price", 0))
                            qty = int(product_item.get("quantity", 0) or 0)
                            line_gross = (price * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                            # deposit and deposit_amount
                            deposit_amount = safe_decimal(product_item.get("deposit_value", product_item.get("deposit_amount", 0)))
                            deposit_cat = product_item.get("deposit_category", "")

                            # Determine tax percentage and tax_amount (compute if not present)
                            # Prefer inventory tax percentage if item exists
                            if item:
                                tax_pct_row = safe_decimal(getattr(getattr(item, "tax_category", None), "tax_percentage", 0))
                            else:
                                tax_pct_row = safe_decimal(product_item.get("tax_percentage", product_item.get("tax_pct", 0)))

                            tax_amount_row = safe_decimal(product_item.get("tax_value", 0))
                            if tax_amount_row == Decimal("0.00") and tax_pct_row != Decimal("0.00"):
                                tax_amount_row = self._extract_vat_from_gross(line_gross, tax_pct_row)

                            # cost price (from inventory if available)
                            cost_price = safe_decimal(getattr(item, "cost_price", product_item.get("cost_price", 0)))

                            department_name = getattr(item.department, "department_name", "") if item and getattr(item, "department", None) else product_item.get("department", "")
                            tax_cat_name = getattr(getattr(item, "tax_category", None), "tax_category", "") if item and getattr(item, "tax_category", None) else product_item.get("tax_category", "")

                            # Create productTransaction row
                            productTransaction.objects.create(
                                transaction=self,
                                transaction_id_num=str(self.transaction_id),
                                transaction_date_time=self.transaction_dt,
                                barcode=str(barcode_raw),
                                name=str(product_item.get("name", "")),
                                department=department_name,
                                sales_price=price,
                                qty=qty,
                                cost_price=cost_price,
                                tax_category=tax_cat_name,
                                tax_percentage=tax_pct_row,
                                tax_amount=tax_amount_row,
                                deposit_category=str(deposit_cat or ""),
                                deposit=Decimal(str(product_item.get("deposit", 0))) if product_item.get("deposit", None) is not None else Decimal("0.00"),
                                deposit_amount=deposit_amount,
                                payment_type=str(self.payment_type),
                            )
                        except Exception as e_item:
                            # one bad product row should not block others — log and continue
                            print("transaction.save: failed creating productTransaction for item:", product_item, "error:", e_item)
                            continue
        except Exception as e_create:
            # don't prevent the header from being saved if product rows fail
            print("transaction.save: productTransaction creation failed:", e_create)

        # ------------------------
        # DEBT handling (SAFE — no duplicate creation)
        # ------------------------
        try:
            if str(self.payment_type).upper() == "DEBT" and not self.debt_created:
                with db_transaction.atomic():
                    # lock this transaction row to avoid races
                    tx_locked = transaction.objects.select_for_update().get(pk=self.pk)

                    # double-check after acquiring lock: if a Debt already exists, skip
                    if not Debt.objects.filter(transaction=tx_locked).exists():
                        paid_amt = safe_decimal(tx_locked.paid_amount)
                        due = tx_locked.debt_due_date if tx_locked.debt_due_date else None
                        debtor = tx_locked.debtor_name or ""

                        debt = Debt.objects.create(
                            transaction=tx_locked,
                            total_amount=tx_locked.total_sale,
                            paid_amount=paid_amt,
                            due_date=due,
                            debtor_name=debtor,
                            created_by=tx_locked.user,
                            phone_number=getattr(tx_locked, "phone_number", None) if hasattr(tx_locked, "phone_number") else None
                        )

                        if paid_amt > Decimal("0.00"):
                            DebtPayment.objects.create(
                                debt=debt,
                                amount=paid_amt,
                                method='CASH',
                                note="Initial payment recorded on transaction save",
                                paid_by=tx_locked.user
                            )

                        # update debt status and mark transaction as having created a debt
                        try:
                            debt.update_status()
                        except Exception:
                            pass

                        # mark the transaction as debt_created using update() to avoid re-entering save()
                        transaction.objects.filter(pk=tx_locked.pk).update(debt_created=True)
        except Exception as e_debt:
            print("transaction.save: debt creation failed:", e_debt)

        return self

    class Meta:
        verbose_name_plural = "Transactions"


class productTransaction(models.Model):
    """
    One row per sold item (child of transaction).
    """

    transaction = models.ForeignKey("transaction", on_delete=models.RESTRICT, null=False, blank=False, editable=False)
    transaction_id_num = models.CharField(max_length=50, editable=False, null=False)
    transaction_date_time = models.DateTimeField(editable=False, null=False, blank=False)
    barcode = models.CharField(max_length=32, editable=False, blank=False, null=False)
    name = models.CharField(max_length=125, editable=False, blank=False, null=False)
    department = models.CharField(max_length=125, editable=False, blank=False, null=True)

    # Increase max_digits on money fields to avoid overflow problems
    sales_price = models.DecimalField(max_digits=15, editable=False, decimal_places=2, null=False, blank=False, default=Decimal("0.00"))
    qty = models.IntegerField(default=0, editable=False, null=True)
    cost_price = models.DecimalField(max_digits=15, decimal_places=2, editable=False, default=Decimal("0.00"), null=True)

    tax_category = models.CharField(max_length=125, editable=False, blank=False, null=False)
    tax_percentage = models.DecimalField(max_digits=6, decimal_places=3, validators=PERCENTAGE_VALIDATOR, null=False, blank=False, default=Decimal("0.00"))
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, editable=False, default=Decimal("0.00"), null=True)

    deposit_category = models.CharField(max_length=125, editable=False, blank=False, null=False)
    deposit = models.DecimalField(max_digits=15, decimal_places=2, null=False, blank=False, default=Decimal("0.00"))
    deposit_amount = models.DecimalField(max_digits=15, decimal_places=2, editable=False, default=Decimal("0.00"), null=True)

    payment_type = models.CharField(max_length=32, null=False, editable=False)

    def save(self, *args, **kwargs):
        """
        When saving a productTransaction, normalize numeric fields and decrement product qty safely.
        """
        # normalize numeric fields
        try:
            self.sales_price = safe_decimal(self.sales_price)
        except Exception:
            self.sales_price = safe_decimal(0)
        try:
            self.cost_price = safe_decimal(self.cost_price)
        except Exception:
            self.cost_price = safe_decimal(0)
        try:
            self.tax_amount = safe_decimal(self.tax_amount)
        except Exception:
            self.tax_amount = safe_decimal(0)
        try:
            self.deposit = safe_decimal(self.deposit)
        except Exception:
            self.deposit = safe_decimal(0)
        try:
            self.deposit_amount = safe_decimal(self.deposit_amount)
        except Exception:
            self.deposit_amount = safe_decimal(0)

        # ensure qty is int
        try:
            self.qty = int(self.qty or 0)
        except Exception:
            self.qty = 0

        # decrement product qty atomically using F expression (only if product exists)
        try:
            if Product.objects.filter(barcode=self.barcode).exists():
                # Use update() with F() to avoid race conditions
                Product.objects.filter(barcode=self.barcode).update(qty=F('qty') - (self.qty or 0))
        except Exception as e:
            # don't fail save due to stock update issues
            print("productTransaction.save: failed updating product qty for barcode", self.barcode, "error:", e)

        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.transaction_id_num}_{self.barcode}"

    class Meta:
        verbose_name_plural = "Product Transactions"

# --------- Debt / Payments models (fixed) ----------
class Debt(models.Model):
    """
    Debt/Credit record tied to a transaction (sale or purchase).
    Stores totals, paid amount, due date, and status.
    One-to-one to transaction to prevent duplicate debts for a single sale.
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PARTIAL', 'Partially Paid'),
        ('PAID', 'Paid'),
        ('OVERDUE', 'Overdue'),
    ]

    # OneToOne ensures only one Debt per Transaction (db-level protection)
    transaction = models.OneToOneField(
        "transaction",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="debt"
    )

    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"))
    paid_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='PENDING')
    due_date = models.DateField(null=True, blank=True)
    debtor_name = models.CharField(max_length=200, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    phone_number = models.CharField(max_length=20, null=True, blank=True)

    def __str__(self):
        return f"Debt #{self.pk or 'new'} | {self.debtor_name or 'Unknown'} | {self.balance}"

    @property
    def balance(self):
        try:
            return (safe_decimal(self.total_amount) - safe_decimal(self.paid_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            return safe_decimal(0)

    def update_status(self):
        """
        Update status based on paid_amount, due_date and today's date.
        Uses an efficient DB update for status + updated_at to avoid extra save loops.
        """
        try:
            paid = safe_decimal(self.paid_amount)
            total = safe_decimal(self.total_amount)
            today = dj_timezone.localdate()

            if total > Decimal("0.00") and paid >= total:
                new_status = 'PAID'
            elif paid > Decimal("0.00") and paid < total:
                new_status = 'PARTIAL'
            else:
                if self.due_date and self.due_date < today:
                    new_status = 'OVERDUE'
                else:
                    new_status = 'PENDING'

            # Only update DB when status actually changed (reduces writes)
            if new_status != (self.status or ''):
                self.status = new_status
                # update status and updated_at in one DB call
                Debt.objects.filter(pk=self.pk).update(status=new_status, updated_at=dj_timezone.now())
            else:
                # keep updated_at fresh even if status unchanged
                Debt.objects.filter(pk=self.pk).update(updated_at=dj_timezone.now())

        except Exception:
            # best-effort fallback: attempt a safe save/update of updated_at
            try:
                Debt.objects.filter(pk=self.pk).update(updated_at=dj_timezone.now())
            except Exception:
                pass

    class Meta:
        verbose_name_plural = "Debts"
        ordering = ["-created_at"]



class DebtPayment(models.Model):
    """
    Payment entries against a Debt. Keeps full history (date/time, method, note, who).
    """
    PAYMENT_METHODS = [
        ('CASH', 'CASH'),
        ('DEBIT/CREDIT', 'DEBIT/CREDIT'),
        ('EBT', 'EBT'),
    ]

    debt = models.ForeignKey(Debt, related_name='payments', on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"))
    method = models.CharField(max_length=32, choices=PAYMENT_METHODS, default='CASH')
    note = models.CharField(max_length=255, blank=True, default="")
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Payment {self.amount} on Debt #{self.debt_id or self.debt.pk}"

    def save(self, *args, **kwargs):
        """
        When a DebtPayment is saved, update the parent Debt's paid_amount and status.
        Note: If you create DebtPayment via code, this will auto-update the debt.
        """
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            try:
                # increment parent debt paid_amount atomically to avoid race conditions
                Debt.objects.filter(pk=self.debt.pk).update(paid_amount=F('paid_amount') + safe_decimal(self.amount))
                # refresh and update status
                debt = Debt.objects.get(pk=self.debt.pk)
                debt.update_status()
            except Exception as e:
                print("DebtPayment.save: failed updating parent debt:", e)


# --------- Expenses model (add below productTransaction) ----------
class Expense(models.Model):
    """
    Business expenses (e.g. electricity, rent, supplies).
    """
    CATEGORY_CHOICES = [
        ("RENT", "Rent"),
        ("SALARY", "Salary"),
        ("UTILITY", "Utility"),
        ("SUPPLIES", "Supplies"),
        ("OTHER", "Other"),
    ]

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, editable=False
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES, default="OTHER")
    note = models.TextField(blank=True, default="")

    def __str__(self):
        return f"{self.created_at.date()} | {self.category} | {self.amount}"

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Expense"
        verbose_name_plural = "Expenses"
