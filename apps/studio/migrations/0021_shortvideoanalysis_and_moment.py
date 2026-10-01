from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('studio', '0020_shortvideoproject_show_cta_badge'),
    ]

    operations = [
        migrations.CreateModel(
            name='ShortVideoAnalysis',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('model', models.CharField(max_length=100)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('ANALYZING', 'Analyzing'), ('COMPLETED', 'Completed'), ('FAILED', 'Failed')], default='PENDING', max_length=20)),
                ('video_summary', models.TextField(blank=True, default='')),
                ('video_duration', models.FloatField(default=0.0)),
                ('chunk_count', models.PositiveIntegerField(default=0)),
                ('completed_chunks', models.PositiveIntegerField(default=0)),
                ('error_message', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='viral_analyses', to='studio.shortvideoproject')),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='ShortVideoMoment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('start_seconds', models.FloatField()),
                ('end_seconds', models.FloatField()),
                ('title', models.CharField(max_length=180)),
                ('description', models.TextField(blank=True, default='')),
                ('category', models.CharField(default='other', max_length=60)),
                ('reason', models.TextField(blank=True, default='')),
                ('suggested_duration', models.FloatField(default=0.0)),
                ('score', models.PositiveSmallIntegerField(default=0)),
                ('score_components', models.JSONField(blank=True, default=dict)),
                ('needs_context', models.BooleanField(default=False)),
                ('thumbnail', models.ImageField(blank=True, upload_to='studio/viral_moment_thumbnails/')),
                ('selected', models.BooleanField(default=False)),
                ('analysis', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='moments', to='studio.shortvideoanalysis')),
            ],
            options={'ordering': ['start_seconds', 'id']},
        ),
    ]