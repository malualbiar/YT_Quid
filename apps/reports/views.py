import csv
from datetime import timedelta
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.utils import timezone
from django.db.models import Sum, Count
from apps.artists.models import Artist, YouTubeChannel
from apps.videos.models import Video, VideoStatisticSnapshot
from apps.analytics.services import AnalyticsService

@login_required
def reports_view(request):
    AnalyticsService.update_video_growth_metrics()
    period = request.GET.get('period', '30d')
    report_type = request.GET.get('type', 'artists')
    artist_id = request.GET.get('artist', '')

    now = timezone.now()
    local_now = timezone.localtime(now)
    if period == '7d':
        start_date = local_now - timedelta(days=7)
    elif period == '90d':
        start_date = local_now - timedelta(days=90)
    elif period == 'all':
        start_date = local_now - timedelta(days=3650)
    else:  # '30d' default
        start_date = local_now - timedelta(days=30)

    artists = Artist.objects.filter(status=Artist.Status.ACTIVE).order_by('stage_name')

    rows = []
    if report_type == 'artists':
        query = artists
        if artist_id:
            query = query.filter(id=artist_id)

        for a in query:
            v_stats = Video.objects.filter(artist=a, is_active=True).aggregate(
                views=Sum('current_views'),
                likes=Sum('current_likes'),
                comments=Sum('current_comments'),
                views_this_week=Sum('views_this_week'),
                views_this_month=Sum('views_this_month'),
                count=Count('id')
            )
            snap_gained = VideoStatisticSnapshot.objects.filter(
                video__artist=a,
                recorded_at__gte=start_date
            ).aggregate(g=Sum('views_change'))['g'] or 0

            if snap_gained > 0:
                gained = snap_gained
            elif period == '7d':
                gained = v_stats['views_this_week'] or 0
            elif period == '30d':
                gained = v_stats['views_this_month'] or 0
            elif period == 'all':
                gained = v_stats['views'] or 0
            else:  # 90d
                gained = min(v_stats['views'] or 0, int((v_stats['views_this_month'] or 0) * 2.8))

            subs = a.channel.subscriber_count if hasattr(a, 'channel') and a.channel else 0

            rows.append({
                'title': a.stage_name,
                'subtitle': a.genre,
                'subscribers': subs,
                'videos_count': v_stats['count'] or 0,
                'total_views': v_stats['views'] or 0,
                'views_gained': gained,
                'total_likes': v_stats['likes'] or 0,
                'total_comments': v_stats['comments'] or 0,
            })
    else:
        # Videos report
        query = Video.objects.filter(is_active=True).select_related('artist')
        if artist_id:
            query = query.filter(artist_id=artist_id)

        for v in query.order_by('-current_views')[:100]:
            snap_gained = VideoStatisticSnapshot.objects.filter(
                video=v,
                recorded_at__gte=start_date
            ).aggregate(g=Sum('views_change'))['g'] or 0

            if snap_gained > 0:
                gained = snap_gained
            elif period == '7d':
                gained = v.views_this_week or 0
            elif period == '30d':
                gained = v.views_this_month or 0
            elif period == 'all':
                gained = v.current_views
            else:  # 90d
                gained = min(v.current_views, int((v.views_this_month or 0) * 2.8))

            rows.append({
                'title': v.title,
                'subtitle': v.artist.stage_name,
                'subscribers': 0,
                'videos_count': 1,
                'total_views': v.current_views,
                'views_gained': gained,
                'total_likes': v.current_likes,
                'total_comments': v.current_comments,
                'like_rate': v.like_rate,
                'comment_rate': v.comment_rate,
            })

    return render(request, 'reports/index.html', {
        'artists': artists,
        'selected_artist': int(artist_id) if artist_id.isdigit() else '',
        'period': period,
        'report_type': report_type,
        'rows': rows,
        'start_date': start_date,
        'end_date': local_now,
    })

