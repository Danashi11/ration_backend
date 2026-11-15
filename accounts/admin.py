from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminUserCreationForm

from .models import User


class UserCreationForm(AdminUserCreationForm):
    """Accounts are only created here, so ask for everything login and roles need."""

    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ('username', 'email', 'full_name', 'role')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].required = True
        self.fields['full_name'].required = True


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    add_form = UserCreationForm
    list_display = ('username', 'full_name', 'email', 'role', 'is_active')
    list_filter = ('role', 'is_active')
    search_fields = ('username', 'full_name', 'email')
    fieldsets = UserAdmin.fieldsets + (('Ration', {'fields': ('full_name', 'role')}),)
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'full_name', 'role', 'usable_password', 'password1', 'password2'),
        }),
    )
