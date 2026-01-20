# inventory/views.py
from django.shortcuts import redirect, render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.urls import reverse
from django import forms
from django.forms import TextInput

from .models import Product, StockAdjustment
from .forms import StockAdjustmentForm

from cart.models import Cart
import decimal


# --- existing simple forms you already used in project --- #
class ProductLookup(forms.Form):
    barcode = forms.CharField(
        widget=TextInput(attrs={
            'autocomplete': "off",
            'placeholder': "Please Enter Barcode...",
            'style': "width:100%;padding: 10px;"
        }),
        max_length=32,
    )


class AddProduct(forms.Form):
    qty = forms.IntegerField(label="Quantity To Be Added", widget=TextInput(attrs={'style': "width:100%"}))
    barcode = forms.CharField(
        label="Product Barcode",
        widget=TextInput(attrs={'autofocus': "autofocus", 'autocomplete': "off", 'style': "width:100%"}),
        max_length=32,
    )


# -----------------------
# Existing views (kept)
# -----------------------
@login_required(login_url="/user/login")
def product_lookup(request):
    obj = None
    notFound = False
    if request.method == "POST":
        form = ProductLookup(request.POST)
        if form.is_valid():
            barcode = form.cleaned_data['barcode'].strip()
            try:
                obj = Product.objects.get(barcode=barcode)
            except Product.DoesNotExist:
                obj = None
                notFound = True
    else:
        form = ProductLookup()

    context = {'form': form, 'notFound': notFound}
    if obj:
        context['obj'] = obj
    return render(request, "productLookup.html", context=context)


@login_required(login_url="/user/login")
def manualAmount(request, manual_department, amount):
    cart = Cart(request)
    product = Product.objects.filter(barcode=manual_department).first()
    if product:
        amount = round(decimal.Decimal(amount), 2)
        product.barcode = f"{product.barcode}_{amount}".replace(".", "")
        product.sales_price = amount
        cart.add(product=product, quantity=int(1))
        return redirect('register')
    else:
        scheme = request.is_secure() and "https" or "http"
        return redirect(f"{scheme}://{request.get_host()}/register/ProductNotFound/")


from .models import InventoryHistory

@login_required(login_url="/user/login")
def inventoryAdd(request):
    context = {}
    if request.method == "POST":
        form = AddProduct(request.POST)
        if form.is_valid():
            try:
                obj = Product.objects.get(barcode=form.cleaned_data['barcode'])
                context['p_qty'] = obj.qty
                context['n_qty'] = int(form.cleaned_data['qty'])
                obj.qty = obj.qty + context['n_qty']
                obj.save()

                # --- save history ---
                InventoryHistory.objects.create(
                    product=obj,
                    added_by=request.user,
                    previous_qty=context['p_qty'],
                    added_qty=context['n_qty'],
                    total_qty=obj.qty,
                    phone_number=request.POST.get('phone_number')  # optional if you add field in form
                )

            except Product.DoesNotExist:
                obj = None
                context['notFound'] = form.cleaned_data['barcode']
            context['obj'] = obj
    form = AddProduct(initial={'qty': 1})
    context['form'] = form
    return render(request, 'addInventory.html', context=context)


from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from .models import InventoryHistory

@login_required(login_url="/user/login")
def inventory_history(request, product_id=None):
    """
    Show history of inventory additions. Optional filter by product (?p=ID)
    """
    q_prod = product_id or request.GET.get("p")
    page = request.GET.get("page", 1)

    if q_prod:
        product = get_object_or_404(Product, pk=int(q_prod))
        qs = InventoryHistory.objects.filter(product=product).select_related("added_by", "product").order_by("-timestamp")
    else:
        product = None
        qs = InventoryHistory.objects.select_related("added_by", "product").order_by("-timestamp")

    paginator = Paginator(qs, 25)  # 25 rows per page
    try:
        history_page = paginator.page(page)
    except PageNotAnInteger:
        history_page = paginator.page(1)
    except EmptyPage:
        history_page = paginator.page(paginator.num_pages)

    context = {
        "product": product,
        "history": history_page,
        "paginator": paginator,
    }
    return render(request, "inventory_history.html", context=context)


