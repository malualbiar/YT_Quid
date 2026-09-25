from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import VideoMilestone, NotificationLog

@login_required
def milestones_list_view(request):
    milestone_type = request.GET.get('type', '')
    milestones = VideoMilestone.objects.filter(video__is_active=True).select_related('video', 'video__artist')

    if milestone_type:
        milestones = milestones.filter(milestone_type=milestone_type)

    types = VideoMilestone.MilestoneType.choices

    return render(request, 'milestones/list.html', {
        'milestones': milestones,
        'types': types,
        'selected_type': milestone_type,
    })

@login_required
def notifications_list_view(request):
    if request.method == 'POST' and request.POST.get('action') == 'mark_all_read':
        NotificationLog.objects.filter(is_read=False).update(is_read=True)
        messages.success(request, "All notifications marked as read.")
        return redirect('notifications_list')

    notifications = NotificationLog.objects.all()
    return render(request, 'milestones/notifications.html', {
        'notifications': notifications,
    })
