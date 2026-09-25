from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('studio', '0019_shortvideoproject_yt_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='shortvideoproject',
            name='show_cta_badge',
            field=models.BooleanField(default=True, help_text='Show bottom "Full Video on YouTube" CTA pill'),
        ),
    ]