@login_required
def export_report_csv(request):
    AnalyticsService.update_video_growth_metrics()
    period = request.GET.get('period', '30d')
    report_type = request.GET.get('type', 'artists')
    artist_id = request.GET.get('artist', '')

    now = timezone.now()
    local_now = timezone.localtime(now)
    if period == '7d':
        start_date = local_now - timedelta(days=7)
    elif period == '90d':
        start_date = local_now - timedelta(days=90)
    elif period == 'all':
        start_date = local_now - timedelta(days=3650)
    else:
        start_date = local_now - timedelta(days=30)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="youtube_analytics_report_{report_type}_{period}_{local_now:%Y%m%d}.csv"'

    writer = csv.writer(response)

    if report_type == 'artists':
        writer.writerow(['Artist Name', 'Genre', 'Subscribers', 'Total Videos', 'Total Views', f'Views Gained ({period.upper()})', 'Total Likes', 'Total Comments'])
        artists = Artist.objects.filter(status=Artist.Status.ACTIVE)
        if artist_id:
            artists = artists.filter(id=artist_id)

        for a in artists:
            v_stats = Video.objects.filter(artist=a, is_active=True).aggregate(
                views=Sum('current_views'), likes=Sum('current_likes'), comments=Sum('current_comments'),
                views_this_week=Sum('views_this_week'), views_this_month=Sum('views_this_month'), count=Count('id')
            )
            snap_gained = VideoStatisticSnapshot.objects.filter(
                video__artist=a, recorded_at__gte=start_date
            ).aggregate(g=Sum('views_change'))['g'] or 0

            if snap_gained > 0:
                gained = snap_gained
            elif period == '7d':
                gained = v_stats['views_this_week'] or 0
            elif period == '30d':
                gained = v_stats['views_this_month'] or 0
            elif period == 'all':
                gained = v_stats['views'] or 0
            else:
                gained = min(v_stats['views'] or 0, int((v_stats['views_this_month'] or 0) * 2.8))

            subs = a.channel.subscriber_count if hasattr(a, 'channel') and a.channel else 0
            writer.writerow([
                a.stage_name, a.genre, subs, v_stats['count'] or 0, v_stats['views'] or 0, gained, v_stats['likes'] or 0, v_stats['comments'] or 0
            ])
    else:
        writer.writerow(['Video Title', 'Artist', 'Release Date', 'Total Views', f'Views Gained ({period.upper()})', 'Likes', 'Comments', 'Like Rate (%)', 'Comment Rate (%)'])
        videos = Video.objects.filter(is_active=True).select_related('artist')
        if artist_id:
            videos = videos.filter(artist_id=artist_id)

        for v in videos.order_by('-current_views'):
            snap_gained = VideoStatisticSnapshot.objects.filter(
                video=v, recorded_at__gte=start_date
            ).aggregate(g=Sum('views_change'))['g'] or 0

            if snap_gained > 0:
                gained = snap_gained
            elif period == '7d':
                gained = v.views_this_week or 0
            elif period == '30d':
                gained = v.views_this_month or 0
            elif period == 'all':
                gained = v.current_views
            else:
                gained = min(v.current_views, int((v.views_this_month or 0) * 2.8))

            pub_str = timezone.localtime(v.published_at).strftime('%Y-%m-%d') if v.published_at else 'N/A'
            writer.writerow([
                v.title, v.artist.stage_name, pub_str, v.current_views, gained, v.current_likes, v.current_comments, v.like_rate, v.comment_rate
            ])

    return response

@login_required
def printable_report_view(request):
    AnalyticsService.update_video_growth_metrics()
    period = request.GET.get('period', '30d')
    report_type = request.GET.get('type', 'artists')
    artist_id = request.GET.get('artist', '')

    now = timezone.now()
    local_now = timezone.localtime(now)
    if period == '7d':
        start_date = local_now - timedelta(days=7)
    elif period == '90d':
        start_date = local_now - timedelta(days=90)
    elif period == 'all':
        start_date = local_now - timedelta(days=3650)
    else:
        start_date = local_now - timedelta(days=30)

    artists = Artist.objects.filter(status=Artist.Status.ACTIVE).order_by('stage_name')
    if artist_id:
        artists = artists.filter(id=artist_id)

    report_items = []
    for a in artists:
        v_stats = Video.objects.filter(artist=a, is_active=True).aggregate(
            views=Sum('current_views'),
            likes=Sum('current_likes'),
            comments=Sum('current_comments'),
            views_this_week=Sum('views_this_week'),
            views_this_month=Sum('views_this_month'),
            count=Count('id')
        )
        snap_gained = VideoStatisticSnapshot.objects.filter(
            video__artist=a,
            recorded_at__gte=start_date
        ).aggregate(g=Sum('views_change'))['g'] or 0

        if snap_gained > 0:
            gained = snap_gained
        elif period == '7d':
            gained = v_stats['views_this_week'] or 0
        elif period == '30d':
            gained = v_stats['views_this_month'] or 0
        elif period == 'all':
            gained = v_stats['views'] or 0
        else:
            gained = min(v_stats['views'] or 0, int((v_stats['views_this_month'] or 0) * 2.8))

        subs = a.channel.subscriber_count if hasattr(a, 'channel') and a.channel else 0
        top_songs = Video.objects.filter(artist=a, is_active=True).order_by('-current_views')[:5]

        report_items.append({
            'artist': a,
            'subscribers': subs,
            'total_videos': v_stats['count'] or 0,
            'total_views': v_stats['views'] or 0,
            'views_gained': gained,
            'total_likes': v_stats['likes'] or 0,
            'total_comments': v_stats['comments'] or 0,
            'top_songs': top_songs,
        })

    return render(request, 'reports/printable.html', {
        'report_items': report_items,
        'period': period,
        'start_date': start_date,
        'end_date': local_now,
    })
