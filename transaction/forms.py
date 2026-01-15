from django import forms
from .models import Expense


# -------------------------------
# Expense Form (unchanged, clean)
# -------------------------------
class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["amount", "category", "note"]
        widgets = {
            "amount": forms.NumberInput(
                attrs={
                    "step": "0.01",
                    "class": "form-control",
                    "placeholder": "Amount",
                }
            ),
            "category": forms.Select(
                attrs={
                    "class": "form-control",
                }
            ),
            "note": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Optional note",
                }
            ),
        }


# -----------------------------------
# Transaction Date Filter (FIXED)
# -----------------------------------
class DateSelector(forms.Form):
    start_date = forms.DateField(
        required=False,   # 🔥 IMPORTANT (allows reset & partial filtering)
        label="From",
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "class": "form-control",
                "placeholder": "Start date",
            }
        ),
        input_formats=["%Y-%m-%d"],  # browser native format
    )

    end_date = forms.DateField(
        required=False,   # 🔥 IMPORTANT
        label="To",
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "class": "form-control",
                "placeholder": "End date",
            }
        ),
        input_formats=["%Y-%m-%d"],
    )

    def clean(self):
        """
        Safety validation:
        - Allows empty fields
        - Ensures end_date >= start_date
        """
        cleaned_data = super().clean()
        start = cleaned_data.get("start_date")
        end = cleaned_data.get("end_date")

        if start and end and end < start:
            raise forms.ValidationError(
                "End date cannot be earlier than start date."
            )

        return cleaned_data
