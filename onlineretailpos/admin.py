from django.contrib import admin
from django.contrib.admin.apps import AdminConfig
from django.conf import settings


class MyAdminSite(admin.AdminSite):
    # ✅ safe: won't crash if STORE_NAME isn't ready yet
    site_header = f"{getattr(settings, 'STORE_NAME', 'Online Retail POS')} - Data Portal"
    site_title = getattr(settings, "STORE_NAME", "Online Retail POS")
    index_title = "Data Administration"


class MyAdminConfig(AdminConfig):
    default_site = "onlineretailpos.admin.MyAdminSite"