# -----------------------
# New: Stock adjustment views (fixed)
# -----------------------
@login_required(login_url="/user/login")
def stock_adjustment(request):
    """
    Scanner-friendly page:
      - Step 1: user scans barcode (POST 'action' == 'lookup') -> we find the product and display adjustment form
      - Step 2: user fills adjustment form (action == 'adjust') -> StockAdjustment created and product.qty decreased
    Template expected: templates/inventory/stock_adjustment.html
    """
    product = None
    notFound = False
    lookup_form = ProductLookup()
    adjust_form = None

    if request.method == "POST":
        action = request.POST.get("action")  # hidden field from template: 'lookup' or 'adjust'

        # ---------- Barcode lookup ----------
        if action == "lookup" or (action is None and "barcode" in request.POST and "action" not in request.POST):
            lookup_form = ProductLookup(request.POST)
            if lookup_form.is_valid():
                barcode = lookup_form.cleaned_data['barcode'].strip()
                try:
                    product = Product.objects.get(barcode=barcode)
                except Product.DoesNotExist:
                    product = None
                    notFound = True
            else:
                notFound = True

        # ---------- Adjustment submission ----------
        elif action == "adjust" or ("product_id" in request.POST and "action" not in request.POST):
            prod_id = request.POST.get("product_id")
            if prod_id:
                product = get_object_or_404(Product, pk=prod_id)
                # instantiate form with product context so it validates against product stock
                adjust_form = StockAdjustmentForm(request.POST, product=product)
                if adjust_form.is_valid():
                    try:
                        # use save(commit=True, user=request.user) if your form supports user param
                        # our StockAdjustmentForm.save() signature supports (commit=True, user=None)
                        adjustment = adjust_form.save(commit=True, user=request.user)
                        messages.success(request, f"{adjustment.adjustment_type} recorded: {adjustment.quantity} x {product.name}. Stock updated.")
                        product.refresh_from_db()
                        # Redirect to same page with product shown to avoid double-post
                        return redirect(reverse("stock_adjustment") + f"?p={product.pk}")
                    except forms.ValidationError as e:
                        adjust_form.add_error(None, e)
                    except Exception as e:
                        adjust_form.add_error(None, f"Failed to save adjustment: {e}")
                else:
                    messages.error(request, "Please correct the errors in the adjustment form.")
            else:
                messages.error(request, "Product not provided for adjustment.")

    # If query param ?p=ID present (redirect target), load that product so page shows adjustment form automatically
    if not product:
        q_prod = request.GET.get("p")
        if q_prod:
            try:
                product = Product.objects.get(pk=int(q_prod))
            except Exception:
                product = None

    # Prepare adjustment form if product exists and none already created during POST handling
    if product and adjust_form is None:
        # instantiate empty form for display; view will let template provide product_id hidden field
        adjust_form = StockAdjustmentForm(product=product)

    context = {
        "lookup_form": lookup_form,
        "product": product,
        "notFound": notFound,
        # IMPORTANT: use 'adjust_form' key to match templates that render {{ adjust_form.* }}
        "adjust_form": adjust_form,
    }
    return render(request, "inventory/stock_adjustment.html", context=context)


from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Sum
from .models import Product, StockAdjustment


@login_required(login_url="/user/login")
def stock_adjustment_history(request, product_id=None):
    """
    Show history of stock adjustments. Supports optional product filter (?p=ID or product_id arg)
    and pagination (?page=N). Returns totals and a paginated page object to template.
    """
    q_prod = product_id or request.GET.get("p")
    page = request.GET.get("page", 1)

    if q_prod:
        product = get_object_or_404(Product, pk=int(q_prod))
        qs = StockAdjustment.objects.filter(product=product).select_related("created_by", "product").order_by("-created_at")
    else:
        product = None
        qs = StockAdjustment.objects.select_related("created_by", "product").order_by("-created_at")

    # Pagination
    paginator = Paginator(qs, 25)  # 25 rows per page
    try:
        adjustments_page = paginator.page(page)
    except PageNotAnInteger:
        adjustments_page = paginator.page(1)
    except EmptyPage:
        adjustments_page = paginator.page(paginator.num_pages)

    # Totals (for the whole queryset, not just current page)
    totals = qs.aggregate(total_removed=Sum("quantity"))
    total_removed = totals.get("total_removed") or 0
    count_all = qs.count()

    context = {
        "product": product,
        "adjustments": adjustments_page,   # page object (iterable)
        "paginator": paginator,
        "total_removed": total_removed,
        "count_all": count_all,
    }
    return render(request, "inventory/stock_adjustment_history.html", context=context)
