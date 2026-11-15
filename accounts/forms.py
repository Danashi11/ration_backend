from django import forms
from django.contrib.auth import authenticate
from .models import User


class LoginForm(forms.Form):
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get('email')
        password = cleaned.get('password')
        if email and password:
            account = User.objects.filter(email__iexact=email).first()
            if account:
                self.user = authenticate(self.request, username=account.username, password=password)
            if self.user is None:
                raise forms.ValidationError('Invalid email or password.')
        return cleaned
