# inventory/forms.py
from django import forms
from django.core.exceptions import ValidationError

from .models import Product, StockAdjustment


class ProductSearchForm(forms.Form):
    """
    Scanner-friendly barcode lookup. Returns a Product instance from clean_barcode.
    """
    barcode = forms.CharField(
        label="Scan or Enter Barcode",
        max_length=64,
        widget=forms.TextInput(
            attrs={
                "autofocus": "autofocus",
                "autocomplete": "off",
                "placeholder": "Scan barcode or type here...",
                "class": "form-control form-control-lg",
            }
        ),
    )

    def clean_barcode(self):
        val = self.cleaned_data["barcode"].strip()
        try:
            return Product.objects.get(barcode=val)
        except Product.DoesNotExist:
            raise ValidationError("Product with this barcode was not found.")


class StockAdjustmentForm(forms.ModelForm):
    """
    Form for creating expired / damaged stock entries.

    - Uses ONLY `quantity` (the model field). Do NOT create a separate `qty` form field.
    - The view must pass `product=product` when instantiating the form so validation can check stock.
    - This form does NOT modify product stock; StockAdjustment.model.save() should handle that atomically.
    """

    class Meta:
        model = StockAdjustment
        fields = ("adjustment_type", "quantity", "note")
        widgets = {
            "adjustment_type": forms.Select(attrs={"class": "form-select form-select-lg"}),
            "quantity": forms.NumberInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "min": 1,
                    "placeholder": "Enter quantity",
                }
            ),
            "note": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 2,
                    "placeholder": "Optional note (reason, batch, expiry date...)",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        # Expect product to be passed in kwargs:
        #   form = StockAdjustmentForm(product=product, data=request.POST)
        self.product = kwargs.pop("product", None)
        super().__init__(*args, **kwargs)

        # Use model-defined choices (robust if model constants change)
        try:
            self.fields["adjustment_type"].choices = StockAdjustment.ADJUSTMENT_TYPE_CHOICES
        except Exception:
            # Fallback to basic choices
            self.fields["adjustment_type"].choices = [
                (StockAdjustment.ADJUSTMENT_DAMAGED, "Damaged"),
                (StockAdjustment.ADJUSTMENT_EXPIRED, "Expired"),
            ]

    def clean_quantity(self):
        """
        Validate quantity (required, >0, not more than available product stock).
        """
        qty = self.cleaned_data.get("quantity")
        if qty is None:
            raise ValidationError("Quantity is required.")
        if qty <= 0:
            raise ValidationError("Quantity must be greater than zero.")

        if not self.product:
            raise ValidationError("Product context is missing (pass product=product to form).")

        # Support common product stock field names
        available = getattr(self.product, "qty", None)
        if available is None:
            available = getattr(self.product, "quantity", None)
        if available is None:
            available = getattr(self.product, "stock", None)

        if available is not None and qty > available:
            raise ValidationError(f"Only {available} units available in stock.")

        return qty

    def save(self, commit=True, user=None):
        """
        Save StockAdjustment only. The StockAdjustment model.save() must handle product stock changes.
        """
        obj = super().save(commit=False)
        obj.product = self.product

        if user is not None and getattr(user, "is_authenticated", False):
            try:
                obj.created_by = user
            except Exception:
                pass

        if commit:
            obj.save()

        return obj
