from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from .models import User

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        # Check if quick demo login was clicked
        demo_role = request.POST.get('demo_role')
        if demo_role:
            demo_user = User.objects.filter(role=demo_role).first()
            if not demo_user:
                # Fallback to superuser if any
                demo_user = User.objects.filter(is_superuser=True).first()
            if demo_user:
                login(request, demo_user)
                messages.success(request, f"Logged in as demo {demo_user.get_role_display()} ({demo_user.email})")
                return redirect('dashboard')
            else:
                messages.error(request, "Demo user not found. Please run seed_demo_data.")
                return render(request, 'auth/login.html')

        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')

        user = authenticate(request, username=email, password=password)
        if user is not None:
            if user.is_active:
                login(request, user)
                messages.success(request, f"Welcome back, {user.get_full_name() or user.username}!")
                next_url = request.GET.get('next', 'dashboard')
                return redirect(next_url)
            else:
                messages.error(request, "Your account has been deactivated.")
        else:
            messages.error(request, "Invalid email or password.")

    return render(request, 'auth/login.html')

def logout_view(request):
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect('login')

@login_required
def profile_view(request):
    if request.method == 'POST':
        user = request.user
        user.first_name = request.POST.get('first_name', user.first_name)
        user.last_name = request.POST.get('last_name', user.last_name)
        user.organization = request.POST.get('organization', user.organization)
        user.phone = request.POST.get('phone', user.phone)
        user.save()
        messages.success(request, "Profile updated successfully.")
        return redirect('profile')

    return render(request, 'auth/profile.html')

@login_required
def users_manage_view(request):
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            email = request.POST.get('email', '').strip()
            username = request.POST.get('username', '').strip() or email.split('@')[0]
            password = request.POST.get('password', '')
            role = request.POST.get('role', User.Role.VIEWER)
            first_name = request.POST.get('first_name', '')
            last_name = request.POST.get('last_name', '')

            if User.objects.filter(email=email).exists():
                messages.error(request, f"A user with email '{email}' already exists.")
            else:
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=password,
                    role=role,
                    first_name=first_name,
                    last_name=last_name,
                    is_staff=(role == User.Role.SUPER_ADMIN),
                    is_superuser=(role == User.Role.SUPER_ADMIN)
                )
                messages.success(request, f"User '{user.email}' created successfully.")
                return redirect('users_manage')

        elif action == 'toggle_status':
            user_id = request.POST.get('user_id')
            user_to_toggle = get_object_or_404(User, id=user_id)
            if user_to_toggle != request.user:
                user_to_toggle.is_active = not user_to_toggle.is_active
                user_to_toggle.save()
                messages.success(request, f"Updated active status for {user_to_toggle.email}")
            return redirect('users_manage')

    users = User.objects.all()
    return render(request, 'auth/users.html', {'users': users})


def terms_of_service_view(request):
    """Public Terms of Service page — required by TikTok (and other OAuth providers)."""
    return render(request, 'auth/terms_of_service.html')


def privacy_policy_view(request):
    """Public Privacy Policy page — required by TikTok (and other OAuth providers)."""
    return render(request, 'auth/privacy_policy.html')
