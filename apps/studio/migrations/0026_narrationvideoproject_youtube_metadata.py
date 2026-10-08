from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('studio', '0025_narrationvideoproject_caption_font_size_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='narrationvideoproject',
            name='youtube_metadata',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='narrationvideoproject',
            name='youtube_metadata_error',
            field=models.TextField(blank=True, default=''),
        ),
    ]
