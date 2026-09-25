from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('studio', '0018_add_time_offset_to_lyric_video_project'),
    ]

    operations = [
        migrations.AddField(
            model_name='shortvideoproject',
            name='source_yt_url',
            field=models.URLField(blank=True, default='', help_text='Original YouTube URL if video was downloaded via yt-dlp', max_length=500),
        ),
        migrations.AddField(
            model_name='shortvideoproject',
            name='yt_video_title',
            field=models.CharField(blank=True, default='', help_text='Title from the original YouTube video', max_length=500),
        ),
        migrations.AddField(
            model_name='shortvideoproject',
            name='yt_video_description',
            field=models.TextField(blank=True, default='', help_text='Description from the original YouTube video'),
        ),
        migrations.AddField(
            model_name='shortvideoproject',
            name='yt_video_tags',
            field=models.JSONField(blank=True, default=list, help_text='Tags/keywords from the original YouTube video'),
        ),
        migrations.AddField(
            model_name='shortvideoproject',
            name='yt_channel_name',
            field=models.CharField(blank=True, default='', help_text='Channel name from the original YouTube video', max_length=255),
        ),
    ]
