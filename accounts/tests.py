from accounts.models import User
from django.test import TestCase
from django.urls import reverse


class AuthFlowTests(TestCase):
    def test_home_requires_login(self):
        response = self.client.get(reverse('home'))
        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_register_page_is_gone(self):
        self.assertEqual(self.client.get('/register/').status_code, 404)
        self.assertNotContains(self.client.get(reverse('login')), 'Register')

    def test_login_with_email(self):
        User.objects.create_user('adel', 'adel@example.com', 'S0me-strong-pass')
        response = self.client.post(reverse('login'), {
            'email': 'ADEL@example.com',
            'password': 'S0me-strong-pass',
        })
        self.assertRedirects(response, reverse('home'), fetch_redirect_response=False)

    def test_login_wrong_password(self):
        User.objects.create_user('adel', 'adel@example.com', 'S0me-strong-pass')
        response = self.client.post(reverse('login'), {
            'email': 'adel@example.com',
            'password': 'wrong',
        })
        self.assertContains(response, 'Invalid email or password.')

    def test_logout(self):
        User.objects.create_user('adel', 'adel@example.com', 'S0me-strong-pass')
        self.client.login(username='adel', password='S0me-strong-pass')
        response = self.client.post(reverse('logout'))
        self.assertRedirects(response, reverse('login'))


class AdminUserCreationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('boss', 'boss@example.com', 'S0me-strong-pass')
        self.client.force_login(self.admin)
        self.url = reverse('admin:accounts_user_add')

    def test_admin_creates_user_with_email_name_and_role(self):
        response = self.client.post(self.url, {
            'username': 'acc', 'email': 'acc@example.com', 'full_name': 'Ana Accountant', 'role': 'accountant',
            'usable_password': 'true', 'password1': 'An0ther-strong-pass', 'password2': 'An0ther-strong-pass',
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='acc')
        self.assertEqual((user.email, user.full_name, user.role), ('acc@example.com', 'Ana Accountant', 'accountant'))
        self.assertTrue(self.client.login(username='acc', password='An0ther-strong-pass'))

    def test_admin_add_form_requires_email(self):
        response = self.client.post(self.url, {
            'username': 'acc', 'email': '', 'full_name': 'Ana', 'role': 'creator',
            'usable_password': 'true', 'password1': 'An0ther-strong-pass', 'password2': 'An0ther-strong-pass',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='acc').exists())
